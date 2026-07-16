"""着手確認タイムアウト排除（§8.10b）の振る舞いテスト。

grep では潰せない振る舞い「pending が長時間放置されても自動 timeout で決着せず、
その後の confirmed / rejected が有効に処理される」を分離実行で担保する。
従来は timeout_sec（既定 300 秒）超過の pending を _exec_confirmation_loop が
timeout 決着させ「もう一度指示してください」を強いていた。
"""
import asyncio
import json
import os

from orchestrator import Orchestrator
from orchestrator.file_queue import FileQueue, atomic_write_json


def _stale_pending_record(tmp_path, exec_request_id="req-1"):
    """旧仕様なら確実に期限切れ（created_at が 2020 年）の pending レコードを置く。"""
    path = str(tmp_path / f"{exec_request_id}.json")
    atomic_write_json(path, {
        "exec_request_id": exec_request_id,
        "conversation_id": "T1:C1:1.0",
        "summary": "テスト要約",
        "status": "pending",
        "channel_id": "C1",
        "team_id": "T1",
        "thread_ts": None,
        "created_at": "2020-01-01T00:00:00+00:00",
        "decided_at": None,
        "decided_by": None,
    })
    return path


def _orchestrator(tmp_path):
    """ループ検証に必要な最小構成の Orchestrator（重い __init__ は通さない）。"""
    o = Orchestrator.__new__(Orchestrator)
    o.exec_confirm_q = FileQueue(str(tmp_path), poll_interval=0.01)
    o.exec_confirm_poll = 0.01
    calls = {"create": [], "reject": []}

    class _FakeConversation:
        def create_exec_task(self, record):
            calls["create"].append(record)
            return "task-xyz"

        def notify_rejected(self, record):
            calls["reject"].append(record)

    o.conversation = _FakeConversation()
    return o, calls


async def _run_loop_briefly(o, seconds=0.1):
    task = asyncio.ensure_future(o._exec_confirmation_loop())
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_stale_pending_is_not_timed_out(tmp_path):
    """created_at がどれだけ古くても pending は決着されず、その場に残り続ける。"""
    path = _stale_pending_record(tmp_path)
    o, calls = _orchestrator(tmp_path)

    asyncio.run(_run_loop_briefly(o))

    assert os.path.exists(path), "pending が done/failed へ退避された（timeout 決着が残存）"
    assert json.load(open(path))["status"] == "pending"
    assert calls["create"] == [] and calls["reject"] == []


def test_confirmed_after_long_pending_creates_task(tmp_path):
    """旧仕様の期限を大きく超えた後の confirmed でも確定タスクが生成される。"""
    path = _stale_pending_record(tmp_path)
    o, calls = _orchestrator(tmp_path)

    record = json.load(open(path))
    record["status"] = "confirmed"
    record["decided_by"] = "U1"
    atomic_write_json(path, record)

    asyncio.run(_run_loop_briefly(o))

    assert len(calls["create"]) == 1
    assert calls["create"][0]["exec_request_id"] == "req-1"
    assert not os.path.exists(path), "決着済みレコードが done/ へ退避されていない"


def test_timeout_machinery_is_removed():
    """timeout 判定・通知の実装そのものが存在しない（分岐の復活検知）。"""
    assert not hasattr(Orchestrator, "_is_confirm_expired")
    from orchestrator.conversation import ConversationManager
    assert not hasattr(ConversationManager, "notify_timeout")
