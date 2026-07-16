"""Tier 2 ハンドラ — Medium Risk: qu-e 審査。deny 時は Tier 3 にエスカレート。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）

通信方式: qu-e は MBP 上の別プロセス。SSH + subprocess で review_cli.py を 1 ショット実行する
（設計書 §8.8、NF-01 通信は SSH）。例外時は安全側で escalate（Tier 3 へ）。
"""

import asyncio
import json
import shlex
import subprocess

from approval_types import Decision, operation_str


class Tier2Handler:
    """Medium Risk: qu-e 審査。

    ya-ta が Tier 2（中リスク）と判定したコマンドを、別マシンの qu-e（sentinel）に再審査
    させる中間層。qu-e が approve なら自動承認、deny / escalate なら人間（Tier 3）へ上げる。
    """

    def __init__(self, ssh_host: str = "mbp", qu_e_dir: str = "/opt/taka-ma/qu-e", *,
                 timeout_sec: float):
        """qu-e への SSH 接続先と配備ディレクトリを保持する。

        Args:
            ssh_host: qu-e が動く MBP の SSH ホスト名（NF-01: コンポーネント間通信は SSH）。
            qu_e_dir: qu-e のソース配備先。review_cli.py の実行 cwd / PYTHONPATH に使う。
            timeout_sec: review_cli 1 ショット（SSH + qu-e LLM 審査）の応答待ち上限秒
                （sa-ru.yaml approval.tier2_timeout_sec が唯一の源。コード側に既定値なし）。
        """
        self.ssh_host = ssh_host
        self.qu_e_dir = qu_e_dir
        self.timeout_sec = timeout_sec

    async def handle(self, pending, ctx=None) -> Decision:
        """qu-e に審査させ、approve なら allow・それ以外は Tier 3 へエスカレートする Decision を返す。

        ctx は使わない（エスカレート時の risk_reason 引き継ぎは呼び出し側＝
        ApprovalPipeline が行う）。qu-e の deny も「自動拒否」せず人間に上げるのは、
        中リスクの最終判断を人に委ねる設計のため。CLI への伝達には関与しない（中核は CLI 非依存）。
        """
        # qu-e にレビューを依頼（SSH 経由）
        result = await self._call_sentinel(pending)

        if result.get("decision") == "approve":
            return Decision(allow=True, handler="tier2_sentinel")
        # qu-e が deny / escalate → Tier 3 にエスカレート（判定理由を引き継ぐ内部シグナル）
        return Decision(allow=False, escalate=True, handler="tier2_sentinel",
                        reason=result.get("reason", ""))

    async def _call_sentinel(self, pending) -> dict:
        """qu-e review_cli.py を SSH + subprocess で 1 ショット実行し、JSON を返す（§8.8）。

        review_cli の出力: {"decision": approve|deny|escalate, "reason": ..., "risk_score": ...}
        通信・パース失敗時は安全側で escalate を返す。
        """
        context = json.dumps({"context": getattr(pending, "context", "")}, ensure_ascii=False)
        # SSH 非ログインシェルの PATH には素の python が無い（macOS 標準・実機 rc=127 で確認）。
        # 他コンポーネントと同じ venv の絶対パスで固定する。
        remote = (
            f"cd {self.qu_e_dir} && PYTHONPATH={self.qu_e_dir} "
            f"/opt/taka-ma-env/bin/python sentinel/review_cli.py --mode command "
            f"--input {shlex.quote(operation_str(pending))} --context {shlex.quote(context)}"
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run, ["ssh", self.ssh_host, remote],
                capture_output=True, text=True, timeout=self.timeout_sec,
            )
            if result.returncode != 0:
                return {"decision": "escalate", "reason": f"qu-e review SSH failed: {result.stderr.strip()}"}
            return json.loads(result.stdout)
        except Exception as e:
            return {"decision": "escalate", "reason": f"qu-e review error: {e}"}
