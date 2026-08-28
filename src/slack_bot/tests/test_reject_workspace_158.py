"""§8.10g (4): file_audit Reject の revert タスクへの workspace 継承の検証。

2026-08-27 実測: Reject が投入する revert タスクが workspace を持たず、sa-ru が既定の
捨て作業場を割り当てて実リポジトリを見失った。アラートの workspace を enqueue_task まで
運ぶ配線と、旧アラート（workspace 無し）での縮退動作を確認する。
"""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from handlers import actions  # noqa: E402
from services import task_queue  # noqa: E402


def _enqueue_and_load(tmp_path, monkeypatch, record):
    monkeypatch.setattr(task_queue, "TASK_DIR", str(tmp_path))
    task_id = actions._enqueue_audit_reject_task(record, user="U1", team_id="T1")
    files = [f for f in os.listdir(tmp_path) if f.endswith(".json")]
    assert len(files) == 1
    with open(os.path.join(tmp_path, files[0])) as f:
        task = json.load(f)
    assert task["task_id"] == task_id
    return task


def test_reject_task_inherits_workspace(tmp_path, monkeypatch):
    record = {"path": "/Users/u/DevDev/repo/docs/02-basic-design.md",
              "workspace": "/Users/u/DevDev/repo",
              "channel_id": "C1", "thread_ts": "123.456"}
    task = _enqueue_and_load(tmp_path, monkeypatch, record)
    assert task["workspace"] == "/Users/u/DevDev/repo"
    assert task["source"] == "slack_action"


def test_reject_task_without_workspace_degrades(tmp_path, monkeypatch):
    """workspace の無い旧アラートはキー自体を持たせない（従来動作へ縮退）。"""
    record = {"path": "/Users/u/DevDev/repo/docs/x.md", "channel_id": "C1"}
    task = _enqueue_and_load(tmp_path, monkeypatch, record)
    assert "workspace" not in task
