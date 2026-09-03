"""Task #162: worker 指示文への既定裁量の定型付与（§8.10f 配布規則）のテスト。

生成・編集依頼で worker が細部（内容・文言等）を質問だけ返して止まり、成果物ゼロで
「完了」する逸脱（2026-09-03 E2E 実測: メモ作成依頼に内容を質問・完了検査 未達）の是正。
コード側テンプレートで全 worker step（runbook 除く）の指示文末尾へ機械付与されることを、
_execute_subtask_in_chain の投入 item（_command）で検証する。
"""
import asyncio
import types

from orchestrator import DEFAULT_DISCRETION_NOTE, Orchestrator


def _bare():
    o = Orchestrator.__new__(Orchestrator)
    o.slack = types.SimpleNamespace(notify=lambda *a, **k: None)
    o.enqueued = []

    async def _notify(*a, **k):
        return None

    async def _enqueue(item):
        o.enqueued.append(dict(item))

    o._notify = _notify
    o._enqueue = _enqueue
    # 写像はスタブ（レーン決定の中身は対象外）
    o._plan_execution = lambda *a, **k: ("agent", ["haiku"], False)
    return o


def _task(**over):
    t = {"task_id": "t1", "channel_id": None, "team_id": None, "thread_ts": None}
    t.update(over)
    return t


def _run_subtask(o, task, subtask):
    async def _go():
        futures = {subtask["step"]: asyncio.get_running_loop().create_future()}
        # _enqueue スタブは結果 future を解決しないため、投入直後に自前で解決して
        # await を戻す（検証対象は投入 item の _command のみ）
        async def _resolve():
            while not o.enqueued:
                await asyncio.sleep(0.01)
            o.enqueued[-1]  # 投入済み
            fut = o._pending_future
            if fut and not fut.done():
                fut.set_result("ok")
        # _execute_subtask_in_chain は _enqueue した item の _result_future を await する。
        # スタブ側でそれを解決できるよう enqueue 時に控える
        orig = o._enqueue

        async def _capture(item):
            o._pending_future = item.get("_result_future")
            await orig(item)
            if o._pending_future and not o._pending_future.done():
                o._pending_future.set_result("ok")

        o._enqueue = _capture
        o._pending_future = None
        await o._execute_subtask_in_chain(task, subtask, {}, futures, None)
    asyncio.run(_go())


def test_note_constant_states_noninteractive_and_default_discretion():
    """定型文の中身: 非対話であること・細部の既定裁量・前提欠落時の報告、の 3 要素を持つ。"""
    assert "非対話" in DEFAULT_DISCRETION_NOTE
    assert "妥当な既定" in DEFAULT_DISCRETION_NOTE
    assert "前提の欠落" in DEFAULT_DISCRETION_NOTE


def test_agent_step_command_gets_discretion_note():
    o = _bare()
    subtask = {"step": 1, "command": "docs/note.md にメモを書く",
               "execution": "agent", "depth": "shallow", "confidence": 0.9,
               "depends_on": []}
    _run_subtask(o, _task(), subtask)
    assert len(o.enqueued) == 1
    cmd = o.enqueued[0]["_command"]
    assert cmd.startswith("docs/note.md にメモを書く")
    assert cmd.endswith(DEFAULT_DISCRETION_NOTE)


def test_runbook_step_never_gets_note():
    """runbook（決定的実行）はプロンプトを持たず、定型付与の対象外（構造的除外）。"""
    o = _bare()
    ran = {}

    def _run_runbook_step(task, subtask):
        ran["called"] = True
        return "runbook done"

    o._run_runbook_step = _run_runbook_step

    async def _go():
        futures = {1: asyncio.get_running_loop().create_future()}
        subtask = {"step": 1, "command": "git -C /r push origin HEAD",
                   "execution": "runbook", "depth": None, "confidence": 1.0,
                   "depends_on": [], "_runbook": {"kind": "push", "params": {}}}
        await o._execute_subtask_in_chain(_task(), subtask, {}, futures, None)
    asyncio.run(_go())
    assert ran.get("called") is True
    assert o.enqueued == []  # queue へ載らない＝_command 装飾の経路を通らない


def test_constraints_block_precedes_and_note_trails():
    """拘束定型（前置）と既定裁量定型（末尾）が同居し、原文がその間に残る。"""
    o = _bare()
    subtask = {"step": 1, "command": "README を追記する",
               "execution": "agent", "depth": "shallow", "confidence": 0.9,
               "depends_on": []}
    _run_subtask(o, _task(constraints=[
        {"text": "テストや検証は行わない", "forbid": True, "patterns": ["pytest"]}]),
        subtask)
    cmd = o.enqueued[0]["_command"]
    assert "テストや検証は行わない" in cmd.split("README を追記する")[0]  # 拘束は前置
    assert cmd.endswith(DEFAULT_DISCRETION_NOTE)                        # 定型は末尾
