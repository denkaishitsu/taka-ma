"""リスク分類器 — ya-ta（ai_gateway）に Tier 判定を依頼する。

構築手順書: docs/procedures/08-approval-pipeline.md Step 3（分類器 — ya-ta 連携）

通信方式: ya-ta はライブラリ方式（設計書 §1.3 / プロジェクト方針）。同一プロセス内の
ai_gateway.RiskClassifier を in-process で呼び出す（同期処理は to_thread で実行）。
"""

import asyncio

from ai_gateway.risk_classifier import RiskClassifier as AiGatewayRiskClassifier

from approval_types import operation_str


class RiskClassifier:
    """ya-ta にリスク分類を依頼する（in-process）。"""

    def __init__(self, config):
        """ya-ta（ai_gateway）のリスク分類器を内部に抱えて初期化する。"""
        self._gateway = AiGatewayRiskClassifier(config)

    async def classify(self, pending: 'PendingApproval') -> dict:
        """ya-ta にリスク分類を依頼。

        Returns: `{"tier": 1|2|3, "reason": str, ...}`。Tier 3（人間承認）では reason を
        Slack 承認リクエストの risk_reason として提示するため、tier だけでなく dict 全体を返す。
        """
        return await self._call_ai_gateway(pending)

    async def _call_ai_gateway(self, pending: 'PendingApproval') -> dict:
        """ai_gateway.RiskClassifier.classify を in-process 実行（同期 → to_thread）。

        構造化 PendingApproval を操作文字列に整形して ya-ta へ渡す（ya-ta の I/F は文字列のまま）。
        ai_gateway 側はパースエラー時に Tier 3（人間判断）へフォールバックする（設計書 §8.4）。
        """
        return await asyncio.to_thread(self._gateway.classify, operation_str(pending))
