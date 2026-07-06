"""audit_approval.record_audit_approval の単体テスト。

Approve が LLM 経路（enqueue_task）を経ず、承認証跡 jsonl へ機械的に追記されることを検証する。
"""

import json
import os
import tempfile

from services.audit_approval import record_audit_approval


def test_records_approval_line():
    """承認 1 件が 1 行の jsonl として追記され、識別キーと承認者が残る。"""
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "sub", "approvals.jsonl")  # 親ディレクトリ自動生成も検証
        record = {"path": "/opt/taka-ma/work/t1/haiku.md", "task_id": "t1", "channel_id": "C1"}
        assert record_audit_approval("aud-1", record, "U123", approval_log=log) is True
        lines = open(log).read().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["audit_log_id"] == "aud-1"
        assert entry["path"] == "/opt/taka-ma/work/t1/haiku.md"
        assert entry["task_id"] == "t1"
        assert entry["decision"] == "approved"
        assert entry["decided_by"] == "U123"
        assert "decided_at" in entry


def test_appends_without_overwrite():
    """複数承認は追記され、既存行を上書きしない。"""
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "approvals.jsonl")
        record_audit_approval("aud-1", {"path": "a", "task_id": "t1"}, "U1", approval_log=log)
        record_audit_approval("aud-2", {"path": "b", "task_id": "t2"}, "U2", approval_log=log)
        lines = open(log).read().strip().splitlines()
        assert [json.loads(l)["audit_log_id"] for l in lines] == ["aud-1", "aud-2"]


def test_missing_fields_default_empty():
    """アラートレコードにキー欠落があっても空文字で埋めて記録する（落とさない）。"""
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "approvals.jsonl")
        assert record_audit_approval("aud-3", {}, "U9", approval_log=log) is True
        entry = json.loads(open(log).read().strip())
        assert entry["path"] == "" and entry["task_id"] == ""
