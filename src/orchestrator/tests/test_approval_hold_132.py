"""Task #132 承認保留・再投入の振る舞いテスト（設計書 §3.3 (4) / §8.10 / §10.4）。

grep や AST では潰せない振る舞いを分離実行で担保する:

  - 保留時に**次段モデルが呼ばれない**こと（昇格＝承認の迂回になりうるため。判定は
    「実行関数が呼ばれたか」という観測可能な事実で行い、コードの見た目に依存しない）
  - worker が例外で終わっても保留が優先されること（承認をブロックされた worker の終了状態は
    保留の根拠にできない — 実機で「ブロックされても正常終了で返る」ことを確認済み）
  - 保留でタスクが `completed` にならず `pending_approval` に落ち、再投入に要る情報
    （`completed_steps` / `held_approval_id` / 凍結プラン）がディスクに残ること（偽の緑の解消）
  - 再投入で済んだ step が再実行されないこと
  - 決着の反映（Approve→再投入 / Reject→failed / `max_reinject` 超過→failed）

Orchestrator の __init__ はモデル/SSH/config 一式を要求するため、__new__ で本体を作らず
対象メソッドと、それが触る属性だけを差し込む（test_execute_chain_86.py と同じ流儀）。
"""
import asyncio
import datetime
import json
import os
import types

from orchestrator import ApprovalHold, Orchestrator, STATUS_PENDING_APPROVAL


async def _noop_notify(*a, **k):
    return None


def _write_approval(approval_dir, request_id, task_id, status, *, held=True):
    """承認レコードを 1 件置く（Tier3Handler が書くのと同じ形・§8.10）。"""
    os.makedirs(approval_dir, exist_ok=True)
    record = {"request_id": request_id, "task_id": task_id, "status": status}
    if held:
        record["held_at"] = datetime.datetime.now().astimezone().isoformat()
    with open(os.path.join(approval_dir, f"{request_id}.json"), "w") as f:
        json.dump(record, f, ensure_ascii=False)


def _write_task(task_dir, task):
    """タスクファイルを 1 件置き、そのパスを返す。"""
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, f"{task['task_id']}.json")
    with open(path, "w") as f:
        json.dump(task, f, ensure_ascii=False)
    return path


# ── _held_approval: 保留の当たり判定（タスク単位・§8.10） ──

def test_held_approval_detects_pending_with_held_at(tmp_path):
    o = Orchestrator.__new__(Orchestrator)
    o.approval_dir = str(tmp_path)
    _write_approval(str(tmp_path), "req-1", "t1", "pending")
    assert o._held_approval("t1") == "req-1"


def test_held_approval_ignores_pending_without_held_at(tmp_path):
    """猶予内でまだ人間を待っているだけのレコードは保留ではない（held_at が印）。"""
    o = Orchestrator.__new__(Orchestrator)
    o.approval_dir = str(tmp_path)
    _write_approval(str(tmp_path), "req-1", "t1", "pending", held=False)
    assert o._held_approval("t1") is None


def test_held_approval_ignores_other_task_and_decided(tmp_path):
    o = Orchestrator.__new__(Orchestrator)
    o.approval_dir = str(tmp_path)
    _write_approval(str(tmp_path), "req-1", "other", "pending")   # 別タスク
    _write_approval(str(tmp_path), "req-2", "t1", "approved")     # 決着済み
    assert o._held_approval("t1") is None


# ── _run_candidate: 保留は例外ではなく戻り値で返る（昇格ラダーを回さない） ──

def _candidate_orch(approval_dir, *, worker):
    o = Orchestrator.__new__(Orchestrator)
    o.approval_dir = approval_dir
    o.config = {"models": {"haiku": {"methods": ["headless"], "command": "claude"}}}
    o._resolve_workspace = lambda item: "/ws"
    o._run_worker_headless = worker
    return o


def test_run_candidate_returns_hold_on_normal_return(tmp_path):
    """worker が正常終了で返っても、保留レコードがあれば未了（hold）として扱う。

    承認をブロックされた worker は「指示どおり終わった」＝正常終了で返るため、
    出力をそのまま採用すると偽の緑になる。
    """
    async def _worker(*a, **k):
        return "途中まで書いた出力"
    o = _candidate_orch(str(tmp_path), worker=_worker)
    _write_approval(str(tmp_path), "req-1", "t1", "pending")
    res = asyncio.run(o._run_candidate({"task_id": "t1"}, "haiku", "cmd", 1, None, None, None))
    assert res == ("hold", "req-1")


def test_run_candidate_hold_takes_precedence_over_exception(tmp_path):
    """worker が例外で終わっても保留を優先する（failed に倒すと承認が届かなくなる）。"""
    async def _worker(*a, **k):
        raise RuntimeError("worker died")
    o = _candidate_orch(str(tmp_path), worker=_worker)
    _write_approval(str(tmp_path), "req-1", "t1", "pending")
    res = asyncio.run(o._run_candidate({"task_id": "t1"}, "haiku", "cmd", 1, None, None, None))
    assert res == ("hold", "req-1")


def test_run_candidate_reraises_when_not_held(tmp_path):
    """保留が無ければ例外はそのまま送出する（従来の昇格ラダーを壊さない）。"""
    async def _worker(*a, **k):
        raise RuntimeError("worker died")
    o = _candidate_orch(str(tmp_path), worker=_worker)
    try:
        asyncio.run(o._run_candidate({"task_id": "t1"}, "haiku", "cmd", 1, None, None, None))
    except RuntimeError as e:
        assert "worker died" in str(e)
    else:
        raise AssertionError("例外が送出されなかった")


def test_run_candidate_returns_ok_when_not_held(tmp_path):
    async def _worker(*a, **k):
        return "完走した出力"
    o = _candidate_orch(str(tmp_path), worker=_worker)
    res = asyncio.run(o._run_candidate({"task_id": "t1"}, "haiku", "cmd", 1, None, None, None))
    assert res == ("ok", "完走した出力")


# ── _execute_worker_task: 保留で次段モデルを呼ばない（迂回の不在） ──

def _worker_orch(hold_at=None):
    """_run_candidate を差し替えた Orchestrator。hold_at のモデルで保留を返す。"""
    o = Orchestrator.__new__(Orchestrator)
    o._notify = _noop_notify
    o._cancelled_tasks = set()  # 中止済み集合（§8.10d）。worker 実行ガードが参照する
    o.ran = []
    o.enqueued = []

    async def _enqueue(item):
        o.enqueued.append(dict(item))
    o._enqueue = _enqueue

    async def _run(item, model_name, command, step, channel, team_id, thread_ts):
        o.ran.append(model_name)
        if model_name == hold_at:
            return ("hold", "req-1")
        return ("ok", f"{model_name} output")
    o._run_candidate = _run
    return o


def _item(lane, candidates):
    loop = asyncio.get_event_loop()
    return {
        "task_id": "t1", "channel_id": None, "team_id": None, "thread_ts": None,
        "_command": "cmd", "_execution": "agent", "_depth": None, "_confidence": 0.9,
        "_model": None, "_lane": lane, "_candidates": candidates,
        "_user_specified": False, "_step": 1, "_result_future": loop.create_future(),
    }


def test_agent_lane_hold_does_not_run_next_candidate():
    """保留時に 2 番目の候補モデルの実行関数が呼ばれない（承認迂回の不在を呼び出し有無で判定）。"""
    async def _run():
        o = _worker_orch(hold_at="haiku")
        item = _item("agent", ["haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert o.ran == ["haiku"]                       # 次段 sonnet / opus は起動しない
        assert isinstance(item["_result_future"].exception(), ApprovalHold)
        assert item["_result_future"].exception().request_id == "req-1"
    asyncio.run(_run())


def test_inline_lane_hold_does_not_reinject_to_agent_lane():
    """inline レーンの保留も agent レーンへ昇格再投入しない（レーン跨ぎの迂回も塞ぐ）。"""
    async def _run():
        o = _worker_orch(hold_at="gemma")
        item = _item("inline", ["gemma", "haiku", "sonnet"])
        await o._execute_worker_task(item)
        assert o.ran == ["gemma"]
        assert o.enqueued == []                         # 再投入なし
        assert isinstance(item["_result_future"].exception(), ApprovalHold)
    asyncio.run(_run())


# ── _execute_chain: 保留で completed にせず pending_approval へ畳む ──

def _chain_orch(tmp_path, *, hold_steps=()):
    """_enqueue でワーカーを模した Orchestrator（hold_steps の step は保留を返す）。"""
    o = Orchestrator.__new__(Orchestrator)
    o._notify = _noop_notify
    o.task_dir = str(tmp_path)
    o.approval_dir = str(tmp_path / "approvals")
    o.max_reinject = 3
    o._push_task_context = lambda task: None
    o._plan_execution = lambda *a, **k: ("agent", ["haiku"], False)
    o.conversation = types.SimpleNamespace(append_task_result=lambda *a, **k: None)
    o.executed = []

    async def _enqueue(item):
        step = item["_step"]
        o.executed.append(step)
        if step in hold_steps:
            item["_result_future"].set_exception(ApprovalHold("req-1"))
        else:
            item["_result_future"].set_result(f"step{step} output")
    o._enqueue = _enqueue
    return o


_SUBTASKS = [
    {"step": 1, "command": "a", "execution": "agent", "depends_on": []},
    {"step": 2, "command": "b", "execution": "agent", "depends_on": [1]},
]


def test_chain_hold_writes_pending_approval_not_completed(tmp_path):
    """偽の緑の解消: 保留時にタスクは completed にならず、再投入の文脈が永続化される。"""
    async def _run():
        o = _chain_orch(tmp_path, hold_steps=(2,))
        task = {"task_id": "t1", "command": "元の指示", "status": "in_progress",
                "channel_id": None, "team_id": None, "thread_ts": None}
        path = _write_task(str(tmp_path), task)
        await o._execute_chain(path, task, _SUBTASKS)

        saved = json.load(open(path))                   # done/ へアーカイブされていない
        assert saved["status"] == STATUS_PENDING_APPROVAL
        assert saved["status"] != "completed"
        assert saved["held_approval_id"] == "req-1"
        assert saved["completed_steps"] == {"1": "step1 output"}   # 済んだ分だけ
        assert [s["step"] for s in saved["_plan"]] == [1, 2]       # 凍結プラン
    asyncio.run(_run())


def test_chain_reinject_skips_completed_steps(tmp_path):
    """再投入では completed_steps にある step の worker を起動せず、結果を後続へ引き渡す。"""
    async def _run():
        o = _chain_orch(tmp_path)
        task = {"task_id": "t1", "command": "元の指示", "status": "in_progress",
                "channel_id": None, "team_id": None, "thread_ts": None,
                "completed_steps": {"1": "step1 output"}}
        path = _write_task(str(tmp_path), task)
        await o._execute_chain(path, task, _SUBTASKS)

        assert o.executed == [2]                        # step 1 は再実行されない
        archived = os.path.join(str(tmp_path), "done",
                                datetime.date.today().isoformat(), "t1.json")
        saved = json.load(open(archived))
        assert saved["status"] == "completed"
    asyncio.run(_run())


# ── _resolve_hold: 決着の反映（再投入 / 却下 / 上限超過） ──

def _hold_orch(tmp_path, *, max_reinject=3):
    o = Orchestrator.__new__(Orchestrator)
    o._notify = _noop_notify
    o.task_dir = str(tmp_path)
    o.approval_dir = str(tmp_path / "approvals")
    o.max_reinject = max_reinject
    o._push_task_context = lambda task: None
    return o


def _held_task(tmp_path, **overrides):
    task = {"task_id": "t1", "command": "元の指示", "status": STATUS_PENDING_APPROVAL,
            "channel_id": None, "team_id": None, "thread_ts": None,
            "held_approval_id": "req-1", "completed_steps": {"1": "step1 output"}}
    task.update(overrides)
    return _write_task(str(tmp_path), task), task


def test_resolve_hold_waits_while_pending(tmp_path):
    """決着していない間は何もしない（人間の承認に期限は無い）。"""
    async def _run():
        o = _hold_orch(tmp_path)
        _write_approval(o.approval_dir, "req-1", "t1", "pending")
        path, task = _held_task(tmp_path)
        await o._resolve_hold(path, task)
        assert json.load(open(path))["status"] == STATUS_PENDING_APPROVAL
        assert os.path.exists(os.path.join(o.approval_dir, "req-1.json"))
    asyncio.run(_run())


def test_resolve_hold_approved_reinjects_task(tmp_path):
    """Approve → 承認レコードを done/ へ退避し、タスクを init へ戻して再投入する。"""
    async def _run():
        o = _hold_orch(tmp_path)
        _write_approval(o.approval_dir, "req-1", "t1", "approved")
        path, task = _held_task(tmp_path)
        await o._resolve_hold(path, task)

        saved = json.load(open(path))
        assert saved["status"] == "init"                # dispatcher が拾える状態へ
        assert saved["_reinject_count"] == 1
        assert saved["held_approval_id"] == ""          # 保留の印を消す
        assert saved["completed_steps"] == {"1": "step1 output"}   # 済み結果は保持
        # 保留の解消（退避しないと再投入直後にまた保留と判定される）
        assert not os.path.exists(os.path.join(o.approval_dir, "req-1.json"))
        assert os.path.exists(os.path.join(o.approval_dir, "done", "req-1.json"))
    asyncio.run(_run())


def test_resolve_hold_rejected_fails_task(tmp_path):
    async def _run():
        o = _hold_orch(tmp_path)
        _write_approval(o.approval_dir, "req-1", "t1", "rejected")
        path, task = _held_task(tmp_path)
        await o._resolve_hold(path, task)

        archived = os.path.join(str(tmp_path), "done",
                                datetime.date.today().isoformat(), "t1.json")
        saved = json.load(open(archived))
        assert saved["status"] == "failed"
        assert "却下" in saved["result"]
    asyncio.run(_run())


def test_resolve_hold_stops_after_max_reinject(tmp_path):
    """承認と再投入が循環して収束しない場合は max_reinject 超過で failed。"""
    async def _run():
        o = _hold_orch(tmp_path, max_reinject=2)
        _write_approval(o.approval_dir, "req-1", "t1", "approved")
        path, task = _held_task(tmp_path, _reinject_count=2)   # 次で 3 回目＝上限超過
        await o._resolve_hold(path, task)

        archived = os.path.join(str(tmp_path), "done",
                                datetime.date.today().isoformat(), "t1.json")
        saved = json.load(open(archived))
        assert saved["status"] == "failed"
        assert "max_reinject" in saved["result"]
    asyncio.run(_run())


def test_resolve_hold_archives_leftover_held_records(tmp_path):
    """同一タスクに保留レコードが 2 件できても、決着時に両方退避して取り残しを作らない。

    同じ猶予ウィンドウ内で 2 つのサブタスクが並行して Tier3 に到達すると、どちらも
    冪等チェックで相手を見つけられずレコードが 2 件できる。1 件しか退避しないと、
    再投入した瞬間にもう 1 件が `_held_approval` に拾われてまた畳まれる。
    """
    async def _run():
        o = _hold_orch(tmp_path)
        _write_approval(o.approval_dir, "req-1", "t1", "approved")   # 人間が押した方
        _write_approval(o.approval_dir, "req-2", "t1", "pending")    # 並行して立った方
        path, task = _held_task(tmp_path)
        await o._resolve_hold(path, task)

        assert json.load(open(path))["status"] == "init"
        for rid in ("req-1", "req-2"):
            assert not os.path.exists(os.path.join(o.approval_dir, f"{rid}.json"))
            assert os.path.exists(os.path.join(o.approval_dir, "done", f"{rid}.json"))
        assert o._held_approval("t1") is None       # 再投入直後に保留と誤判定されない
    asyncio.run(_run())


def test_resolve_hold_corrupt_reinject_count_stops_instead_of_looping(tmp_path):
    """再投入回数が数値でない（破損・手編集）ときは上限超過として締める。

    例外を上へ投げると保留ループが毎ポーリング（既定 1 秒）同じタスクで失敗し続け、
    スタックトレースでログが埋まる。0 に倒すと上限を素通りして循環が止まらない。
    """
    async def _run():
        o = _hold_orch(tmp_path)
        _write_approval(o.approval_dir, "req-1", "t1", "approved")
        path, task = _held_task(tmp_path, _reinject_count="こわれた値")
        await o._resolve_hold(path, task)

        archived = os.path.join(str(tmp_path), "done",
                                datetime.date.today().isoformat(), "t1.json")
        saved = json.load(open(archived))
        assert saved["status"] == "failed"
        assert "max_reinject" in saved["result"]
    asyncio.run(_run())


def test_resolve_hold_missing_record_fails_task(tmp_path):
    """承認レコードが消えていれば誰も決着させられない → 放置せず failed で締める。"""
    async def _run():
        o = _hold_orch(tmp_path)
        os.makedirs(o.approval_dir, exist_ok=True)
        path, task = _held_task(tmp_path)
        await o._resolve_hold(path, task)

        archived = os.path.join(str(tmp_path), "done",
                                datetime.date.today().isoformat(), "t1.json")
        assert json.load(open(archived))["status"] == "failed"
    asyncio.run(_run())
