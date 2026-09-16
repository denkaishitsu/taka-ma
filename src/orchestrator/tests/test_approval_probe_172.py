"""承認スレッドでの実測回答（設計書 §3.3 (5) (b)・#172 probe_approval）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
  - thread_ts ⇄ 承認レコードの決定的照合（保留中 → 実測ブロック・該当なし → None）
  - ブロックは承認レコード＋現在の照合（ハッシュ再計測）のみから機械組立
  - 決着直後（60 分内）の追い質問には決着状態（TOCTOU ブロック含む）を返す
  - 1 問ごとに読み直す（複数回の問答が成り立つ）
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import hashlib
import json
import os
import datetime

from orchestrator.conversation import ConversationManager


def _mgr(approval_dir, files: dict, task_dir=None):
    """probe_approval に必要な属性だけ持つ部分構築の ConversationManager。"""
    mgr = ConversationManager.__new__(ConversationManager)
    mgr._approval_dir = str(approval_dir)
    if task_dir is not None:
        mgr.task_dir = str(task_dir)

    class _PM:
        @staticmethod
        def run_ssh_probe(command, timeout=15):
            for path, content in files.items():
                if command == f"shasum -a 256 {path}":
                    if content is None:
                        return (1, "")
                    return (0, hashlib.sha256(content.encode()).hexdigest()
                            + f"  {path}")
            return (1, "")
    mgr.process_mgr = _PM()
    return mgr


def _record(request_id="req1", thread="111.222", status="pending", *,
            sha, decided_at=None, extra=None):
    rec = {"request_id": request_id, "task_id": "t1", "command": "bash setup.sh",
           "tool_name": "Bash", "thread_ts": thread, "status": status,
           "risk_reason": "high", "created_at": "2026-09-16T00:00:00+00:00",
           "decided_at": decided_at, "decided_by": "tester",
           "evidence": {"files": [{"path": "/ws/setup.sh", "sha256": sha,
                                   "excerpt": "1\techo hi", "truncated": False}],
                        "notes": []}}
    rec.update(extra or {})
    return rec


def test_pending_record_yields_measured_block(tmp_path):
    content = "echo hi"
    sha = hashlib.sha256(content.encode()).hexdigest()
    with open(tmp_path / "req1.json", "w") as f:
        json.dump(_record(sha=sha), f)
    mgr = _mgr(tmp_path, {"/ws/setup.sh": content})
    block = mgr._approval_probe_block({"thread_ts": "111.222"})
    assert block is not None
    assert "承認リクエストの実測" in block and "bash setup.sh" in block
    assert "一致（承認時から変更なし）" in block
    assert "機械的に組み立てています" in block
    # task_dir 未構成 → 契約欄なしを明示（無印にしない）
    assert "契約: タスクファイルを引けません" in block


def test_block_includes_contract_fields(tmp_path):
    """契約の該当欄（依頼・拘束・完了条件）が task_id 経由で併記される（§3.3 (5) (b)）。"""
    content = "echo hi"
    sha = hashlib.sha256(content.encode()).hexdigest()
    approvals = tmp_path / "approvals"
    approvals.mkdir()
    with open(approvals / "req1.json", "w") as f:
        json.dump(_record(sha=sha), f)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    with open(tasks / "20260916_t1.json", "w") as f:
        json.dump({"task_id": "t1",
                   "command": "docs/spec.md の TODO-A を置き換える",
                   "constraints": [{"text": "テストや検証は行わない", "forbid": True}],
                   "acceptance": [{"kind": "file_changed",
                                   "params": {"path": "docs/spec.md"}}]}, f)
    mgr = _mgr(approvals, {"/ws/setup.sh": content}, task_dir=tasks)
    block = mgr._approval_probe_block({"thread_ts": "111.222"})
    assert "依頼: docs/spec.md の TODO-A を置き換える" in block
    assert "拘束条件: （禁止）テストや検証は行わない" in block
    assert "完了条件: file_changed docs/spec.md" in block
    assert "契約: タスクファイルを引けません" not in block


def test_pending_block_flags_premature_change(tmp_path):
    with open(tmp_path / "req1.json", "w") as f:
        json.dump(_record(sha="approvedhash"), f)
    mgr = _mgr(tmp_path, {"/ws/setup.sh": "changed!"})
    block = mgr._approval_probe_block({"thread_ts": "111.222"})
    assert "不一致（承認時から変更あり）" in block


def test_no_match_returns_none(tmp_path):
    with open(tmp_path / "req1.json", "w") as f:
        json.dump(_record(sha="h", thread="999.000"), f)
    mgr = _mgr(tmp_path, {})
    assert mgr._approval_probe_block({"thread_ts": "111.222"}) is None
    assert mgr._approval_probe_block({"thread_ts": None}) is None


def test_recent_decided_toctou_block(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rec = _record(sha="h", status="approved", decided_at=now,
                  extra={"toctou_mismatch": True,
                         "toctou_report": "/opt/x/req1.toctou.diff"})
    with open(done / "req1.json", "w") as f:
        json.dump(rec, f)
    mgr = _mgr(tmp_path, {"/ws/setup.sh": None})
    block = mgr._approval_probe_block({"thread_ts": "111.222"})
    assert "実行せず" in block and "req1.toctou.diff" in block


def test_old_decided_record_is_ignored(tmp_path):
    done = tmp_path / "done"
    done.mkdir()
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=2)).isoformat()
    with open(done / "req1.json", "w") as f:
        json.dump(_record(sha="h", status="approved", decided_at=old), f)
    mgr = _mgr(tmp_path, {})
    assert mgr._approval_probe_block({"thread_ts": "111.222"}) is None


def test_reread_each_question(tmp_path):
    """1 問ごとに読み直す＝レコードの変化が次の回答へ反映される（複数回の問答）。"""
    content = "echo hi"
    sha = hashlib.sha256(content.encode()).hexdigest()
    path = tmp_path / "req1.json"
    with open(path, "w") as f:
        json.dump(_record(sha=sha), f)
    mgr = _mgr(tmp_path, {"/ws/setup.sh": content})
    assert "保留中" in mgr._approval_probe_block({"thread_ts": "111.222"})
    # 決着してレコードが done/ へ動いた後の質問 → 決着ブロックへ切り替わる
    done = tmp_path / "done"
    done.mkdir()
    rec = _record(sha=sha, status="approved",
                  decided_at=datetime.datetime.now(
                      datetime.timezone.utc).isoformat())
    os.remove(path)
    with open(done / "req1.json", "w") as f:
        json.dump(rec, f)
    block2 = mgr._approval_probe_block({"thread_ts": "111.222"})
    assert "approved" in block2 and "保留中" not in block2
