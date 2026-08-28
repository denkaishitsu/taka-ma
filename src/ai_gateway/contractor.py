"""契約化 — 会話から実行契約を取り出す（設計書 §8.4「契約化の呼び出し」/ §8.10f）。

依頼理解の構造化（会話 → {directive, constraints, acceptance, runbook, workspace,
needs_repo, rest_summary}）は分解・分類と同族の ya-ta 業務。従来は sa-ru の会話脳
（MoE）が担っていたが、構造化出力が実測で不安定だった（2026-08-28 E2E: 5 回中 4 回
仕様違反）ため ya-ta（分解と同じ dense モデル）へ移管し、失敗時は昇格ラダー
（§8.4.x (e)）で上位 worker モデルが同じ業務を引き受ける。

責務の境界（設計書 §8.4）:
- ya-ta（本モジュール）が持つのは「抽出」と「昇格の段取り」まで
- 受理判断は sa-ru 側の validate_contract のみ。本モジュールは呼び出し元から
  検証関数を受け取って合否を聞くだけで、検証規則を持たない（権威はフィールド）
- 上位モデルの実行手段（SSH・worker CLI）は sa-ru が escalate_runner として注入する
  （ya-ta モジュールに SSH・CLI 依存を持ち込まない — ライブラリ方式の維持）
"""

import json
import logging
from pathlib import Path

from ai_gateway.llm import (
    GenerationProgress,
    OllamaConnectionError,
    OllamaTimeoutError,
    extract_json,
    repair_json_escapes,
    run_ollama,
)
from ai_gateway.logger import YaTaLogger

logger = logging.getLogger("ya-ta.contractor")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# ローカルモデル（ya-ta.model）での試行回数。2 回連続の不合格で昇格ラダーへ進む
# （設計書 §8.4.x (e) 発火条件。従来 sa-ru 実装の 1 回リトライと同じ回数）
LOCAL_ATTEMPTS = 2


class Contractor:
    """会話履歴（二窓ビュー）と確定要約から実行契約 JSON を抽出する（設計書 §8.4）。

    ローカルモデルで LOCAL_ATTEMPTS 回試行し、いずれも検証不合格なら昇格ラダー
    （routing.escalation.ladder）の各モデルで 1 回ずつ再契約化する。どの段の出力も
    受理判断は呼び出し元が注入する検証関数（sa-ru の validate_contract）が行う。
    全段失敗で (None, 来歴) を返し、呼び出し側が fail-closed で人へ差し戻す。
    """

    def __init__(self, config, escalate_runner=None):
        """モデル・接続先・ラダー・昇格実行手段を用意する。

        Args:
            config: sa-ru / ya-ta マージ済み設定。モデル名・タイムアウト・think は
                ya-ta.yaml の既存キーを共用する（契約化専用キーは新設しない・§8.4）。
            escalate_runner: 上位モデルでの再契約化の実行手段
                （(model_name, prompt) -> 出力テキスト。失敗は例外）。sa-ru が注入する。
                None なら昇格せずローカル失敗で確定する（段階導入・単体テスト用）。
        """
        self.model = config["ya-ta"]["model"]
        # 接続先はマージ済み config の sa-ru.ollama_host を唯一の源にする（設計書 §8.4）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        self.llm_timeout = config["ya-ta"]["llm_timeout_sec"]
        self.llm_think = config["ya-ta"].get("llm_think")
        # 昇格ラダーは worker 実行の昇格（§8.4.x (a)）と同じキーを共用する。
        # 未構成なら空＝昇格なし（ローカル失敗で fail-closed へ）
        self.ladder = list(((config.get("routing") or {}).get("escalation") or {})
                           .get("ladder") or [])
        self.escalate_runner = escalate_runner
        self.logger = YaTaLogger()
        # 契約化プロンプトは静的なので起動時に 1 度だけ読む
        self._template = (PROMPTS_DIR / "contract.md").read_text()

    def contract(self, history_text: str, summary: str, validate,
                 progress: GenerationProgress | None = None) -> tuple[dict | None, dict]:
        """会話と確定要約から検証済み契約を得る。(契約 | None, 来歴) を返す。

        Args:
            history_text: 会話履歴の二窓ビューを整形したテキスト（セッションの持ち主は
                sa-ru。ya-ta は状態を持たない・§8.4）。
            summary: 確定要約。
            validate: 受理判断（parsed dict -> (検証済み契約 | None, 逸脱理由リスト)）。
                sa-ru の validate_contract を束ねたもの。
            progress: ハートビート進捗通知（§10.8）へ生成トークン数を届ける共有ホルダー
                （ローカル ollama 呼び出しのみ。昇格段の SSH 単発は対象外）。

        来歴 dict: {"origin": "local" | モデル名 | None, "local_failures": int,
                    "attempts": [{"model": str, "problems": [str, ...]}, ...]}
        origin=None は全段失敗（呼び出し側が fail-closed で人へ差し戻す）。
        """
        prompt = (self._template
                  .replace("{history}", history_text)
                  .replace("{summary}", summary))
        attempts: list[dict] = []

        # ローカルモデル（ya-ta.model）で LOCAL_ATTEMPTS 回
        for _ in range(LOCAL_ATTEMPTS):
            validated = self._attempt(
                self.model, attempts,
                lambda: run_ollama(self.model, prompt, timeout=self.llm_timeout,
                                   host=self.ollama_host, think=self.llm_think,
                                   progress=progress),
                validate)
            if validated is not None:
                return validated, self._provenance("local", attempts)

        # 昇格ラダー（§8.4.x (e)）。各段 1 回・同一プロンプト・同一検証。
        # 段の実行エラー（SSH 不達・CLI エラー・タイムアウト）も検証不合格と同列に
        # その段の失敗として次段へ進む
        local_failures = len(attempts)
        for name in self.ladder:
            if self.escalate_runner is None:
                break
            validated = self._attempt(
                name, attempts,
                lambda name=name: self.escalate_runner(name, prompt),
                validate)
            if validated is not None:
                logger.info("契約化を昇格で確定: ローカル %d 回不合格 → %s",
                            local_failures, name)
                return validated, self._provenance(name, attempts,
                                                   local_failures=local_failures)

        # 全段失敗 — fail-closed の最終防衛は呼び出し側（着手確認を出さず人へ・§8.10f）
        return None, self._provenance(None, attempts, local_failures=local_failures)

    def _attempt(self, model_name: str, attempts: list[dict], run, validate):
        """1 回の契約化試行。成功なら検証済み契約、失敗なら None（attempts へ記録）。"""
        try:
            stdout = run()
            try:
                parsed = json.loads(extract_json(stdout))
            except json.JSONDecodeError:
                # 不正エスケープの機械修復（sa-ru _invoke_llm と同じ規律・2026-08-24 実測）
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
        except (json.JSONDecodeError, OllamaTimeoutError, OllamaConnectionError,
                RuntimeError, OSError) as e:
            logger.warning("契約化の実行失敗（%s）: %s", model_name, e)
            attempts.append({"model": model_name, "problems": [f"実行失敗: {e}"]})
            return None
        validated, problems = validate(parsed)
        if validated is not None:
            attempts.append({"model": model_name, "problems": []})
            return validated
        logger.warning("契約化の出力が逸脱（%s）: %s", model_name, problems)
        attempts.append({"model": model_name, "problems": problems})
        return None

    def _provenance(self, origin, attempts: list[dict], local_failures: int = 0) -> dict:
        """来歴を組み立て、試行列を判定ログ（§8.4.1）へ記録する（ラダー較正の実データ）。"""
        provenance = {"origin": origin, "local_failures": local_failures,
                      "attempts": attempts}
        # ログ書き込み失敗は契約化本体を壊さない（decompose の判定ログと同じ耐障害方針）
        try:
            self.logger.log_contract(origin, attempts)
        except Exception:
            pass
        return provenance
