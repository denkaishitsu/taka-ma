"""リスク分類 — Tier 1/2/3 判定（不可逆性ベース）。

構築手順書: docs/procedures/04-ai-gateway.md Step 6（リスク分類プロンプト + RiskClassifier 実装）
関連: 設計書 §3.3〜§3.4（タスク指示スコープ判定 + Tier 判定）
"""

import json
import subprocess
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


class RiskClassifier:
    """操作の不可逆性から Tier 1/2/3 を判定する（設計書 §3.3〜§3.4）。

    Tier は「失敗したとき元に戻せるか」を軸にした危険度で、後段の承認フロー
    （自動 / 自動だが記録 / 人間承認）への振り分けに使う。判定は ya-ta モデルに委ねる。
    """

    def __init__(self, config):
        """ya-ta.yaml の ya-ta.model（判定に使うローカルモデル）を取り出す。"""
        self.model = config["ya-ta"]["model"]

    def classify(self, operation: str) -> dict:
        """操作のリスクレベルを判定する。
        フォールバック: パースエラー時は Tier 3（人間判断）を返す（設計書 §8.4）。
        """
        # リスク判定プロンプト（Tier の定義と判定基準）を読み込む
        with open(PROMPTS_DIR / "classify_risk.md") as f:
            system_prompt = f.read()

        # プロンプト＋対象操作を ya-ta モデルに渡し、JSON 形式の判定を得る
        result = subprocess.run(
            ["ollama", "run", self.model],
            input=f"{system_prompt}\n\n操作: {operation}",
            capture_output=True, text=True, timeout=60,
        )
        try:
            # tier 欠落は判定不成立とみなし、フォールバックへ落とす
            parsed = json.loads(result.stdout)
            if "tier" not in parsed:
                raise KeyError("tier missing")
            return parsed
        except (json.JSONDecodeError, KeyError):
            # 判定不能なら安全側に倒し、Tier 3（人間承認）へ回す（設計書 §8.4）
            return {"tier": 3, "reason": "parse error — default to human approval", "action": "route_to_human"}
