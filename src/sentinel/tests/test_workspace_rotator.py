"""workspace rotation（§8.13 retention 削除）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
- rotate_workspaces: 終了済み＋retention 超過のみ削除／実行中・新しい・orphan は不可侵／
  workspace_base 外（repo: 指定）と symlink 脱出は実体を消さない／壊れレコードで止まらない
- FileAuditHandler.suppress_subtree: 抑制中 subtree の監査除外と TTL 失効
"""

import asyncio
import datetime
import json
import os
import time

from file_auditor import FileAuditHandler
from workspace_rotator import rotate_workspaces


def _write_record(ctx_dir, task_id, status, workspace, *, age_days=0):
    """task_context レコードを作り、mtime を age_days 日前に偽装する。"""
    path = os.path.join(ctx_dir, f"{task_id}.json")
    with open(path, "w") as f:
        json.dump({"task_id": task_id, "status": status, "workspace": workspace}, f)
    if age_days:
        past = (datetime.datetime.now() - datetime.timedelta(days=age_days)).timestamp()
        os.utime(path, (past, past))
    return path


def _setup(tmp_path):
    ctx_dir = str(tmp_path / "task-context")
    base = str(tmp_path / "work")
    os.makedirs(ctx_dir)
    os.makedirs(base)
    return ctx_dir, base


def _make_ws(base, task_id):
    ws = os.path.join(base, task_id)
    os.makedirs(ws)
    with open(os.path.join(ws, "artifact.txt"), "w") as f:
        f.write("x")
    return ws


# ── 削除する側 ──

def test_completed_over_retention_deletes_workspace_and_record(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "t1")
    rec = _write_record(ctx_dir, "t1", "completed", ws, age_days=31)
    suppressed = []
    rotate_workspaces(ctx_dir, base, 30, on_before_delete=suppressed.append)
    assert not os.path.exists(ws)
    assert not os.path.exists(rec)
    # 抑制宣言（file_audit への自己操作宣言）が rmtree 対象パスに対して行われている
    assert suppressed == [ws]


def test_failed_status_is_also_rotated(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "t2")
    _write_record(ctx_dir, "t2", "failed", ws, age_days=31)
    rotate_workspaces(ctx_dir, base, 30)
    assert not os.path.exists(ws)


def test_record_without_existing_workspace_is_removed(tmp_path):
    # repo: 運用等で dir が無い/既に消えている場合でもレコードだけは掃除される
    ctx_dir, base = _setup(tmp_path)
    rec = _write_record(ctx_dir, "t3", "completed",
                        os.path.join(base, "t3"), age_days=31)
    rotate_workspaces(ctx_dir, base, 30)
    assert not os.path.exists(rec)


# ── 削除しない側（安全弁） ──

def test_within_retention_is_kept(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "t4")
    rec = _write_record(ctx_dir, "t4", "completed", ws, age_days=10)
    rotate_workspaces(ctx_dir, base, 30)
    assert os.path.exists(ws)
    assert os.path.exists(rec)


def test_in_progress_is_never_deleted(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "t5")
    rec = _write_record(ctx_dir, "t5", "in_progress", ws, age_days=90)
    rotate_workspaces(ctx_dir, base, 30)
    assert os.path.exists(ws)
    assert os.path.exists(rec)


def test_active_store_overrides_completed_record(tmp_path):
    # レコード上は終了でもメモリ store が実行中と言うなら store 優先で見送る
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "t6")
    _write_record(ctx_dir, "t6", "completed", ws, age_days=31)
    rotate_workspaces(ctx_dir, base, 30, active_task_ids={"t6"})
    assert os.path.exists(ws)


def test_workspace_outside_base_is_not_deleted(tmp_path):
    # repo: 明示指定の実開発リポジトリ（workspace_base 外）は実体不可侵・レコードのみ掃除
    ctx_dir, base = _setup(tmp_path)
    repo = str(tmp_path / "dev-repo")
    os.makedirs(repo)
    rec = _write_record(ctx_dir, "t7", "completed", repo, age_days=31)
    rotate_workspaces(ctx_dir, base, 30)
    assert os.path.exists(repo)
    assert not os.path.exists(rec)


def test_symlink_escape_is_not_deleted(tmp_path):
    # {base}/{task_id} が外部への symlink でも realpath 判定で実体を消さない
    ctx_dir, base = _setup(tmp_path)
    target = str(tmp_path / "outside")
    os.makedirs(target)
    link = os.path.join(base, "t8")
    os.symlink(target, link)
    _write_record(ctx_dir, "t8", "completed", link, age_days=31)
    rotate_workspaces(ctx_dir, base, 30)
    assert os.path.exists(target)


def test_orphan_dir_without_record_is_kept(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    ws = _make_ws(base, "no-record")
    rotate_workspaces(ctx_dir, base, 30)
    assert os.path.exists(ws)


def test_broken_record_does_not_stop_rotation(tmp_path):
    ctx_dir, base = _setup(tmp_path)
    with open(os.path.join(ctx_dir, "aaa-broken.json"), "w") as f:
        f.write("{ not json")
    ws = _make_ws(base, "t9")
    _write_record(ctx_dir, "t9", "completed", ws, age_days=31)
    rotate_workspaces(ctx_dir, base, 30)
    # 壊れレコード（走査順で先）を踏んでも後続の正常レコードは処理される
    assert not os.path.exists(ws)


# ── FileAuditHandler.suppress_subtree（自己操作の監査抑制） ──

def _handler(tmp):
    """最小 config で FileAuditHandler を組む（suppress 判定のみ検証）。"""
    config = {
        "file_audit": {
            "ignore_patterns": [],
            "log_dir": os.path.join(tmp, "logs"),
            "o_moi_alert_dir": os.path.join(tmp, "alerts"),
            "mac_mini_host": "mac-mini",
            "debounce_sec": 1,
            "control_plane_files": [".taka-hook-settings.json"],
        },
        "task_context": {"dir": os.path.join(tmp, "task-context")},
    }
    loop = asyncio.new_event_loop()
    try:
        return FileAuditHandler(config, reviewer=None, task_context_store={}, loop=loop)
    finally:
        loop.close()


def test_suppressed_subtree_is_ignored(tmp_path):
    h = _handler(str(tmp_path))
    ws = str(tmp_path / "work" / "t1")
    os.makedirs(ws)
    inner = os.path.join(ws, "src", "a.py")
    assert not h._should_ignore(inner)
    h.suppress_subtree(ws, ttl_sec=60)
    assert h._should_ignore(inner)
    # subtree 外は抑制されない
    assert not h._should_ignore(str(tmp_path / "work" / "t2" / "b.py"))


def test_suppression_expires_after_ttl(tmp_path):
    h = _handler(str(tmp_path))
    ws = str(tmp_path / "work" / "t1")
    os.makedirs(ws)
    h.suppress_subtree(ws, ttl_sec=0.05)
    time.sleep(0.1)
    assert not h._should_ignore(os.path.join(ws, "a.py"))
