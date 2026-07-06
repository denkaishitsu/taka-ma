"""task-90（worker I/O 堅牢化・H14/H15）の振る舞いテスト。

grep/AST では潰せない振る舞い（headless の ssh が -tt を付ける・pty close が tmux を kill する・
_notify/_update_status が同期 I/O を別スレッドへ逃がす async である）を分離実行で担保する。
設計書 §8.5 / §10.7。
"""
import asyncio
import inspect
import types

import pytest

from orchestrator import Orchestrator
from orchestrator import headless_runner as hr
from orchestrator import pty_wrapper as pw


# ── H15: pty close が tmux セッションを kill する（§8.5 資源回収） ──

def test_pty_close_kills_tmux_session(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(pw.subprocess, "run", fake_run)
    w = pw.WorkerPtyWrapper("inst-123", command="claude", ssh_host="mbp")
    w.child = None  # 未起動でも close の後始末（tmux kill）は走る
    w.close()

    assert any(c[0] == "ssh" and "tmux kill-session -t inst-123" in c[-1] for c in calls), calls


def test_pty_close_swallows_kill_failure(monkeypatch):
    """kill-session の SSH 失敗は例外を外へ出さない（後始末が本処理を止めない）。"""
    def boom(cmd, **kw):
        raise OSError("ssh unreachable")

    monkeypatch.setattr(pw.subprocess, "run", boom)
    w = pw.WorkerPtyWrapper("inst-x", command="claude", ssh_host="mbp")
    w.child = None
    w.close()  # raise しなければ OK


# ── H15: headless の ssh が -tt を付ける（timeout 時に remote へ SIGHUP 伝播） ──

def test_headless_run_uses_tt(monkeypatch):
    captured = {}

    class FakeStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration  # result を出さず終了＝ハング扱い

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProc:
        def __init__(self):
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()

        async def wait(self):
            return 0

        def kill(self):
            pass

    async def fake_exec(*args, **kw):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(hr.asyncio, "create_subprocess_exec", fake_exec)
    runner = hr.WorkerHeadlessRunner("inst", command="claude", ssh_host="mbp")
    with pytest.raises(RuntimeError):   # result 無し終了 → ハング RuntimeError
        asyncio.run(runner.run("do it", timeout=5))

    assert captured["args"][0] == "ssh"
    assert captured["args"][1] == "-tt"


# ── H14: 同期 I/O をイベントループから切り離す async 化 ──

def test_status_and_notify_are_coroutines():
    assert inspect.iscoroutinefunction(Orchestrator._update_status)
    assert inspect.iscoroutinefunction(Orchestrator._notify)
    assert inspect.iscoroutinefunction(Orchestrator._notify_failure)
    assert inspect.iscoroutinefunction(Orchestrator._finalize_confirm)


def test_notify_offloads_to_slack(monkeypatch):
    """_notify は別スレッド経由で slack.notify を同じ引数で呼ぶ（イベントループを塞がない）。"""
    got = {}
    o = Orchestrator.__new__(Orchestrator)
    o.slack = types.SimpleNamespace(
        notify=lambda text, channel, **kw: got.update(text=text, channel=channel, **kw))

    async def _run():
        await o._notify("hi", "C1", team_id="T1", thread_ts="ts9")

    asyncio.run(_run())
    assert got == {"text": "hi", "channel": "C1", "team_id": "T1", "thread_ts": "ts9"}


def test_update_status_offloads_push(monkeypatch, tmp_path):
    """_update_status は _push_task_context（SSH 同期）を別スレッドで呼ぶ（await で待つ）。"""
    from orchestrator.file_queue import atomic_write_json
    path = str(tmp_path / "t.json")
    atomic_write_json(path, {"task_id": "t1", "status": "init", "command": "c"})

    o = Orchestrator.__new__(Orchestrator)
    o.task_dir = str(tmp_path)
    pushed = {}
    o._push_task_context = lambda task: pushed.update(status=task["status"])

    async def _run():
        await o._update_status(path, "in_progress")

    asyncio.run(_run())
    assert pushed["status"] == "in_progress"
