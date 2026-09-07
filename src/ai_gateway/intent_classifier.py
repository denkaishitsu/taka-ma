"""意図判定 — 発話を chat / probe_repo / probe_task / execute の 4 値に分類する（設計書 §8.4「意図判定の呼び出し」）。

旧来この判定は sa-ru の会話脳（速度優先選定のアクティブ 3B）が返信生成と同時に行っていたが、
実行依頼を状態確認と誤判定して成果物に到達しない事故が反復した（2026-09-07 実測。#151 時点で
ready 判定の失敗率 11% を実測しながらパッチで済ませた反省）。判定と返信生成を分離し、判定を
検証可能な単能業務として ya-ta に置く。

構成（§8.4）:
- 一次: ローカル ya-ta.model（ollama HTTP）。二次: worker CLI（contractor.model と同じ opus
  経路・escalate_runner 注入）。中間段は置かない。
- 昇格条件はすべて機械判定: (1) スキーマ不合格は同段 1 回再試行→再不合格で次段
  (2) evidence の逐語照合不合格は即次段 (3) confidence < routing.confidence_threshold は即次段
  (4) 二段目の前に preflight.check_ssh を通し、不達なら昇格せず fail-closed。
- fail-closed の向きは常に action="chat"（会話継続 = 人への確認質問）。誤って実行・probe へ
  進む事故より、誤って質問する事故が常に安い。
- 毎判定を判定ログ（§8.4.1）へ kind=intent で記録。一次と二次が食い違えば二次を採用し両方記録
  （一次モデルの誤り率の実測 = 換装判断の材料）。
"""

import json
import logging
from pathlib import Path

from ai_gateway.llm import (
    OllamaConnectionError,
    OllamaTimeoutError,
    extract_json,
    repair_json_escapes,
    run_ollama,
)
from ai_gateway.logger import YaTaLogger

logger = logging.getLogger("ya-ta.intent")

PROMPTS_DIR = Path(__file__).parent / "prompts"

ACTIONS = ("chat", "probe_repo", "probe_task", "execute")

# スキーマ不合格の同段再試行回数（§8.4。契約化の ATTEMPTS と同じ値・同じ意味）
ATTEMPTS = 2

# fail-closed の最終判定（§8.4「fail-closed の向き」）。呼び出し側はこの action を受けたら
# 会話継続（確認質問）へ倒す
FAIL_CLOSED = {"action": "chat", "confidence": 0.0, "evidence": None,
               "origin": None, "escalated": False, "fail_closed": True}


def _validate_schema(parsed) -> list[str]:
    """スキーマ検証（昇格条件 1）。逸脱理由のリストを返す（空 = 合格）。"""
    problems = []
    if not isinstance(parsed, dict):
        return ["JSON がオブジェクトでない"]
    if parsed.get("action") not in ACTIONS:
        problems.append(f"action が 4 値以外: {parsed.get('action')!r}")
    conf = parsed.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) or not (0 <= conf <= 1):
        problems.append(f"confidence が 0〜1 の数値でない: {conf!r}")
    if not isinstance(parsed.get("evidence"), str) or not parsed["evidence"].strip():
        problems.append("evidence が空または文字列でない")
    return problems


class IntentClassifier:
    """発話の意図を 4 値判定する（§8.4）。判定のみを行い、返信文は生成しない。"""

    def __init__(self, config, escalate_runner=None, preflight=None):
        """設定・CLI 実行手段・到達性検査を注入する。

        Args:
            config: sa-ru / ya-ta マージ済み設定。一次モデルは ya-ta.yaml の model、
                閾値は routing.confidence_threshold、二次モデルは contractor.model を共用
                （意図判定専用の新キーを増やさない）。
            escalate_runner: worker CLI 実行手段（(model_key, prompt) -> 出力テキスト。
                失敗は例外）。sa-ru が契約化と同じものを注入する。None なら昇格不能 =
                一次不合格は fail-closed。
            preflight: AuthPreflight（check_ssh を持つ）。二段目の前の到達性ゲート
                （§8.4・キャッシュ共用）。None なら到達性を見ずに昇格を試みる
                （単体テスト用。実運用では必ず注入する）。
        """
        ya = config["ya-ta"]
        self.model = ya["model"]
        self.ollama_host = config["sa-ru"]["ollama_host"]
        self.llm_timeout = ya["llm_timeout_sec"]
        self.llm_think = ya.get("llm_think")
        self.threshold = config["routing"]["confidence_threshold"]
        self.cli_model = (ya.get("contractor") or {}).get("model", "opus")
        self.escalate_runner = escalate_runner
        self.preflight = preflight
        self.logger = YaTaLogger()
        self._template = (PROMPTS_DIR / "intent.md").read_text()

    # ── 公開 API ──

    def classify(self, history_text: str, latest_text: str) -> dict:
        """最新発話の意図を判定する。

        Returns:
            {"action", "confidence", "evidence", "origin"(モデル名|None),
             "escalated"(bool), "fail_closed"(bool)}。失敗しても例外は投げず、
            最終防衛は FAIL_CLOSED（action=chat）を返す。
        """
        prompt = (self._template
                  + f"\n\n### 会話履歴\n{history_text}\n\n### 最新の発話\n{latest_text}\n")
        first, first_reason = self._attempt_stage(
            lambda: run_ollama(self.model, prompt, timeout=self.llm_timeout,
                               host=self.ollama_host, think=self.llm_think),
            self.model, latest_text)
        if first is not None and first_reason is None:
            result = {**first, "origin": self.model, "escalated": False,
                      "fail_closed": False}
            self._log(result, first=first, escalate_reason=None)
            return result

        # ── 昇格（§8.4 昇格条件 4: 到達性ゲートを先に通す） ──
        if self.escalate_runner is None:
            logger.warning("意図判定: 一次不合格（%s）だが昇格手段なし → fail-closed(chat)",
                           first_reason)
            result = dict(FAIL_CLOSED)
            self._log(result, first=first, escalate_reason=first_reason)
            return result
        if not self._cli_reachable():
            logger.warning("意図判定: 一次不合格（%s）だが worker CLI 到達不能 → "
                           "fail-closed(chat)", first_reason)
            result = dict(FAIL_CLOSED)
            self._log(result, first=first, escalate_reason=f"{first_reason}; 到達不能")
            return result

        second, second_reason = self._attempt_stage(
            lambda: self.escalate_runner(self.cli_model, prompt),
            self.cli_model, latest_text, final_stage=True)
        if second is not None and second_reason is None:
            result = {**second, "origin": self.cli_model, "escalated": True,
                      "fail_closed": False}
            self._log(result, first=first, escalate_reason=first_reason)
            return result

        logger.warning("意図判定: 全段不合格（一次: %s / 二次: %s）→ fail-closed(chat)",
                       first_reason, second_reason)
        result = dict(FAIL_CLOSED)
        self._log(result, first=first,
                  escalate_reason=f"{first_reason}; 二次: {second_reason}")
        return result

    # ── 段の実行 ──

    def _attempt_stage(self, run, model_name: str, latest_text: str,
                       final_stage: bool = False):
        """1 段ぶんの試行。(判定 dict | None, 不合格理由 | None) を返す。

        スキーマ不合格のみ同段 1 回再試行（§8.4 昇格条件 1）。逐語照合不合格・
        低 confidence は再試行しない（同じモデルに同じ入力で正答は期待できない）。
        final_stage の低 confidence も不合格（次段が無いため呼び出し側で fail-closed）。
        """
        last_reason = None
        for attempt in range(ATTEMPTS):
            try:
                stdout = run()
                try:
                    parsed = json.loads(extract_json(stdout))
                except json.JSONDecodeError:
                    parsed = json.loads(extract_json(repair_json_escapes(stdout)))
            except (json.JSONDecodeError,) as e:
                last_reason = f"パース不能({model_name}): {e}"
                logger.warning("意図判定: %s", last_reason)
                continue                                  # スキーマ系 → 同段再試行
            except (OllamaTimeoutError, OllamaConnectionError, RuntimeError, OSError) as e:
                return None, f"実行失敗({model_name}): {e}"  # 呼び出し自体の失敗 → 即次段
            problems = _validate_schema(parsed)
            if problems:
                last_reason = f"スキーマ不合格({model_name}): {'; '.join(problems)}"
                logger.warning("意図判定: %s", last_reason)
                continue                                  # 同段再試行（1 回だけ）
            # 昇格条件 2: evidence の逐語照合（出所束縛 §8.10f と同一原理）
            if parsed["evidence"] not in latest_text:
                return None, (f"evidence 逐語照合不合格({model_name}): "
                              f"{parsed['evidence'][:80]!r} が発話に存在しない")
            # 昇格条件 3: confidence 閾値
            if parsed["confidence"] < self.threshold:
                return ({k: parsed[k] for k in ("action", "confidence", "evidence")},
                        f"低confidence({model_name}): {parsed['confidence']}")
            return {k: parsed[k] for k in ("action", "confidence", "evidence")}, None
        return None, last_reason or f"スキーマ不合格が {ATTEMPTS} 回継続({model_name})"

    def _cli_reachable(self) -> bool:
        """二段目の前の到達性ゲート（§8.4 昇格条件 4・check_ssh のキャッシュ共用）。"""
        if self.preflight is None:
            return True     # 単体テスト・段階導入: 到達性検査なしで昇格を試みる
        try:
            self.preflight.check_ssh()
            return True
        except Exception:
            return False

    # ── 記録（§8.4.1 判定ログ・kind=intent） ──

    def _log(self, result: dict, first: dict | None, escalate_reason: str | None):
        """毎判定を記録する。一次と二次の食い違いは両方残す（換装判断の実測材料）。

        ログ書き込み失敗は判定本体を壊さない（契約化・分解と同じ耐障害方針）。
        """
        try:
            mismatch = bool(result.get("escalated") and first
                            and first.get("action") != result.get("action"))
            self.logger.log_intent(
                action=result.get("action"), confidence=result.get("confidence"),
                origin=result.get("origin"), escalated=bool(result.get("escalated")),
                fail_closed=bool(result.get("fail_closed")),
                first_action=(first or {}).get("action"),
                mismatch=mismatch, escalate_reason=escalate_reason)
        except Exception:
            pass
