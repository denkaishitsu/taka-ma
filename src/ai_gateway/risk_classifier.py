"""リスク分類 — Tier 1/2/3 判定（不可逆性ベース）。

構築手順書: docs/procedures/04-ai-gateway.md Step 6（リスク分類プロンプト + RiskClassifier 実装）
関連: 設計書 §3.3〜§3.4（タスク指示スコープ判定 + Tier 判定）
"""

import json
from pathlib import Path

from ai_gateway.llm import extract_json, run_ollama

PROMPTS_DIR = Path(__file__).parent / "prompts"


class RiskClassifier:
    """操作の不可逆性から Tier 1/2/3 を判定する（設計書 §3.3〜§3.4）。

    Tier は「失敗したとき元に戻せるか」を軸にした危険度で、後段の承認フロー
    （自動 / 自動だが記録 / 人間承認）への振り分けに使う。判定は ya-ta モデルに委ねる。
    """

    def __init__(self, config):
        """ya-ta.yaml の ya-ta.model（判定に使うローカルモデル）と接続先 ollama を取り出す。"""
        self.model = config["ya-ta"]["model"]
        # 接続先はマージ済み config の sa-ru.ollama_host を唯一の源にする（設計書 §8.4）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        # タイムアウトは ya-ta.yaml を唯一の供給元とする（設計書 §8.4。コード側に既定値を
        # 置くと供給元が二重になるため必須アクセス。欠落時は KeyError で即落とす）
        self.llm_timeout = config["ya-ta"]["llm_timeout_sec"]
        # 思考の有効/無効（None=モデル既定・§8.4）
        self.llm_think = config["ya-ta"].get("llm_think")

    def classify(self, operation: str) -> dict:
        """操作のリスクレベルを判定する。
        フォールバック: パースエラー時は Tier 3（人間判断）を返す（設計書 §8.4）。
        """
        # リスク判定プロンプト（Tier の定義と判定基準）を読み込む
        with open(PROMPTS_DIR / "classify_risk.md") as f:
            system_prompt = f.read()

        try:
            # プロンプト＋対象操作を ya-ta モデルに渡し、JSON 形式の判定を得る。
            # ollama 実行失敗は run_ollama が RuntimeError を送出し、下の except で
            # 安全側フォールバック（Tier 3）へ落ちる（設計書 §8.4「ollama 実行失敗の検知」）。
            stdout = run_ollama(
                self.model,
                f"{system_prompt}\n\n操作: {operation}",
                timeout=self.llm_timeout,
                host=self.ollama_host,
                think=self.llm_think,
            )
            # tier 欠落は判定不成立とみなし、フォールバックへ落とす
            parsed = json.loads(extract_json(stdout))
            if "tier" not in parsed:
                raise KeyError("tier missing")
            return parsed
        except (json.JSONDecodeError, KeyError, RuntimeError):
            # 判定不能・ollama 実行失敗なら安全側に倒し、Tier 3（人間承認）へ回す（設計書 §8.4）
            return {"tier": 3, "reason": "parse error — default to human approval", "action": "route_to_human"}
