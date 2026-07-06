"""Tier 1 ハンドラ — Low Risk: 自動承認。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）
"""

from approval_types import Decision


class Tier1Handler:
    """Low Risk: 自動承認。

    ya-ta が Tier 1（低リスク）と判定したコマンドは人間も qu-e も介さず即承認する。
    grey zone を人手に回さず流すことで、安全な操作で worker を止めないのが狙い。
    """

    async def handle(self, pending, ctx=None) -> Decision:
        """即承認の Decision を返す（CLI への伝達はアダプタの責務）。

        Tier 1 は審査不要のため pending の内容も ctx も参照しない。allow を返すだけで、
        y/n キー送信・フック応答等の物理的な伝達には関与しない（中核は CLI 非依存）。
        """
        return Decision(allow=True, handler="tier1_auto")
