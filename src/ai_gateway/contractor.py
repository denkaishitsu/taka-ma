"""契約化 — 会話から実行契約を取り出す（設計書 §8.4「契約化の呼び出し」/ §8.10f）。

依頼理解の構造化（会話 → {directive, constraints, acceptance, runbook, workspace,
branch, target_paths, needs_repo, rest_summary, unmapped}）は分解・分類と同族の
ya-ta 業務。LLM バックエンドは worker CLI の上位モデル（既定 opus）を用いる —
会話脳（MoE）→ ローカル dense と移した旧構成はいずれも実測で不合格が続いた
（2026-08-28 E2E: 5 回中 4 回仕様違反、2026-08-29: 構造合格・意味不正）。契約化は
ready 毎 1 回と最低頻度で誤りのコストが最大のため、確実なモデルをこの 1 点に使う。

責務の境界（設計書 §8.4）:
- ya-ta（本モジュール）が持つのは「抽出」と「縮退の段取り」まで
- 受理判断は sa-ru 側の validate_contract のみ。本モジュールは呼び出し元から
  検証関数を受け取って合否を聞くだけで、検証規則を持たない（権威はフィールド）
- worker CLI の実行手段（SSH）は sa-ru が cli_runner として注入する
  （ya-ta モジュールに SSH・CLI 依存を持ち込まない — ライブラリ方式の維持）
- 縮退: CLI の呼び出し自体の失敗（SSH 不達・CLI エラー・認証失効。検証不合格は
  含まない）時のみローカル ya-ta.model で契約化する。検証は同一
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

# 同一バックエンドでの試行回数（検証不合格の 1 回リトライ）。2 回連続の不合格で
# fail-closed（設計書 §8.10f。既定バックエンドが最上位のため昇格ラダーは適用しない）
ATTEMPTS = 2

# 旧名の互換（既存テストが参照。値の意味は同一 = 1 バックエンドあたりの試行回数）
LOCAL_ATTEMPTS = ATTEMPTS


def _is_unmapped(problems: list[str]) -> bool:
    """検証不合格がスキーマ閉包の unmapped 検出か（§8.10f。リトライ対象ではない）。"""
    return any(p.startswith("unmapped:") for p in problems)


class Contractor:
    """会話履歴（二窓ビュー）と確定要約から実行契約 JSON を抽出する（設計書 §8.4）。

    既定は worker CLI（contractor.model・既定 opus）で ATTEMPTS 回試行し、呼び出し
    自体の失敗が続いたときのみローカル ya-ta.model へ縮退する。どのバックエンドの
    出力も受理判断は呼び出し元が注入する検証関数（sa-ru の validate_contract）が行う。
    不合格 2 回で (None, 来歴) を返し、呼び出し側が fail-closed で人へ差し戻す。
    """

    def __init__(self, config, escalate_runner=None):
        """モデル・接続先・バックエンド・CLI 実行手段を用意する。

        Args:
            config: sa-ru / ya-ta マージ済み設定。契約化のバックエンドは
                ya-ta.yaml の contractor.backend / contractor.model（§8.4）。
                縮退（ローカル実行）は ya-ta.yaml の既存キー（model /
                llm_timeout_sec / llm_think）を共用する。
            escalate_runner: worker CLI 実行手段（(model_name, prompt) -> 出力
                テキスト。失敗は例外）。sa-ru が注入する。None なら CLI を呼べない
                ため、backend 設定にかかわらずローカルのみで動く（単体テスト・
                段階導入用の縮退）。
        """
        ya = config["ya-ta"]
        contractor_conf = ya.get("contractor") or {}
        # 既定バックエンドは worker CLI・モデルは models レジストリのキー（既定 opus）。
        # モデル名の直書きをせず、実体は models.<key> の command / model_flag が持つ
        self.backend = contractor_conf.get("backend", "worker_cli")
        self.cli_model = contractor_conf.get("model", "opus")
        self.model = ya["model"]  # 縮退（ローカル契約化）用
        # 接続先はマージ済み config の sa-ru.ollama_host を唯一の源にする（設計書 §8.4）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        self.llm_timeout = ya["llm_timeout_sec"]
        self.llm_think = ya.get("llm_think")
        self.escalate_runner = escalate_runner
        self.logger = YaTaLogger()
        # 契約化プロンプトは静的なので起動時に 1 度だけ読む
        self._template = (PROMPTS_DIR / "contract.md").read_text()

    def contract(self, history_text: str, summary: str, validate,
                 progress: GenerationProgress | None = None) -> tuple[dict | None, dict]:
        """会話と確定要約から検証済み契約を得る。(契約 | None, 来歴) を返す。

        Args:
            history_text: 会話履歴の二窓ビューを整形したテキスト（発話 id 注記つき。
                セッションの持ち主は sa-ru。ya-ta は状態を持たない・§8.4）。
            summary: 確定要約。
            validate: 受理判断（parsed dict -> (検証済み契約 | None, 逸脱理由リスト)）。
                sa-ru の validate_contract を束ねたもの（出所束縛込み・§8.10f）。
            progress: ハートビート進捗通知（§10.8）へ生成トークン数を届ける共有ホルダー
                （ローカル縮退時の ollama 呼び出しのみ。CLI の SSH 単発は対象外）。

        来歴 dict: {"origin": モデル名 | "local" | None, "backend": "worker_cli" | "local",
                    "degraded": bool, "attempts": [{"model", "problems"}...],
                    "unmapped": [逐語引用, ...]（スキーマ閉包検出時のみ）}
        origin=None は不成立（呼び出し側が fail-closed で人へ差し戻す）。
        """
        prompt = (self._template
                  .replace("{history}", history_text)
                  .replace("{summary}", summary))
        attempts: list[dict] = []

        use_cli = self.backend == "worker_cli" and self.escalate_runner is not None
        if use_cli:
            exec_failures = 0
            for _ in range(ATTEMPTS):
                validated, status = self._attempt(
                    self.cli_model, attempts,
                    lambda: self.escalate_runner(self.cli_model, prompt),
                    validate)
                if validated is not None:
                    return validated, self._provenance(
                        self.cli_model, "worker_cli", attempts)
                if status == "unmapped":
                    # スキーマ閉包の正常な検出（§8.10f）。リトライせず直ちに人へ
                    return None, self._provenance(
                        None, "worker_cli", attempts,
                        unmapped=self._unmapped_items(attempts))
                if status == "exec_error":
                    exec_failures += 1
            if exec_failures < ATTEMPTS:
                # 検証不合格を含む失敗 = モデルは応答している。最上位で不合格の契約を
                # ローカルへ落とす意味はない（fail-closed・§8.4）
                return None, self._provenance(None, "worker_cli", attempts)
            # 呼び出し自体の失敗のみ → ローカル縮退（§8.4「契約化の呼び出し」縮退）
            logger.warning("契約化 CLI が %d 回とも呼び出し失敗 → ローカル縮退（%s）",
                           exec_failures, self.model)

        # ローカル契約化（縮退・または backend=local / CLI 実行手段なし）
        degraded = use_cli
        for _ in range(ATTEMPTS):
            validated, status = self._attempt(
                self.model, attempts,
                lambda: run_ollama(self.model, prompt, timeout=self.llm_timeout,
                                   host=self.ollama_host, think=self.llm_think,
                                   progress=progress),
                validate)
            if validated is not None:
                return validated, self._provenance("local", "local", attempts,
                                                   degraded=degraded)
            if status == "unmapped":
                return None, self._provenance(
                    None, "local", attempts, degraded=degraded,
                    unmapped=self._unmapped_items(attempts))

        # 不成立 — fail-closed の最終防衛は呼び出し側（着手確認を出さず人へ・§8.10f）。
        # ここへ来るのはローカル試行の後なので、最終試行のバックエンドは常に local
        return None, self._provenance(None, "local", attempts, degraded=degraded)

    def _attempt(self, model_name: str, attempts: list[dict], run, validate):
        """1 回の契約化試行。(検証済み契約 | None, 状態) を返す（attempts へ記録）。

        状態: "ok" / "exec_error"（呼び出し自体の失敗 = 縮退判定の材料）/
              "invalid"（検証不合格）/ "unmapped"（スキーマ閉包検出・リトライしない）。
        """
        try:
            stdout = run()
            try:
                parsed = json.loads(extract_json(stdout))
            except json.JSONDecodeError:
                # 不正エスケープの機械修復（sa-ru _invoke_llm と同じ規律・2026-08-24 実測）
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
        except json.JSONDecodeError as e:
            # パース不能はモデルが応答した上での失敗 = 検証不合格と同列（縮退させない）
            logger.warning("契約化の出力がパース不能（%s）: %s", model_name, e)
            attempts.append({"model": model_name, "problems": [f"パース不能: {e}"]})
            return None, "invalid"
        except (OllamaTimeoutError, OllamaConnectionError, RuntimeError, OSError) as e:
            logger.warning("契約化の実行失敗（%s）: %s", model_name, e)
            attempts.append({"model": model_name, "problems": [f"実行失敗: {e}"]})
            return None, "exec_error"
        validated, problems = validate(parsed)
        if validated is not None:
            attempts.append({"model": model_name, "problems": []})
            return validated, "ok"
        logger.warning("契約化の出力が逸脱（%s）: %s", model_name, problems)
        attempts.append({"model": model_name, "problems": problems})
        return None, ("unmapped" if _is_unmapped(problems) else "invalid")

    @staticmethod
    def _unmapped_items(attempts: list[dict]) -> list[str]:
        """attempts から unmapped の逐語引用列を取り出す（人への確認文の材料）。"""
        for a in reversed(attempts):
            for p in a.get("problems") or []:
                if p.startswith("unmapped:"):
                    return [s.strip() for s in p[len("unmapped:"):].split(" / ") if s.strip()]
        return []

    def _provenance(self, origin, backend: str, attempts: list[dict],
                    degraded: bool = False, unmapped: list[str] | None = None) -> dict:
        """来歴を組み立て、試行列を判定ログ（§8.4.1）へ記録する（換装判断の実データ）。"""
        provenance = {"origin": origin, "backend": backend, "degraded": degraded,
                      "attempts": attempts}
        if unmapped:
            provenance["unmapped"] = unmapped
        # ログ書き込み失敗は契約化本体を壊さない（decompose の判定ログと同じ耐障害方針）
        try:
            self.logger.log_contract(origin, attempts, backend=backend,
                                     degraded=degraded)
        except Exception:
            pass
        return provenance
