"""Tier 3 ハンドラ — High Risk: 人間承認（Slack 経由・cross-process）。

設計書 §8.10（u-zu → sa-ru 承認結果通知）のファイルベース方式を実装する:

  1. sa-ru: 承認ファイル `{APPROVAL_DIR}/{request_id}.json` を pending で作成
  2. sa-ru: Slack に Block Kit 承認リクエストを送信（送信元ワークスペースへ）
  3. ユーザー: Slack で Approve / Reject ボタンをクリック
  4. u-zu: ボタンイベント受信 → 承認ファイルの status を更新（approved / rejected）
  5. sa-ru: ポーリング（1 秒間隔・承認待ち中のみ）で status 変更を検知 → PTY に y/n

5 分 pending のままなら自動 deny（status=timeout に更新し Slack へ通知）。

旧実装は同一プロセス内の asyncio.Future を待つだけで、別プロセス u-zu のボタン結果が
到達せず常に timeout deny になっていた。本実装で cross-process を完成させる。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）
"""

import asyncio
import datetime
import json
import logging
import os
import time
import uuid

from approval_types import Decision, operation_str

logger = logging.getLogger("sa-ru.tier3")

# 承認ファイルを作成・ポーリングするディレクトリ。u-zu（approval_store.py）と sa-ru で
# 同一ディレクトリを共有する（共有 FS、§8.3 と同経路）。デプロイ間で 1 つの環境変数
# `TAKA_MA_APPROVAL_DIR` を両プロセスに与えれば供給元を 1 つにできる（パス直書きの SSOT 化）。
APPROVAL_DIR = os.environ.get("TAKA_MA_APPROVAL_DIR", "/opt/taka-ma/data/approvals")
# タイムアウト（§8.10: 5 分）とポーリング間隔（§8.10: 承認待ち中のみ 1 秒）は
# sa-ru.yaml の approval.tier3_timeout_sec / approval.poll_interval_sec を唯一の源にする
# （ApprovalPipeline が構築時に注入。コード側に既定値を置かない＝供給元の二重化を防ぐ）。

# 承認ファイル status の契約値（§8.10）。u-zu(approval_store.py) は別ツリーに配備され
# import 共有できないため、両側が**同じ文字列**を使う規約。定数化で各プロセス内のタイプミスを
# 防ぎ、`grep STATUS_` で両側の一致を確認できるようにする（裸リテラル散在の解消）。
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
# poll で終端とみなす status（人間が押した結果）。timeout は sa-ru 側が確定する。
TERMINAL_DECISIONS = (STATUS_APPROVED, STATUS_REJECTED)


class Tier3Handler:
    """High Risk: 人間承認（Slack 経由・ファイルベース cross-process）。"""

    def __init__(self, slack_notifier, approval_dir: str = APPROVAL_DIR, *,
                 timeout_sec: float, poll_interval_sec: float):
        """Tier 3 ハンドラを構築する。

        Args:
            slack_notifier: 人間へ承認リクエストを提示する通知手段。
            approval_dir: u-zu のボタン結果が書き込まれる承認ファイルの監視ディレクトリ
                （cross-process 連携の受け渡し場所、§8.10）。
            timeout_sec: pending のまま自動 deny へ倒すまでの上限秒（§8.10。
                sa-ru.yaml approval.tier3_timeout_sec が唯一の源）。
            poll_interval_sec: 承認待ち中の status ポーリング間隔秒（§8.10。
                sa-ru.yaml approval.poll_interval_sec が唯一の源）。
        """
        self.slack_notifier = slack_notifier
        self.approval_dir = approval_dir
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec

    def _generate_request_id(self) -> str:
        """承認リクエストの一意 ID（Slack ボタン value／承認ファイル名で特定）。"""
        return uuid.uuid4().hex

    async def handle(self, pending, ctx=None) -> Decision:
        """承認ファイルを pending 作成 → Slack 送信 → status 変化をポーリング → 終端で Decision を返す。

        ctx（ApprovalPipeline.decide が渡す）から instance_id / risk_reason /
        team_id / channel / task_id を受け取り、承認リクエストを送信元ワークスペースへ返す。
        allow/deny の物理的な伝達（y/n 送信・フック応答）には関与しない（中核は CLI 非依存）。
        """
        ctx = ctx or {}
        request_id = self._generate_request_id()
        instance_id = ctx.get("instance_id", "")
        risk_reason = ctx.get("risk_reason", "")
        team_id = ctx.get("team_id")
        channel = ctx.get("channel")
        task_id = ctx.get("task_id", "")
        thread_ts = ctx.get("thread_ts")
        # 承認者が「何を実行しようとしているか」を判断できるよう、前後文脈と操作文字列を渡す。
        context = getattr(pending, "context", "")
        command = operation_str(pending)

        approval_path = os.path.join(self.approval_dir, f"{request_id}.json")

        # 承認ファイルを status=pending で作成（§8.10 フロー 1）。
        # command は人間可読の操作文字列、tool_name/tool_input は構造化データ（後方互換で併記）。
        # team_id / channel_id は応答先ワークスペース特定用の記録。
        self._write_record(approval_path, {
            "request_id": request_id,
            "task_id": task_id,
            "instance_id": instance_id,
            "command": command,
            "tool_name": pending.tool_name,
            "tool_input": pending.tool_input,
            "context": context,
            "tier": 3,
            "risk_reason": risk_reason,
            "status": STATUS_PENDING,
            "created_at": self._now(),
            "decided_at": None,
            "decided_by": None,
            "team_id": team_id or "",
            "channel_id": channel or "",
            "thread_ts": thread_ts,
        })

        # Slack に Block Kit 承認リクエストを送信（送信元 WS へ。SlackNotifier は同期メソッド）。
        # 送信に失敗すると人間承認を得る経路が無いため、安全側で deny に倒す（孤児 pending を残さない）。
        try:
            self.slack_notifier.send_approval_request(
                request_id=request_id,
                command=command,
                instance_id=instance_id,
                risk_reason=risk_reason,
                context=context,
                channel=channel,
                team_id=team_id,
                thread_ts=thread_ts,
            )
        except Exception:
            logger.exception("Tier3 承認リクエストの Slack 送信に失敗。安全側で deny します: %s", request_id)
            self._mark_status(approval_path, STATUS_ERROR)
            self._finalize(approval_path)
            return Decision(allow=False, handler="tier3_human", reason="slack_error")

        # status 変化を poll_interval_sec 間隔でポーリング（最大 timeout_sec。いずれも
        # sa-ru.yaml approval が唯一の源）。u-zu が approved / rejected に更新する。
        # decide_deadline（デーモンの外側タイムアウト締切）があるときは、その内側に収める。
        # 前段（リスク分類・qu-e 審査）の消費時間で残余が timeout_sec を割っても、Tier3 が
        # 外側より先に timeout を確定させ、監査記録・done/ 退避・timeout 理由を必ず残す（V11 の包含）。
        budget = self.timeout_sec
        dl = ctx.get("decide_deadline")
        if dl is not None:
            budget = max(0, min(self.timeout_sec, int(dl - time.monotonic())))
        decision = await self._poll_decision(approval_path, budget)
        if decision is None:
            # ポーリングが時間切れ。ただし最後の読取と本処理の間に u-zu が決定を書く競合があるため、
            # timeout を主張するのは「まだ pending」のときだけ。既に決定済みならそれを尊重する（最終裁定）。
            decision = self._claim_timeout(approval_path)

        try:
            if decision == STATUS_APPROVED:
                return Decision(allow=True, handler="tier3_human")
            if decision == STATUS_REJECTED:
                return Decision(allow=False, handler="tier3_human")

            # timeout（または想定外 status）→ 自動 deny し Slack へ通知（§8.10）。
            # 通知失敗が deny 確定を覆さないよう、送信は例外を握って続行する。
            try:
                self.slack_notifier.notify(
                    f"承認タイムアウト: 自動 deny しました (ID: {request_id})",
                    channel=channel, team_id=team_id, thread_ts=thread_ts,
                )
            except Exception:
                logger.exception("タイムアウト通知の送信に失敗（deny は確定）: %s", request_id)
            return Decision(allow=False, handler="tier3_human", reason="timeout")
        finally:
            # 決定済みの承認ファイルは done/ へ退避（履歴保持＋ディレクトリ肥大防止）。
            self._finalize(approval_path)

    async def _poll_decision(self, approval_path: str, budget: float) -> str | None:
        """承認ファイルの status を poll_interval_sec 間隔で読み、approved / rejected を検知して返す。

        budget 秒（tier3_timeout_sec・decide_deadline があれば残余に切詰め）以内に決まらなければ
        None（タイムアウト）。ファイル読取は同期 I/O のため、event loop を塞がないよう
        to_thread でワーカスレッドに逃がす（共有 FS で遅延しても他のコルーチン＝
        dispatcher/worker を止めない）。
        """
        elapsed = 0
        while elapsed < budget:
            await asyncio.sleep(self.poll_interval_sec)
            elapsed += self.poll_interval_sec
            status = await asyncio.to_thread(self._read_status, approval_path)
            if status in TERMINAL_DECISIONS:
                return status
        return None

    def _read_status(self, approval_path: str) -> str | None:
        """承認ファイルの現在 status を返す。読み取り失敗（書き込み途中等）は None で次回再試行。"""
        try:
            with open(approval_path) as f:
                return json.load(f).get("status")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _claim_timeout(self, approval_path: str) -> str | None:
        """pending のままなら status=timeout に確定し "timeout" を返す。

        既に approved/rejected が書き込まれていれば**上書きせず**その status を返す
        （u-zu の決定を握り潰さない＝競合の最終裁定）。ファイル不在/破損は None。
        """
        try:
            with open(approval_path) as f:
                record = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        status = record.get("status")
        if status != STATUS_PENDING:
            return status
        record["status"] = STATUS_TIMEOUT
        record["decided_at"] = self._now()
        self._write_record(approval_path, record)
        return STATUS_TIMEOUT

    def _mark_status(self, approval_path: str, status: str):
        """承認ファイルの status を上書きする（Slack 送信失敗時の error 記録など）。不在/破損は無視。"""
        try:
            with open(approval_path) as f:
                record = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        record["status"] = status
        record["decided_at"] = self._now()
        self._write_record(approval_path, record)

    def _finalize(self, approval_path: str):
        """決定済み承認ファイルを done/ サブディレクトリへ退避する。

        承認ディレクトリの無制限肥大を防ぎ履歴を残す（FileAuditHandler の done/ 退避と同方式）。
        既に移動済み/不在なら何もしない。
        """
        try:
            done_dir = os.path.join(self.approval_dir, "done")
            os.makedirs(done_dir, exist_ok=True)
            os.replace(approval_path, os.path.join(done_dir, os.path.basename(approval_path)))
        except FileNotFoundError:
            pass

    @staticmethod
    def _now() -> str:
        """ローカルタイムゾーン付き ISO8601 タイムスタンプ。"""
        return datetime.datetime.now().astimezone().isoformat()

    @staticmethod
    def _write_record(approval_path: str, record: dict):
        """承認ファイルを原子的に書き込む。

        sa-ru のポーラと u-zu の更新が同じファイルを触るため、書き手ごとに一意な tmp
        （uuid サフィックス）へ書いて os.replace で差し替える。共有の固定 tmp 名だと
        2 つの書き手が互いの tmp を truncate しうるため、衝突しない一時名にする（§8.10）。
        """
        os.makedirs(os.path.dirname(approval_path), exist_ok=True)
        tmp = f"{approval_path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, approval_path)
        except Exception:
            # 書込/置換が途中失敗したら一意 tmp を残さず掃除する（ディレクトリ肥大防止）。
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
