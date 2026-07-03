"""タスク分解 — DeepSeek-R1 32B でユーザー指示をサブタスクに分解する。

構築手順書: docs/procedures/04-ai-gateway.md Step 4（タスク分解 + TaskDecomposer 実装）
関連: 設計書 §2.2 / §8.4 / §8.4.1（判定ログ → Phase 2）
"""

import json
import subprocess
from pathlib import Path

from ai_gateway.logger import YaTaLogger

PROMPTS_DIR = Path(__file__).parent / "prompts"


class TaskDecomposer:
    """ユーザー指示を依存関係付きサブタスク列に分解する（設計書 §2.2 / §8.4）。

    DeepSeek-R1 32B（ya-ta モデル）に「指示 → サブタスク（step / command / category /
    depends_on）」の分解をさせる。各サブタスクは light/heavy に分類され、信頼度の低い
    light は heavy に格上げされる。これが live の正規分類経路にあたる。
    """

    def __init__(self, config):
        """分解に使うモデル・登録済みモデル一覧・判定ロガーを用意する。"""
        # ya-ta.yaml の ya-ta.model を使用（現在: deepseek-r1:32b）
        self.model = config["ya-ta"]["model"]
        self.valid_models = set(config.get("models", {}).keys())
        # 判定ログ。decompose は live の正規分類経路であり、ここで記録しないと
        # 判定ログは production に 1 件も残らない（設計書 §8.4.1 / Phase 2 の基盤）。
        self.logger = YaTaLogger()

    def decompose(self, command: str) -> list[dict]:
        """ユーザー指示をサブタスクに分解する。
        フォールバック: JSONパースエラー時は元の指示を1件の heavy として返す。
        """
        # 分解プロンプトを組み立てる: 分解規則のテンプレートにカテゴリ定義を差し込む
        with open(PROMPTS_DIR / "categories.md") as f:
            categories = f.read()
        with open(PROMPTS_DIR / "decompose_task.md") as f:
            system_prompt = f.read().replace("{categories}", categories)

        # プロンプト＋ユーザー指示をローカル ollama（ya-ta モデル）に渡しサブタスク JSON を得る
        result = subprocess.run(
            ["ollama", "run", self.model],
            input=f"{system_prompt}\n\nユーザー指示: {command}",
            capture_output=True, text=True, timeout=60,
        )
        try:
            # サブタスクごとに生判定をログし、信頼度の低い light を heavy へ格上げする
            subtasks = json.loads(result.stdout)
            for s in subtasks:
                # 判定ログ: モデルの生判定（light→heavy 強制の前）をサブタスク単位で記録する。
                # 強制後ではなく生判定を残すのは、Phase 2 が「モデルがどう誤ったか」を学習対象に
                # するため（設計書 §8.4.1）。ログ書き込み失敗は分解本体を壊さない。
                try:
                    self.logger.log_decision(
                        task=s.get("command", ""),
                        category=s.get("category", ""),
                        model=s.get("model") or "",
                        reason=s.get("reason", ""),
                        confidence=s.get("confidence", 1.0),
                    )
                except Exception:
                    pass
                # confidence < 0.8 の light → heavy 強制（設計書 §2.2, §8.4）
                if s.get("category") == "light" and s.get("confidence", 1.0) < 0.8:
                    s["category"] = "heavy"
            return subtasks
        except (json.JSONDecodeError, KeyError):
            # フォールバック: 元の指示を1件の heavy として扱う（設計書 §8.4）
            return [{"step": 1, "command": command, "category": "heavy", "depends_on": []}]
