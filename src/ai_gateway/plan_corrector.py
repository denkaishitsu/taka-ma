"""計画訂正の自然言語解釈 — ya-ta（ローカル・安価）で発話を構造化パッチへ変換する。

設計書 §10.2.1「訂正の入力経路」。簡易記法（決定的パース・orchestrator.plan）で解釈できな
かった発話だけがここへ来る。音声入力時はこちらが主経路になる（番号を言わず「コミットのやつ」
のように overview の文言で対象を指すため）。出力は簡易記法と同一の構造化パッチで、適用は
orchestrator.plan.apply_patches に一本化する（パーサを二重に持たない）。
"""

import json
import logging
from pathlib import Path

from ai_gateway.llm import extract_json, run_ollama

logger = logging.getLogger("ya-ta.plan_corrector")

PROMPTS_DIR = Path(__file__).parent / "prompts"


class PlanCorrector:
    """現行プラン + 発話 → 構造化パッチ（`[{"steps": [...], "model": ..., "depth": ...}]`）。"""

    def __init__(self, config):
        """分解と同じ ya-ta モデル・接続先・タイムアウトを使う（設定の供給元は ya-ta.yaml）。"""
        self.model = config["ya-ta"]["model"]
        self.ollama_host = config["sa-ru"]["ollama_host"]
        # 訂正解釈は会話の応答を塞ぐ位置に在る（計画確認中の発話は訂正解釈 → 通常会話の順）ため、
        # 分解の llm_timeout_sec ではなく専用の短いタイムアウトで打ち切る。ya-ta が詰まっても
        # 会話が 300 秒無応答にならない。供給元は ya-ta.yaml のみ（コード既定値なし）
        self.llm_timeout = config["ya-ta"]["correction_timeout_sec"]
        self.llm_think = config["ya-ta"].get("llm_think")
        self.valid_models = list(config.get("models", {}).keys())
        self._prompt_template = (PROMPTS_DIR / "correct_plan.md").read_text()

    def correct(self, subtasks: list[dict], text: str, progress=None) -> list[dict]:
        """発話を訂正パッチへ変換する。訂正でない・解釈不能なら空リストを返す。

        空リストは呼び出し側で「訂正ではない」＝通常の会話処理へ落とす合図になるため、
        失敗（タイムアウト・接続断・パース不能）も空リストに倒す（安全側: 解釈できない
        発話でユーザーの計画を勝手に書き換えない）。
        """
        plan_json = json.dumps(
            [{"step": s["step"], "overview": s.get("command", ""),
              "execution": s.get("execution", "agent"), "depth": s.get("depth"),
              "model": s.get("model_override") or s.get("model")}
             for s in subtasks], ensure_ascii=False)
        prompt = (self._prompt_template.replace("{models}", ", ".join(self.valid_models))
                  + f"\n\n## 現行プラン\n```\n{plan_json}\n```\n\n## ユーザー発話\n{text}\n")
        try:
            stdout = run_ollama(self.model, prompt, timeout=self.llm_timeout,
                                host=self.ollama_host, think=self.llm_think,
                                progress=progress)
            parsed = json.loads(extract_json(stdout))
            patches = parsed.get("patches") if isinstance(parsed, dict) else None
            if not isinstance(patches, list):
                return []
            # 形が違う要素（steps 欠落・型不正）は捨てる。適用側（apply_patches）は値の
            # 妥当性（未登録モデル・未知 step）を見るが、構造は入口で揃えておく
            valid = []
            for p in patches:
                if not isinstance(p, dict):
                    continue
                steps = p.get("steps")
                if steps != "all" and not (isinstance(steps, list)
                                           and all(isinstance(n, int) for n in steps)):
                    continue
                if "model" not in p and "depth" not in p:
                    continue
                valid.append(p)
            return valid
        except Exception:
            # 訂正解釈の失敗で会話全体を落とさない（呼び出し側は通常会話へ落とす）
            logger.exception("計画訂正の解釈に失敗（訂正なしとして継続）")
            return []
