"""共有 FileQueue の振る舞いテスト。

検証する振る舞い（存在ではなく挙動）:
- 壊れた JSON は failed/ へ隔離され、走査対象から外れる（exec-confirmations の隔離ドリフト回帰も兼ねる）。
- claim は reserve_status 指定時のみ status を書き換える（dispatcher=予約あり / control=予約なしの両分岐）。
- run（pick-one ループ）は処理成功で done/、handler 失敗時に on_error に従い隔離 or 据え置きする。

file_queue.py は orchestrator パッケージ本体（pexpect/watchdog 等の重い依存）を引かないため、
ファイル直ロードで単体テストする。
"""

import asyncio
import datetime
import importlib.util
import json
import os

import pytest

_HERE = os.path.dirname(__file__)
_FQ_PATH = os.path.join(_HERE, "..", "file_queue.py")

_spec = importlib.util.spec_from_file_location("file_queue", _FQ_PATH)
file_queue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(file_queue)
FileQueue = file_queue.FileQueue


def _write(directory, name, record):
    path = os.path.join(directory, name)
    with open(path, "w") as fp:
        if isinstance(record, str):
            fp.write(record)          # 壊れた JSON を意図的に書く用
        else:
            json.dump(record, fp, ensure_ascii=False)
    return path


def _names(directory):
    return sorted(f for f in os.listdir(directory) if f.endswith(".json"))


def test_iter_records_quarantines_broken(tmp_path):
    """壊れた JSON は failed/ へ隔離し、健全分のみ yield する（exec-confirmations 隔離ドリフト回帰）。"""
    d = str(tmp_path)
    _write(d, "good.json", {"status": "pending"})
    _write(d, "bad.json", "{ this is not json")
    q = FileQueue(d, poll_interval=0.01)

    records = list(q.iter_records())

    assert [os.path.basename(p) for p, _ in records] == ["good.json"]
    assert _names(os.path.join(d, "failed")) == ["bad.json"]
    assert not os.path.exists(os.path.join(d, "bad.json"))


def test_claim_reserves_status_and_stamps_updated_at(tmp_path):
    """reserve_status 指定時は取得と同時に status を書き換え、updated_at を自動で刻む（dispatcher 経路）。"""
    d = str(tmp_path)
    _write(d, "t.json", {"status": "init"})
    q = FileQueue(d, poll_interval=0.01)

    picked = q.claim("init", reserve_status="accepted")

    assert picked is not None
    with open(os.path.join(d, "t.json")) as fp:
        on_disk = json.load(fp)
    assert on_disk["status"] == "accepted"
    # claim が予約時に updated_at を ISO 文字列で刻む（caller は渡さない）
    assert "updated_at" in on_disk
    datetime.datetime.fromisoformat(on_disk["updated_at"])  # ISO としてパース可能


def test_claim_without_reserve_leaves_status(tmp_path):
    """reserve_status 未指定なら status を書き換えない（control 経路の冪等・取りこぼし防止）。"""
    d = str(tmp_path)
    _write(d, "c.json", {"status": "pending"})
    q = FileQueue(d, poll_interval=0.01)

    picked = q.claim("pending")

    assert picked is not None
    with open(os.path.join(d, "c.json")) as fp:
        assert json.load(fp)["status"] == "pending"


def test_claim_reserve_write_is_atomic_no_tmp_leftover(tmp_path):
    """予約書込は tmp→os.replace の原子的書込。中間 .tmp を残さず、内容は健全な JSON。"""
    d = str(tmp_path)
    _write(d, "a.json", {"status": "init", "task_id": "x"})
    q = FileQueue(d, poll_interval=0.01)

    q.claim("init", reserve_status="accepted")

    leftover = [f for f in os.listdir(d) if f.endswith(".tmp")]
    assert leftover == []
    with open(os.path.join(d, "a.json")) as fp:
        rec = json.load(fp)           # 破損していない（torn-read 窓を作らない）
    assert rec["status"] == "accepted" and rec["task_id"] == "x"


def test_claim_returns_none_when_no_match(tmp_path):
    d = str(tmp_path)
    _write(d, "c.json", {"status": "processing"})
    q = FileQueue(d, poll_interval=0.01)
    assert q.claim("init") is None


async def _drain_once(q, handler, **kw):
    """run() を起動し、1 周処理させてからキャンセルする（無限ループを試験で止める）。"""
    task = asyncio.create_task(q.run(handler, **kw))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_run_moves_to_done_on_success(tmp_path):
    d = str(tmp_path)
    _write(d, "m.json", {"status": "init"})
    q = FileQueue(d, poll_interval=0.01)
    seen = []

    async def handler(path, record):
        seen.append(record["status"])

    asyncio.run(_drain_once(q, handler, ready_status="init", reserve_status="processing"))

    assert seen == ["processing"]                       # 予約後の record が渡る
    assert _names(os.path.join(d, "done")) == ["m.json"]
    assert not os.path.exists(os.path.join(d, "m.json"))


def test_run_quarantines_on_handler_error(tmp_path):
    """quarantine_on_error=True（control・既定）: handler 失敗で failed/ へ隔離しループは継続する。"""
    d = str(tmp_path)
    _write(d, "e.json", {"status": "pending"})
    q = FileQueue(d, poll_interval=0.01)

    async def handler(path, record):
        raise RuntimeError("boom")

    asyncio.run(_drain_once(q, handler, ready_status="pending", quarantine_on_error=True))

    assert _names(os.path.join(d, "failed")) == ["e.json"]
    assert not os.path.exists(os.path.join(d, "e.json"))


def test_run_leaves_on_handler_error_when_configured(tmp_path):
    """quarantine_on_error=False（conversation）: 予約済みのため失敗しても据え置き、done/failed に移さない。"""
    d = str(tmp_path)
    _write(d, "l.json", {"status": "init"})
    q = FileQueue(d, poll_interval=0.01)

    async def handler(path, record):
        raise RuntimeError("boom")

    asyncio.run(_drain_once(q, handler, ready_status="init",
                            reserve_status="processing", quarantine_on_error=False))

    assert os.path.exists(os.path.join(d, "l.json"))     # 据え置き
    assert not os.path.exists(os.path.join(d, "done"))
    assert not os.path.exists(os.path.join(d, "failed"))
    with open(os.path.join(d, "l.json")) as fp:
        assert json.load(fp)["status"] == "processing"   # 予約済み→再取得されない
