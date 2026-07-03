"""承認パイプライン本体 — 静的安全床 → Tier 分類 → ハンドラ実行 → 監査ログ。

フロー（設計書 §3.3 / §3.4）:
  (0) 静的安全床（決定論・Tier 判定前・最優先）— pipeline.yaml の safety リストと機械的に照合
  (1) スコープ判定 / (2) 三段階リスク判定 — ya-ta(LLM) が grey zone を判定

構築手順書: docs/procedures/08-approval-pipeline.md Step 6（パイプライン統合）
"""

import asyncio
import logging
import os
import re
import time

import yaml

from classifier import RiskClassifier
from tier1_handler import Tier1Handler
from tier2_handler import Tier2Handler
from tier3_handler import Tier3Handler
from audit_logger import AuditLogger

logger = logging.getLogger("sa-ru.approval")

# pipeline.yaml は本モジュールと同じツリーへ配備される（pyinfra files.sync、構築手順書 08）。
# 自モジュール相対で解決すれば実行時 cwd に依存せずロードできる（配備先＝
# /opt/taka-ma/sa-ru/approval-pipeline/config/pipeline.yaml）。
_PIPELINE_YAML = os.path.join(os.path.dirname(__file__), "config", "pipeline.yaml")


class ApprovalPipeline:
    """承認パイプライン本体。1 つの承認プロンプトを安全床 → Tier 分類 → ハンドラ実行で裁く。

    worker の y/n プロンプトを受け、(0) 決定論の静的安全床で機械照合し、漏れたものを
    (1)(2) ya-ta(LLM) の Tier 分類に回して Tier 1/2/3 ハンドラへ振り分ける。全判定は監査
    ログに残す（設計書 §3.3 / §3.4 / §3.5）。
    """

    def __init__(self, config: dict, slack_notifier=None, ssh_host: str = "mbp"):
        """分類器・各 Tier ハンドラ・安全床リスト・監査ロガーを組み立てる。

        Args:
            config: ya-ta（RiskClassifier）が参照するモデル設定等を含む dict。
            slack_notifier: Tier 3 の人間承認リクエストを Slack へ送る送信器。
            ssh_host: Tier 2 の qu-e 審査の SSH 接続先（qu-e は MBP 上の別プロセス・§8.8）。
        """
        # RiskClassifier は ya-ta を in-process 呼出（config から ya-ta モデルを参照）
        self.classifier = RiskClassifier(config)
        self.handlers = {
            1: Tier1Handler(),
            2: Tier2Handler(ssh_host=ssh_host),   # qu-e は SSH（§8.8）
            3: Tier3Handler(slack_notifier),
        }

        # pipeline.yaml（SSOT）をロードし、監査ログ出力先と安全床リストを取得する（設計 §3.3 (0)/§3.4）。
        pcfg = self._load_pipeline_config()
        audit_path = pcfg.get("audit", {}).get("log_path")
        # log_path が yaml にあればそれを SSOT として使い、無ければ AuditLogger の既定にフォールバック。
        self.logger = AuditLogger(log_path=audit_path) if audit_path else AuditLogger()
        safety = pcfg.get("safety", {})
        # 安全床（決定論の最終防壁）。LLM を介さずコードで照合する（§3.3 (0)）。
        self.always_deny = safety.get("always_deny") or []
        self.always_escalate = safety.get("always_escalate_to_human") or []

    @staticmethod
    def _load_pipeline_config() -> dict:
        """承認パイプライン設定 pipeline.yaml を読み込む（SSOT）。

        配備済みファイルが無い/壊れている場合でも sa-ru（orchestrator）全体を落とさないよう
        例外を握り、空設定を返す（安全床は best-effort の追加防壁であり、Tier 判定は別途機能する）。
        欠落・破損は運用ログで気付けるよう error を出す。
        """
        try:
            with open(_PIPELINE_YAML) as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            # OSError 全般（FileNotFoundError / PermissionError / IsADirectoryError 等）を握る。
            # docstring の「sa-ru 全体を落とさない」を満たすため FileNotFoundError 限定にしない。
            logger.error(
                "pipeline.yaml をロードできません（安全床リスト無効・監査ログは既定パス）: %s",
                _PIPELINE_YAML,
            )
            return {}

    async def process(self, prompt: 'InterceptedPrompt', pty_wrapper, instance_id: str,
                      *, team_id: str | None = None, channel: str | None = None,
                      task_id: str = ""):
        """1 件の承認プロンプトを裁定し、結果 dict（action / handler / reason）を返す。

        裁定順は固定: まず決定論の安全床（always_deny → always_escalate）を最優先で照合し、
        漏れたものだけ ya-ta の Tier 分類に回す。Tier 2 が deny を返したら Tier 3 へ繋ぐ。
        いずれの経路でも最後に監査ログを残す。

        Args:
            prompt: 検出された承認プロンプト（command と前後文脈 context を持つ）。
            pty_wrapper: 裁定結果を worker の PTY に y/n として送る相手。
            instance_id: 判定元の worker インスタンス識別子（監査・承認リクエスト用）。
            team_id / channel: Tier 3 承認リクエストを送信元ワークスペースへ返すための宛先（§8.10）。
            task_id: 紐付くタスク ID（承認リクエストの突き合わせ用）。
        """
        start = time.monotonic()
        command = prompt.command

        # (0) 静的安全床（決定論・Tier 判定前・最優先、§3.3 (0) / §3.4 の SF ノード）。
        #     LLM 判定の前段で機械的に照合する。ya-ta(LLM) が誤った/乗っ取られた場合でも
        #     破壊的操作を通さない最終防壁のため、意図的に LLM を介さない。
        deny_rule = self._match_rule(command, self.always_deny)
        if deny_rule:
            pty_wrapper.deny()
            result = {"action": "denied", "reason": f"always_deny: {deny_rule}",
                      "handler": "safety_deny"}
            self._audit(result, instance_id, command, tier=0, start=start)
            return result

        escalate_rule = self._match_rule(command, self.always_escalate)
        if escalate_rule:
            # スコープ・Tier 判定をスキップして人間承認（Tier 3）へ直行する。
            ctx = {
                "instance_id": instance_id,
                "risk_reason": f"always_escalate: {escalate_rule}",
                "team_id": team_id,
                "channel": channel,
                "task_id": task_id,
            }
            result = await self.handlers[3].handle(prompt, pty_wrapper, ctx)
            # 安全床由来であることを監査ログに残す（always_deny と対称・§3.5 SSOT）。
            # Tier3Handler の戻りは approved/rejected で reason 空のため、ここで補う。
            result["reason"] = ctx["risk_reason"] + (
                f" ({result['reason']})" if result.get("reason") else "")
            self._audit(result, instance_id, command, tier=3, start=start)
            return result

        # (1)(2) リスク分類（tier ＋ reason）。reason は Tier 3 承認リクエストの risk_reason に使う。
        classification = await self.classifier.classify(prompt)
        # ya-ta(ollama) はプロンプト次第で "tier":"3" 等の文字列/実数を返しうる。handlers は int キーの
        # ため int 化し、欠落・不正・未知 tier はフォールセーフに Tier3（人間承認）へ寄せる（§8.4）。
        try:
            tier = int(classification["tier"])
        except (KeyError, TypeError, ValueError):
            tier = 3
        if tier not in self.handlers:
            tier = 3
        risk_reason = classification.get("reason", "")

        # Tier 3（人間承認）に必要なコンテキスト。Tier 1/2 は ctx を無視する。
        # team_id / channel は承認リクエストを送信元ワークスペースへ返すため（§8.10）。
        ctx = {
            "instance_id": instance_id,
            "risk_reason": risk_reason,
            "team_id": team_id,
            "channel": channel,
            "task_id": task_id,
        }

        # 対応するハンドラで処理
        handler = self.handlers[tier]
        result = await handler.handle(prompt, pty_wrapper, ctx)

        # Tier 2 deny → Tier 3 エスカレート（qu-e の判定理由を risk_reason に引き継ぐ）
        if result.get("action") == "escalate":
            tier = 3
            ctx["risk_reason"] = result.get("reason", "") or risk_reason
            handler = self.handlers[3]
            result = await handler.handle(prompt, pty_wrapper, ctx)

        self._audit(result, instance_id, command, tier=tier, start=start)
        return result

    def _audit(self, result: dict, instance_id: str, command: str, *, tier: int, start: float):
        """1 回の判定結果を監査ログ（jsonl）へ 1 行追記する（§3.5）。"""
        duration_ms = int((time.monotonic() - start) * 1000)
        self.logger.log({
            "instance_id": instance_id,
            "command": command,
            "tier": tier,
            "handler": result["handler"],
            "decision": result["action"],
            "reason": result.get("reason", ""),
            "duration_ms": duration_ms,
        })

    @staticmethod
    def _match_rule(command: str, rules: list[str]) -> str | None:
        """command が safety リストの規則に一致すれば、その規則を返す（無ければ None）。

        素朴な部分一致は正規コマンドを誤拒否する（`rm -rf /` が `rm -rf /tmp/build` に、
        `sudo` が `cat /etc/sudoers` に一致してしまう）。規則の前後が単語/パス構成文字
        （`\\w` ・ `/` ・ `-`）でない位置のみ一致とみなす語境界照合にする。
        例: `rm -rf /` は `rm -rf /`（末尾）には一致し `rm -rf /tmp...` には一致しない。
        """
        for rule in rules:
            if rule and re.search(rf"(?<![\w/-]){re.escape(rule)}(?![\w/-])", command):
                return rule
        return None
