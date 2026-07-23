"""Task #126 昇格ラダーの worker 実行振る舞いテスト。

_execute_worker_task / _escalate_to_agent_lane の分岐（inline 失敗→agent レーン再投入、
ESCALATE 自己申告→次段昇格、明示指定は昇格しない、agent レーンの逐次昇格、候補皆無で
恒久ハングせず例外解決）を、_run_candidate をスタブ化して分離実行で担保する。設計書 §2.2。

Orchestrator の重い依存（SSH/モデル）は使わないよう __new__ で生成し、_run_candidate と
_enqueue と _notify だけを差し替える。
"""
import asyncio
import types

import pytest

from orchestrator import Orchestrator


def _orch():
    """__init__ を通さず、昇格分岐に必要な最小限だけ差し込んだ Orchestrator。"""
    o = Orchestrator.__new__(Orchestrator)
    o._notify = _noop_notify
    o.enqueued = []
    async def _fake_enqueue(item):
        o.enqueued.append(dict(item))  # 再投入時点のスナップショットを残す
    o._enqueue = _fake_enqueue
    return o


async def _noop_notify(*a, **k):
    return None


def _item(lane, candidates, *, user_specified=False, model=None):
    loop = asyncio.get_event_loop()
    return {
        "task_id": "t1", "channel_id": None, "team_id": None, "thread_ts": None,
        "_command": "cmd", "_execution": ("inline" if lane == "inline" else "agent"),
        "_depth": None, "_confidence": 0.9, "_model": model,
        "_lane": lane, "_candidates": candidates, "_user_specified": user_specified,
        "_step": 1, "_result_future": loop.create_future(),
    }


def _stub_run(o, behavior):
    """_run_candidate を model_name → 挙動（str=出力 / Exception=送出）で差し替える。

    呼ばれた model_name を o.ran に順に記録する。behavior は dict または callable。
    """
    o.ran = []

    async def _run(item, model_name, command, step, channel, team_id, thread_ts):
        o.ran.append(model_name)
        result = behavior(model_name) if callable(behavior) else behavior[model_name]
        if isinstance(result, Exception):
            raise result
        return result
    o._run_candidate = _run


# ── inline レーン ──

def test_inline_success_no_escalation():
    async def _run():
        o = _orch()
        _stub_run(o, {"gemma": "OK result"})
        item = _item("inline", ["gemma", "haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].result() == "OK result"
        assert o.ran == ["gemma"]          # gemma だけ実行
        assert o.enqueued == []            # 再投入なし
    asyncio.run(_run())


def test_inline_failure_escalates_to_agent_lane():
    async def _run():
        o = _orch()
        _stub_run(o, {"gemma": RuntimeError("ollama down")})
        item = _item("inline", ["gemma", "haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        # future はまだ未解決（agent レーンへ再投入して継続）
        assert not item["_result_future"].done()
        assert len(o.enqueued) == 1
        re = o.enqueued[0]
        assert re["_lane"] == "agent"                      # レーン跨ぎ
        assert re["_execution"] == "agent"
        assert re["_candidates"] == ["haiku", "sonnet", "opus"]  # gemma を除いた残段
    asyncio.run(_run())


def test_inline_escalate_marker_escalates_to_agent_lane():
    async def _run():
        o = _orch()
        _stub_run(o, {"gemma": "部分的な結果\nESCALATE: ツールが要る"})
        item = _item("inline", ["gemma", "haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert not item["_result_future"].done()
        assert o.enqueued[0]["_candidates"] == ["haiku", "sonnet", "opus"]
    asyncio.run(_run())


def test_inline_user_specified_no_escalation_on_failure():
    """:gemma 明示指定は失敗しても昇格せず例外解決。"""
    async def _run():
        o = _orch()
        _stub_run(o, {"gemma": RuntimeError("boom")})
        item = _item("inline", ["gemma"], user_specified=True, model="gemma")
        await o._execute_worker_task(item)
        assert item["_result_future"].exception() is not None
        assert o.enqueued == []
    asyncio.run(_run())


# ── agent レーン（逐次昇格）──

def test_agent_first_success():
    async def _run():
        o = _orch()
        _stub_run(o, {"opus": "done"})
        item = _item("agent", ["opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].result() == "done"
        assert o.ran == ["opus"]
    asyncio.run(_run())


def test_agent_escalates_on_exception_then_succeeds():
    async def _run():
        o = _orch()
        _stub_run(o, {"haiku": RuntimeError("fail"), "sonnet": "recovered"})
        item = _item("agent", ["haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].result() == "recovered"
        assert o.ran == ["haiku", "sonnet"]   # opus まで行かず sonnet で回復
    asyncio.run(_run())


def test_agent_escalates_on_marker_then_succeeds():
    async def _run():
        o = _orch()
        _stub_run(o, {"haiku": "軽い試行\nESCALATE: 難所", "sonnet": "解決"})
        item = _item("agent", ["haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].result() == "解決"
        assert o.ran == ["haiku", "sonnet"]
    asyncio.run(_run())


def test_agent_all_fail_sets_exception():
    async def _run():
        o = _orch()
        _stub_run(o, lambda m: RuntimeError(f"{m} down"))
        item = _item("agent", ["haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].exception() is not None
        assert o.ran == ["haiku", "sonnet", "opus"]   # 全段試行
    asyncio.run(_run())


def test_agent_top_level_escalate_marker_fails():
    """最上位 opus が ESCALATE 申告 → 昇格先なし → failed。"""
    async def _run():
        o = _orch()
        _stub_run(o, {"opus": "無理\nESCALATE: 解決不能"})
        item = _item("agent", ["opus"])
        await o._execute_worker_task(item)
        assert item["_result_future"].exception() is not None
    asyncio.run(_run())


def test_agent_user_specified_no_escalation():
    """:sonnet 明示指定は失敗しても次段へ行かない。"""
    async def _run():
        o = _orch()
        _stub_run(o, {"sonnet": RuntimeError("boom")})
        item = _item("agent", ["sonnet"], user_specified=True, model="sonnet")
        await o._execute_worker_task(item)
        assert item["_result_future"].exception() is not None
        assert o.ran == ["sonnet"]
    asyncio.run(_run())


# ── 候補皆無（matrix 不備）で恒久ハングしない ──

def test_empty_candidates_resolves_exception():
    async def _run():
        o = _orch()
        _stub_run(o, {})
        item = _item("agent", [])
        await o._execute_worker_task(item)
        assert item["_result_future"].exception() is not None  # ハングせず例外解決
    asyncio.run(_run())
