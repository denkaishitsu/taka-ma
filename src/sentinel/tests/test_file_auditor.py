"""FileAuditHandler の除外判定・event 集約の単体テスト。

- `_merge_event`: atomic write（delete↔create/moved 共起）を modify に畳み、
  真の削除（delete のみ）は残す（§8.12 原子的書き込みの集約）。
- `_should_ignore`: システム制御プレーン（sa-ru 生成の制御ファイル）を除外し、
  `.gitignore` は常に監査、ユーザー成果物は監査対象に残す（§8.12）。
"""

import asyncio
import os
import tempfile

from file_auditor import FileAuditHandler


def _handler(tmp, *, ignore_patterns=None, control_plane_files=None, task_context_dir=None):
    """最小 config で FileAuditHandler を組む（reviewer は使わないメソッドのみ検証）。

    #103 yaml SSOT 化で control_plane_files / task_context.dir も必須キーになったため、
    未指定時は qu-e.yaml の実効値と同値（制御ファイル名）／どのテストパスとも交差しない
    dir を与える（旧コード既定値と同じ振る舞いを保つ）。
    """
    fa = {
        "ignore_patterns": ignore_patterns if ignore_patterns is not None else [],
        "log_dir": os.path.join(tmp, "logs"),
        "o_moi_alert_dir": os.path.join(tmp, "alerts"),
        "mac_mini_host": "mac-mini",
        "debounce_sec": 1,
        "control_plane_files": (control_plane_files if control_plane_files is not None
                                else [".taka-hook-settings.json"]),
    }
    config = {"file_audit": fa,
              "task_context": {"dir": (task_context_dir if task_context_dir is not None
                                       else os.path.join(tmp, "task-context"))}}
    loop = asyncio.new_event_loop()
    try:
        return FileAuditHandler(config, reviewer=None, task_context_store={}, loop=loop)
    finally:
        loop.close()


# ── _merge_event（staticmethod・§8.12 原子的書き込みの集約） ──

def test_merge_first_event_returns_itself():
    assert FileAuditHandler._merge_event(None, "created") == "created"
    assert FileAuditHandler._merge_event(None, "deleted") == "deleted"


def test_merge_delete_then_create_is_modified():
    """tmp→本体 rename や 削除→再作成: delete の後に新規実体の出現（created/moved）→ modify に畳む。"""
    assert FileAuditHandler._merge_event("deleted", "moved") == "modified"
    assert FileAuditHandler._merge_event("deleted", "created") == "modified"


def test_merge_create_then_delete_is_modified():
    """順序が逆（存在→delete がウィンドウ内共起）でも削除アラート化させない。"""
    assert FileAuditHandler._merge_event("created", "deleted") == "modified"
    assert FileAuditHandler._merge_event("moved", "deleted") == "modified"


def test_merge_delete_only_stays_deleted():
    """存在イベントが来ない delete のみ → 真の削除として残す。"""
    assert FileAuditHandler._merge_event("deleted", "deleted") == "deleted"


def test_merge_edit_then_delete_stays_deleted():
    """編集→即削除（modified→deleted）は modify に畳まず削除を保全する（真の削除を隠さない）。"""
    assert FileAuditHandler._merge_event("modified", "deleted") == "deleted"
    # created/moved（新規実体の出現）との共起だけが modify 集約の対象
    assert FileAuditHandler._merge_event("created", "deleted") == "modified"
    assert FileAuditHandler._merge_event("moved", "deleted") == "modified"


def test_merge_exists_events_take_latest():
    assert FileAuditHandler._merge_event("created", "modified") == "modified"
    assert FileAuditHandler._merge_event("modified", "moved") == "moved"


# ── _should_ignore（§8.12 除外判定） ──

def test_control_plane_file_ignored():
    """sa-ru が workspace に配る制御ファイルは basename で除外（自己生成物）。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp)  # 既定 control_plane_files = [".taka-hook-settings.json"]
        assert h._should_ignore("/opt/taka-ma/work/task-1/.taka-hook-settings.json") is True
        # 別タスクの workspace でも同名なら除外
        assert h._should_ignore("/opt/taka-ma/work/task-2/.taka-hook-settings.json") is True


def test_user_artifact_not_ignored():
    """ユーザー成果物は監査対象に残す（制御ファイルと同ディレクトリでも）。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp)
        assert h._should_ignore("/opt/taka-ma/work/task-1/haiku.md") is False


def test_gitignore_always_audited():
    """`.gitignore` 自身の変更は除外ルール書き換えなので常に監査（除外しない）。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp)
        assert h._should_ignore("/opt/taka-ma/work/task-1/.gitignore") is False


def test_task_context_dir_ignored():
    """task_context ディレクトリ配下（qu-e 制御プレーン）は絶対パス prefix で除外。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp, task_context_dir="/opt/taka-ma/data/task-context")
        assert h._should_ignore("/opt/taka-ma/data/task-context/task-1.json") is True


def test_ignore_patterns_applied():
    """固定無視パターン（*.log 等）は従来どおり効く。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp, ignore_patterns=["*.log"])
        assert h._should_ignore("/opt/taka-ma/work/task-1/run.log") is True
        assert h._should_ignore("/opt/taka-ma/work/task-1/note.txt") is False


def test_control_plane_files_configurable():
    """config の control_plane_files で除外対象名を差し替えられる。"""
    with tempfile.TemporaryDirectory() as tmp:
        h = _handler(tmp, control_plane_files=[".custom-control"])
        assert h._should_ignore("/opt/taka-ma/work/task-1/.custom-control") is True
        # 既定名は差し替えたので監査対象に戻る
        assert h._should_ignore("/opt/taka-ma/work/task-1/.taka-hook-settings.json") is False
