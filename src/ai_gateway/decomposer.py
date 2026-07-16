"""タスク分解 — DeepSeek-R1 32B でユーザー指示をサブタスクに分解する。

構築手順書: docs/procedures/04-ai-gateway.md Step 4（タスク分解 + TaskDecomposer 実装）
関連: 設計書 §2.2 / §8.4 / §8.4.1（判定ログ → Phase 2）
"""

import json
from pathlib import Path

from ai_gateway.llm import GenerationProgress, extract_json, run_ollama
from ai_gateway.logger import YaTaLogger

PROMPTS_DIR = Path(__file__).parent / "prompts"


class TaskDecomposer:
    """ユーザー指示を依存関係付きサブタスク列に分解する（設計書 §2.2 / §8.4）。

    DeepSeek-R1 32B（ya-ta モデル）に「指示 → サブタスク（step / command / category /
    depends_on）」の分解をさせる。各サブタスクは light/heavy に分類され、信頼度の低い
    light は heavy に格上げされる。これが live の正規分類経路にあたる。
    """

    def __init__(self, config):
        """分解に使うモデル・接続先 ollama・登録済みモデル一覧・判定ロガーを用意する。"""
        # ya-ta.yaml の ya-ta.model を使用（モデル名の SSOT は ya-ta.yaml）
        self.model = config["ya-ta"]["model"]
        # 接続先はマージ済み config の sa-ru.ollama_host を唯一の源にする（設計書 §8.4）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        # タイムアウトは ya-ta.yaml を唯一の供給元とする（設計書 §8.4。コード側に既定値を
        # 置くと供給元が二重になるため必須アクセス。欠落時は KeyError で即落とす）
        self.llm_timeout = config["ya-ta"]["llm_timeout_sec"]
        # 思考の有効/無効（None=モデル既定。qwen3.6 は思考が分解 281 秒の支配項・§8.4）
        self.llm_think = config["ya-ta"].get("llm_think")
        self.valid_models = set(config.get("models", {}).keys())
        # 判定ログ。decompose は live の正規分類経路であり、ここで記録しないと
        # 判定ログは production に 1 件も残らない（設計書 §8.4.1 / Phase 2 の基盤）。
        self.logger = YaTaLogger()

    def decompose(self, command: str,
                  progress: GenerationProgress | None = None) -> list[dict]:
        """ユーザー指示をサブタスクに分解する。
        フォールバック: JSONパースエラー時は元の指示を1件の heavy として返す。
        progress はハートビート進捗通知（§10.8）へ生成トークン数を届ける共有ホルダー。
        """
        # 分解プロンプトを組み立てる: 分解規則のテンプレートにカテゴリ定義を差し込む
        with open(PROMPTS_DIR / "categories.md") as f:
            categories = f.read()
        with open(PROMPTS_DIR / "decompose_task.md") as f:
            system_prompt = f.read().replace("{categories}", categories)

        try:
            # プロンプト＋ユーザー指示をローカル ollama（ya-ta モデル）に渡しサブタスク JSON を得る。
            # ollama 実行失敗は run_ollama が RuntimeError を送出し、下の except で
            # 安全側フォールバックへ落ちる（設計書 §8.4「ollama 実行失敗の検知」）。
            stdout = run_ollama(
                self.model,
                f"{system_prompt}\n\nユーザー指示: {command}",
                timeout=self.llm_timeout,
                host=self.ollama_host,
                think=self.llm_think,
                progress=progress,
            )
            subtasks = json.loads(extract_json(stdout))
            # 構造検証: 「サブタスクの配列」でなければフォールバックへ（設計書 §8.4）
            if not isinstance(subtasks, list) or not subtasks:
                raise ValueError("分解出力がサブタスク配列でない")
            # サブタスクごとに生判定をログし、信頼度の低い light を heavy へ格上げする
            for i, s in enumerate(subtasks, start=1):
                # 各要素は最低限 command / category を持つこと（欠く場合はフォールバック）
                if not isinstance(s, dict) or "command" not in s or "category" not in s:
                    raise ValueError("サブタスクに command/category が無い")
                # step 欠落は配列順の連番で補完する。下流の依存解決（_execute_chain）が
                # step を前提とするため、欠落を放置すると当該サブタスクが無音でロストする。
                if s.get("step") is None:
                    s["step"] = i
                # depends_on 欠落は空リスト（依存なし）に正規化する
                if s.get("depends_on") is None:
                    s["depends_on"] = []
                # confidence 欠損/null は既定 1.0 に正規化する。null のまま閾値比較すると
                # TypeError で分解全体が落ちるため（設計書 §8.4「confidence 欠損値の正規化」）。
                confidence = s.get("confidence")
                if confidence is None:
                    confidence = 1.0
                # 判定ログ: モデルの生判定（light→heavy 強制の前）をサブタスク単位で記録する。
                # 強制後ではなく生判定を残すのは、Phase 2 が「モデルがどう誤ったか」を学習対象に
                # するため（設計書 §8.4.1）。ログ書き込み失敗は分解本体を壊さない。
                try:
                    self.logger.log_decision(
                        task=s.get("command", ""),
                        category=s.get("category", ""),
                        model=s.get("model") or "",
                        reason=s.get("reason", ""),
                        confidence=confidence,
                    )
                except Exception:
                    pass
                # confidence < 0.8 の light → heavy 強制（設計書 §2.2, §8.4）
                if s.get("category") == "light" and confidence < 0.8:
                    s["category"] = "heavy"
            return subtasks
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, RuntimeError):
            # フォールバック: パースエラー・構造不正・ollama 実行失敗のいずれも、
            # 元の指示を1件の heavy として扱う（設計書 §8.4）
            return [{"step": 1, "command": command, "category": "heavy", "depends_on": []}]
