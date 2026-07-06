"""task-86（チェーン future 未解決ハング一掃）の振る舞いテスト。

grep/AST では潰せない振る舞い（循環でデッドロックしない・依存先失敗で後続が永久 await せず
skip される・cleanup 失敗で例外が外へ漏れない）を、分離実行で担保する。設計書 §10.3 / §10.7。

Orchestrator の __init__ はモデル/SSH/config 一式を要求するため、__new__ で本体を作らず
対象メソッドだけを直接呼ぶ（検査対象メソッドは self の重い依存を使わない設計）。
"""
import asyncio
import os
import shutil
import types

import pytest

from orchestrator import Orchestrator


def _bare():
    """__init__ を通さず Orchestrator インスタンスを得る（重い依存の構築を回避）。"""
    o = Orchestrator.__new__(Orchestrator)
    o.slack = types.SimpleNamespace(notify=lambda *a, **k: None)
    return o


# ── H4/H5: _validate_subtask_graph（実行前検証・設計書 §10.3「実行前検証」） ──

def test_validate_detects_duplicate_step():
    o = _bare()
    subtasks = [
        {"step": 1, "command": "a", "category": "light", "depends_on": []},
        {"step": 1, "command": "b", "category": "light", "depends_on": []},
    ]
    err = o._validate_subtask_graph(subtasks)
    assert err is not None and "重複" in err


def test_validate_detects_self_dependency():
    o = _bare()
    subtasks = [{"step": 1, "command": "a", "category": "light", "depends_on": [1]}]
    err = o._validate_subtask_graph(subtasks)
    assert err is not None and "自分自身" in err


def test_validate_detects_cycle():
    o = _bare()
    subtasks = [
        {"step": 1, "command": "a", "category": "light", "depends_on": [2]},
        {"step": 2, "command": "b", "category": "light", "depends_on": [1]},
    ]
    err = o._validate_subtask_graph(subtasks)
    assert err is not None and "循環" in err


def test_validate_accepts_valid_dag():
    o = _bare()
    subtasks = [
        {"step": 1, "command": "a", "category": "heavy", "depends_on": []},
        {"step": 2, "command": "b", "category": "heavy", "depends_on": []},
        {"step": 3, "command": "c", "category": "light", "depends_on": [1, 2]},
    ]
    assert o._validate_subtask_graph(subtasks) is None


def test_validate_ignores_dangling_dependency():
    """存在しない step への依存は失敗にしない（実行時無視と揃える・設計書 §10.3）。"""
    o = _bare()
    subtasks = [{"step": 1, "command": "a", "category": "light", "depends_on": [99]}]
    assert o._validate_subtask_graph(subtasks) is None


# ── H1: 依存先失敗で後続が永久 await せず futures[step] が例外解決される ──

def test_cascading_skip_resolves_future_not_hang():
    async def _run():
        o = _bare()
        loop = asyncio.get_running_loop()
        dep_future = loop.create_future()
        dep_future.set_exception(RuntimeError("依存先失敗"))
        futures = {1: dep_future, 2: loop.create_future()}
        results = {}
        task = {"task_id": "t", "channel_id": None, "team_id": None, "thread_ts": None}
        subtask = {"step": 2, "command": "x", "category": "light", "depends_on": [1]}
        # 依存先が失敗しているため enqueue 前に skip され、futures[2] が例外で解決されるはず。
        # 5 秒で終わらなければ「永久 await（ハング）」の退行とみなす。
        await asyncio.wait_for(
            o._execute_subtask_in_chain(task, subtask, results, futures, None),
            timeout=5)
        assert futures[2].done()
        assert futures[2].exception() is not None
    asyncio.run(_run())


# ── F15: _daily_cleanup は OSError を外へ送出しない（dispatcher ライブロック防止・§10.7） ──

def test_daily_cleanup_swallows_listdir_oserror(tmp_path, monkeypatch):
    o = _bare()
    o.config = {"cleanup": {"retention_days": 90}}
    o.task_dir = str(tmp_path)
    done = tmp_path / "done"
    done.mkdir()

    def _boom(_):
        raise OSError("listdir denied")

    monkeypatch.setattr(os, "listdir", _boom)
    # 例外が送出されれば dispatcher が落ちる → ここで raise しないことを確認
    asyncio.run(o._daily_cleanup())


def test_daily_cleanup_swallows_rmtree_oserror(tmp_path, monkeypatch):
    o = _bare()
    o.config = {"cleanup": {"retention_days": 90}}
    o.task_dir = str(tmp_path)
    done = tmp_path / "done"
    done.mkdir()
    (done / "2000-01-01").mkdir()  # retention 超過 → 削除対象

    def _boom(_):
        raise OSError("rmtree denied")

    monkeypatch.setattr(shutil, "rmtree", _boom)
    asyncio.run(o._daily_cleanup())
