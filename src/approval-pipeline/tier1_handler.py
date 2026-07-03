"""Tier 1 ハンドラ — Low Risk: 自動承認。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）
"""


class Tier1Handler:
    """Low Risk: 自動承認。

    ya-ta が Tier 1（低リスク）と判定したコマンドは人間も qu-e も介さず即承認する。
    grey zone を人手に回さず流すことで、安全な操作で worker を止めないのが狙い。
    """

    async def handle(self, prompt, pty_wrapper, ctx=None):
        """PTY に y（承認）を送って即承認する。

        Tier 1 は審査不要のため prompt の内容も ctx も参照しない（引数はハンドラ共通
        インターフェースを揃えるためだけに受ける）。戻りの handler 名は監査ログ用。
        """
        pty_wrapper.approve()
        return {"action": "approved", "handler": "tier1_auto"}
