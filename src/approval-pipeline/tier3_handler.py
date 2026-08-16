"""Tier 3 ハンドラ — High Risk: 人間承認（Slack 経由・cross-process）。

設計書 §8.10（u-zu → sa-ru 承認結果通知）のファイルベース方式を実装する:

  1. sa-ru: 承認ファイル `{APPROVAL_DIR}/{request_id}.json` を pending で作成
  2. sa-ru: Slack に Block Kit 承認リクエストを送信（送信元ワークスペースへ）
  3. ユーザー: Slack で Approve / Reject ボタンをクリック
  4. u-zu: ボタンイベント受信 → 承認ファイルの status を更新（approved / rejected）
  5. sa-ru: ポーリング（1 秒間隔・承認待ち中のみ）で status 変更を検知 → PTY に y/n

猶予（hold_grace_sec）を超えても pending なら **保留**（hold）。自動 deny は行わない
（設計書 §3.3 (4) / §8.10）。承認レコードは status=pending のまま存置して held_at を
追記し、Decision{allow:false, hold:true} を返す。ツールはブロックされるが承認要求は
生き続け、人間はいつ押してもよい。worker を畳んだ後の再投入は sa-ru（orchestrator）が
担う（本ハンドラは worker/タスクの生死を知らない）。

旧実装は同一プロセス内の asyncio.Future を待つだけで、別プロセス u-zu のボタン結果が
到達せず常に timeout deny になっていた。本実装で cross-process を完成させる。

構築手順書: docs/procedures/08-approval-pipeline.md Step 4（Tier ハンドラ）
"""

import asyncio
import datetime
import fcntl
import glob
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
# 猶予（§8.10: worker を待たせる上限）とポーリング間隔（§8.10: 承認待ち中のみ 1 秒）は
# sa-ru.yaml の approval.hold_grace_sec / approval.poll_interval_sec を唯一の源にする
# （ApprovalPipeline が構築時に注入。コード側に既定値を置かない＝供給元の二重化を防ぐ）。

# 承認ファイル status の契約値（§8.10）。u-zu(approval_store.py) は別ツリーに配備され
# import 共有できないため、両側が**同じ文字列**を使う規約。定数化で各プロセス内のタイプミスを
# 防ぎ、`grep STATUS_` で両側の一致を確認できるようにする（裸リテラル散在の解消）。
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
# timeout は**新規に書かない**（自動 deny の廃止・§8.10）。過去に書かれたレコードを
# 読む側の互換のためだけに残す定数（終端として受理する）。
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
# poll で終端とみなす status（人間が押した結果）。猶予超過は終端ではなく保留になる。
TERMINAL_DECISIONS = (STATUS_APPROVED, STATUS_REJECTED)
# 猶予超過で保留に落ちたことを示す内部シグナル（status ではない。承認ファイルの status は
# pending のまま据え置き、保留の印は held_at フィールドで持つ）。
HOLD = "hold"


class Tier3Handler:
    """High Risk: 人間承認（Slack 経由・ファイルベース cross-process）。"""

    def __init__(self, slack_notifier, approval_dir: str = APPROVAL_DIR, *,
                 hold_grace_sec: float, poll_interval_sec: float):
        """Tier 3 ハンドラを構築する。

        Args:
            slack_notifier: 人間へ承認リクエストを提示する通知手段。
            approval_dir: u-zu のボタン結果が書き込まれる承認ファイルの監視ディレクトリ
                （cross-process 連携の受け渡し場所、§8.10）。
            hold_grace_sec: **worker を待たせる上限**秒（承認の期限ではない）。超過したら
                保留（hold）に落とす（§8.10。sa-ru.yaml approval.hold_grace_sec が唯一の源）。
            poll_interval_sec: 承認待ち中の status ポーリング間隔秒（§8.10。
                sa-ru.yaml approval.poll_interval_sec が唯一の源）。
        """
        self.slack_notifier = slack_notifier
        self.approval_dir = approval_dir
        self.hold_grace_sec = hold_grace_sec
        self.poll_interval_sec = poll_interval_sec

    def _generate_request_id(self) -> str:
        """承認リクエストの一意 ID（Slack ボタン value／承認ファイル名で特定）。"""
        return uuid.uuid4().hex

    async def handle(self, pending, ctx=None) -> Decision:
        """承認ファイルを pending 作成 → Slack 送信 → status 変化をポーリング → 決着 or 保留を返す。

        ctx（ApprovalPipeline.decide が渡す）から instance_id / risk_reason /
        team_id / channel / task_id を受け取り、承認リクエストを送信元ワークスペースへ返す。
        allow/deny の物理的な伝達（y/n 送信・フック応答）には関与しない（中核は CLI 非依存）。

        猶予（hold_grace_sec）を超えても決着しなければ保留（hold）を返す。自動 deny はしない。
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

        # 保留の冪等化（§8.10）: 同一タスクに未決着の保留レコードがある間は、worker が別ツール
        # で迂回を試みても新規レコードを作らず Slack へも再投稿せず、猶予を待たずに即 hold を返す。
        # これが無いと「迂回 N 回 × 猶予」の待ちが累積し、保留通知も同じ内容で連投される。
        held_id = await asyncio.to_thread(self._find_held, task_id)
        if held_id:
            logger.info("保留中の承認があるため即座に hold を返します: task_id=%s request_id=%s",
                        task_id, held_id)
            return Decision(allow=False, hold=True, handler="tier3_human",
                            reason=f"hold: 承認待ち（保留中 {held_id}）")

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

        # status 変化を poll_interval_sec 間隔でポーリング（最大 hold_grace_sec。いずれも
        # sa-ru.yaml approval が唯一の源）。u-zu が approved / rejected に更新する。
        # decide_deadline（デーモンの外側タイムアウト締切）があるときは、その内側に収める。
        # 前段（リスク分類・qu-e 審査）の消費時間で残余が猶予を割っても、Tier3 が外側より
        # 先に保留を確定させ、監査記録と held_at を必ず残す（V11 の包含関係）。
        budget = self.hold_grace_sec
        dl = ctx.get("decide_deadline")
        if dl is not None:
            budget = max(0, min(self.hold_grace_sec, int(dl - time.monotonic())))
        decision = await self._poll_decision(approval_path, budget)
        if decision is None:
            # ポーリングが猶予切れ。ただし最後の読取と本処理の間に u-zu が決定を書く競合があるため、
            # 保留を主張するのは「まだ pending」のときだけ。既に決定済みならそれを尊重する（最終裁定）。
            decision = await asyncio.to_thread(self._claim_hold, approval_path)

        if decision == STATUS_APPROVED:
            self._finalize(approval_path)
            return Decision(allow=True, handler="tier3_human")
        if decision == STATUS_REJECTED:
            self._finalize(approval_path)
            return Decision(allow=False, handler="tier3_human")

        if decision == HOLD:
            # 猶予超過 → 保留（§3.3 (4) / §8.10）。承認レコードは pending のまま**存置**し
            # done/ へ退避しない（退避すると人間が後から押しても届かなくなる＝旧実装の実害）。
            # 人間の決着と未了分の再投入は sa-ru が承認レコードを見て行う。
            try:
                self.slack_notifier.notify(
                    f"承認待ちのため保留しました。期限はありません。"
                    f"承認いただければ未了分から再開します (ID: {request_id})",
                    channel=channel, team_id=team_id, thread_ts=thread_ts,
                )
            except Exception:
                logger.exception("保留通知の送信に失敗（保留は確定）: %s", request_id)
            return Decision(allow=False, hold=True, handler="tier3_human",
                            reason=f"hold: 承認待ち ({request_id})")

        # 想定外 status（レコード消失・破損、過去に書かれた legacy timeout 等）は保留にできない
        # ため安全側で deny に倒す。保留は「承認レコードが生きている」ことが前提であり、拠り所を
        # 失った状態を保留と称すると、誰も決着させられないタスクが永久に残る。
        self._finalize(approval_path)
        return Decision(allow=False, handler="tier3_human",
                        reason=f"承認レコードを追跡できません (status={decision})")

    async def _poll_decision(self, approval_path: str, budget: float) -> str | None:
        """承認ファイルの status を poll_interval_sec 間隔で読み、approved / rejected を検知して返す。

        budget 秒（hold_grace_sec・decide_deadline があれば残余に切詰め）以内に決まらなければ
        None（猶予切れ＝保留へ）。ファイル読取は同期 I/O のため、event loop を塞がないよう
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

    def _claim_hold(self, approval_path: str) -> str | None:
        """pending のままなら held_at を追記して保留を確定し HOLD を返す（status は pending 据置）。

        既に approved/rejected が書き込まれていれば**上書きせず**その status を返す
        （u-zu の決定を握り潰さない＝競合の最終裁定）。ファイル不在/破損は None。

        read-modify-write は u-zu(approval_store.resolve_approval) と同じ `{path}.lock` の
        flock 排他下で行う（§8.10「決定の一意性」）。ロック無しだと「u-zu が approved を書く」
        のと「こちらが held_at を書き戻す」が交差し、承認が held_at 付き pending に巻き戻って
        押したはずのボタンが消える。
        """
        lock_f = self._open_lock(approval_path)
        if lock_f is None:
            return None
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                with open(approval_path) as f:
                    record = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return None
            status = record.get("status")
            if status != STATUS_PENDING:
                return status
            # status は pending のまま。保留の印は held_at のみ（§8.10「保留時に追記する
            # フィールド」）。status を変えると u-zu 側の「pending のときのみ決定を受理」に
            # 引っかかり、人間がボタンを押せなくなる。
            record["held_at"] = self._now()
            self._write_record(approval_path, record)
            return HOLD
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
            lock_f.close()

    def _find_held(self, task_id: str) -> str | None:
        """当該タスクに未決着の保留レコード（status=pending かつ held_at 有り）があれば
        その request_id を返す（無ければ None）。

        保留の冪等化（§8.10）に使う。task_id が空のときは同定できないため常に None を返す
        （空 task_id 同士を同一タスクと誤認すると、無関係な承認要求まで保留に巻き込む）。
        走査対象は承認ディレクトリ直下のみ。done/ 配下は決着済み＝保留ではない。
        """
        if not task_id:
            return None
        for path in glob.glob(os.path.join(self.approval_dir, "*.json")):
            try:
                with open(path) as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue  # 書込途中・破損は次周回に委ねる（保留の見落としは冪等性が落ちるだけ）
            if (record.get("task_id") == task_id
                    and record.get("status") == STATUS_PENDING
                    and record.get("held_at")):
                return record.get("request_id") or os.path.basename(path)[:-len(".json")]
        return None

    @staticmethod
    def _open_lock(approval_path: str):
        """承認ファイル単位のロックファイルを開く（u-zu と同じ `{path}.lock` 規約）。

        承認ディレクトリ自体が無い等でロックを取れないときは None（呼び出し側で不在扱い）。
        """
        try:
            return open(f"{approval_path}.lock", "w")
        except OSError:
            return None

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
