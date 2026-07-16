"""タスク分類 — light/heavy 2値判定 + :モデル名 抽出。

構築手順書: docs/procedures/04-ai-gateway.md Step 5（タスク分類プロンプト + TaskClassifier 実装）
関連: 設計書 §2.2 / §8.4（meta 廃止、light/heavy 2 値）
"""

import json
import re
from pathlib import Path

from ai_gateway.llm import extract_json, run_ollama
from ai_gateway.logger import YaTaLogger

PROMPTS_DIR = Path(__file__).parent / "prompts"


class InvalidModelError(Exception):
    """未登録の :モデル名 が指定された場合に送出する。

    ユーザーが `:gpt5` のような登録外モデルを明示指定したケース。曖昧に無視せず
    エラーにして、利用可能なモデル一覧を添えて呼び出し側に返す。
    """
    pass


class TaskClassifier:
    """タスク指示を light/heavy の 2 値に分類し、明示指定モデルを抽出する（設計書 §2.2 / §8.4）。

    meta は廃止され分類は light/heavy の 2 値のみ。ya-ta モデルに判定させた上で、
    信頼度の低い light は heavy に格上げして取りこぼしを防ぐ。
    """

    def __init__(self, config):
        """設定から判定用モデルと登録済みモデル一覧を取り出し、判定ロガーを用意する。"""
        self.ai_gateway_model = config["ya-ta"]["model"]
        # 接続先はマージ済み config の sa-ru.ollama_host を唯一の源にする（設計書 §8.4）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        # タイムアウトは ya-ta.yaml を唯一の供給元とする（設計書 §8.4。コード側に既定値を
        # 置くと供給元が二重になるため必須アクセス。欠落時は KeyError で即落とす）
        self.llm_timeout = config["ya-ta"]["llm_timeout_sec"]
        # 思考の有効/無効（None=モデル既定・§8.4）
        self.llm_think = config["ya-ta"].get("llm_think")
        self.models = config.get("models", {})
        # :モデル名 指定の検証に使う登録済みモデル名の集合
        self.valid_models = set(self.models.keys())
        self.logger = YaTaLogger()  # 判定ログ（Phase 2 分類改善の基盤）

    def _build_capability_prompt(self) -> str:
        """ya-ta.yaml の models から capability 判定セクションを動的生成する。

        モデルを増減してもプロンプトを手で書き換えずに済むよう、各モデルの
        capability_description を設定から拾って分類プロンプトに差し込む形にしている。
        """
        # capability_description を持つモデルだけを「### 名前\n説明」の節として並べる
        lines = []
        for name, model_conf in self.models.items():
            desc = model_conf.get("capability_description", "")
            if desc:
                lines.append(f"### {name}\n{desc.strip()}")
        return "\n\n".join(lines) if lines else "(特殊モデルなし)"

    def parse_model(self, command: str) -> tuple[str, list[str]]:
        """コマンドから :モデル名 を抽出し、検証する。
        Returns: (モデル指定を除去したコマンド, 検証済みモデル名のリスト)
        Raises: InvalidModelError（未登録のモデル名）
        """
        # `:xxx` 形式の明示モデル指定をすべて拾う。無ければそのまま返す
        matches = re.findall(r':(\S+)', command)
        if not matches:
            return command, []

        # 指示文本体には :モデル名 を残さないよう除去（後段はクリーンなコマンドを扱う）
        clean_command = re.sub(r'\s*:\S+', '', command).strip()
        # 指定された各モデルが登録済みかを検証。1 つでも未登録なら一覧を添えて弾く
        validated = []
        for m in matches:
            if m not in self.valid_models:
                available = ", ".join(f":{k}" for k in sorted(self.valid_models))
                raise InvalidModelError(
                    f"':{m}' は登録されていません。利用可能: {available}"
                )
            validated.append(m)
        return clean_command, validated

    def classify(self, command: str) -> dict:
        """タスクの難易度を判定する。
        フォールバック: パースエラー時は heavy を返す（設計書 §8.4）。
        """
        # 分類プロンプトを組み立てる: カテゴリ定義と、登録モデルから生成した能力一覧を差し込む
        with open(PROMPTS_DIR / "categories.md") as f:
            categories = f.read()
        with open(PROMPTS_DIR / "classify_task.md") as f:
            template = f.read()

        system_prompt = template.replace(
            "{categories}", categories
        ).replace(
            "{capabilities_from_ai_gateway_yaml}",
            self._build_capability_prompt()
        )

        try:
            # プロンプト＋タスクをローカル ollama（ya-ta モデル）に渡して判定 JSON を得る。
            # ollama 実行失敗は run_ollama が RuntimeError を送出し、下の except で
            # 安全側フォールバック（heavy）へ落ちる（設計書 §8.4「ollama 実行失敗の検知」）。
            stdout = run_ollama(
                self.ai_gateway_model,
                f"{system_prompt}\n\nタスク: {command}",
                timeout=self.llm_timeout,
                host=self.ollama_host,
                think=self.llm_think,
            )
            # category 欠落は判定不成立とみなしフォールバックへ落とす
            parsed = json.loads(extract_json(stdout))
            if "category" not in parsed:
                raise KeyError("category missing")
            # confidence 欠損/null は既定 1.0 に正規化する。null のまま閾値比較すると
            # TypeError で分類が落ちるため（設計書 §8.4「confidence 欠損値の正規化」）。
            confidence = parsed.get("confidence")
            if confidence is None:
                confidence = 1.0
            # 判定ログ: モデルの生判定（light→heavy 強制の前）を記録。
            # 判定ログは Phase 2（誤判定パターン抽出→分類プロンプト改善）の基盤。
            # ログ書き込み失敗は分類本体を壊さない。
            try:
                self.logger.log_decision(
                    task=command,
                    category=parsed.get("category", ""),
                    model=parsed.get("model") or "",
                    reason=parsed.get("reason", ""),
                    confidence=confidence,
                )
            except Exception:
                pass
            # confidence < 0.8 の light → heavy 強制
            if parsed.get("category") == "light" and confidence < 0.8:
                parsed["category"] = "heavy"
            return parsed
        except (json.JSONDecodeError, KeyError, TypeError, RuntimeError):
            # 判定不能・ollama 実行失敗なら安全側に倒して heavy 扱い
            # （軽く見て取りこぼすより重く回す）（設計書 §8.4）
            return {"category": "heavy", "model": None,
                    "reason": "parse error - default to heavy", "confidence": 0.0}
