"""Tier 2 ハンドラ — Medium Risk: qu-e 審査。deny 時は Tier 3 にエスカレート。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）

通信方式: qu-e は MBP 上の別プロセス。SSH + subprocess で review_cli.py を 1 ショット実行する
（設計書 §8.8、NF-01 通信は SSH）。例外時は安全側で escalate（Tier 3 へ）。
"""

import asyncio
import json
import shlex
import subprocess


class Tier2Handler:
    """Medium Risk: qu-e 審査。

    ya-ta が Tier 2（中リスク）と判定したコマンドを、別マシンの qu-e（sentinel）に再審査
    させる中間層。qu-e が approve なら自動承認、deny / escalate なら人間（Tier 3）へ上げる。
    """

    def __init__(self, ssh_host: str = "mbp", qu_e_dir: str = "/opt/taka-ma/qu-e"):
        """qu-e への SSH 接続先と配備ディレクトリを保持する。

        Args:
            ssh_host: qu-e が動く MBP の SSH ホスト名（NF-01: コンポーネント間通信は SSH）。
            qu_e_dir: qu-e のソース配備先。review_cli.py の実行 cwd / PYTHONPATH に使う。
        """
        self.ssh_host = ssh_host
        self.qu_e_dir = qu_e_dir

    async def handle(self, prompt, pty_wrapper, ctx=None):
        """qu-e に審査させ、approve なら承認・それ以外は Tier 3 へエスカレートする。

        ctx は使わない（エスカレート時の risk_reason 引き継ぎは呼び出し側＝
        ApprovalPipeline が行う）。qu-e の deny も「自動拒否」せず人間に上げるのは、
        中リスクの最終判断を人に委ねる設計のため。
        """
        # qu-e にレビューを依頼（SSH 経由）
        result = await self._call_sentinel(prompt)

        if result.get("decision") == "approve":
            pty_wrapper.approve()
            return {"action": "approved", "handler": "tier2_sentinel"}
        else:
            # qu-e が deny / escalate → Tier 3 にエスカレート（判定理由を引き継ぐ）
            return {"action": "escalate", "reason": result.get("reason", ""), "handler": "tier2_sentinel"}

    async def _call_sentinel(self, prompt) -> dict:
        """qu-e review_cli.py を SSH + subprocess で 1 ショット実行し、JSON を返す（§8.8）。

        review_cli の出力: {"decision": approve|deny|escalate, "reason": ..., "risk_score": ...}
        通信・パース失敗時は安全側で escalate を返す。
        """
        context = json.dumps({"context": getattr(prompt, "context", "")}, ensure_ascii=False)
        remote = (
            f"cd {self.qu_e_dir} && PYTHONPATH={self.qu_e_dir} "
            f"python sentinel/review_cli.py --mode command "
            f"--input {shlex.quote(prompt.command)} --context {shlex.quote(context)}"
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["ssh", self.ssh_host, remote],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return {"decision": "escalate", "reason": f"qu-e review SSH failed: {result.stderr.strip()}"}
            return json.loads(result.stdout)
        except Exception as e:
            return {"decision": "escalate", "reason": f"qu-e review error: {e}"}
