"""実開発リポジトリ監査（§8.12 動的監視・コミット前ゲート）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
- DynamicWatchManager: task_context の workspace による動的登録／参照カウント解除／
  静的ルート配下のスキップ／登録失敗の escalate（fail-closed）／pre-commit フック自動導入
- commit_audit_cli: staged diff の審査結果正規化（approve のみ通す fail-closed）と
  監査 jsonl（event="commit"）への記録
"""

import json
import os
import stat
import subprocess
import sys

import pytest

# commit_audit_cli は運用時と同じ `sentinel.` パッケージ import（PYTHONPATH=/opt/taka-ma/qu-e
# 相当）を使うため、conftest の src/sentinel に加えて親の src/ もパスに通す
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import commit_audit_cli  # noqa: E402
from file_auditor import DynamicWatchManager  # noqa: E402


# ── テスト用スタブ ──

class _StubObserver:
    """schedule/unschedule の呼び出しを記録する watchdog Observer 代替。"""

    def __init__(self, fail_schedule=False):
        self.scheduled = []      # (handler, path, recursive)
        self.unscheduled = []    # watch トークン
        self.fail_schedule = fail_schedule

    def schedule(self, handler, path, recursive=False):
        if self.fail_schedule:
            raise OSError(f"no such directory: {path}")
        token = object()
        self.scheduled.append((handler, path, recursive, token))
        return token

    def unschedule(self, watch):
        self.unscheduled.append(watch)


class _StubHandler:
    """登録失敗アラート（fail-closed）の呼び出しを記録するハンドラ代替。"""

    def __init__(self):
        self.failure_alerts = []

    def _push_audit_failure_alert(self, path, event_type, reason=None):
        self.failure_alerts.append({"path": path, "event": event_type, "reason": reason})


def _manager(observer=None, handler=None, static_roots=None, commit_gate=None):
    # commit_gate は #103 で yaml SSOT 化され install_hook キー必須になった
    # （未指定時は qu-e.yaml の実効値と同値 install_hook: true を渡す）
    return DynamicWatchManager(
        observer or _StubObserver(),
        handler or _StubHandler(),
        static_roots=static_roots if static_roots is not None else ["/opt/taka-ma"],
        commit_gate=commit_gate if commit_gate is not None else {"install_hook": True},
    )


def _ctx(workspace, status="in_progress"):
    return {"task_id": "t", "workspace": workspace, "status": status}


# ── DynamicWatchManager: 登録・解除 ──

def test_register_workspace_outside_static_roots(tmp_path):
    """静的ルート外の workspace は再帰監視として動的登録される。"""
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx(str(tmp_path)))
    assert len(obs.scheduled) == 1
    _, path, recursive, _ = obs.scheduled[0]
    assert path == str(tmp_path) and recursive is True


def test_workspace_under_static_root_is_not_registered():
    """静的ルート配下は既に監視済みのため動的登録しない（二重イベント防止）。"""
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx("/opt/taka-ma/work/task-1"))
    assert obs.scheduled == []


def test_unregister_on_task_end(tmp_path):
    """終了系（ctx=None）で監視が解除される。"""
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx(str(tmp_path)))
    token = obs.scheduled[0][3]
    mgr.sync("task-1", None)
    assert obs.unscheduled == [token]


def test_shared_path_refcount(tmp_path):
    """同一リポジトリを複数タスクが使う間は解除せず、最後のタスク終了で解除する。"""
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx(str(tmp_path)))
    mgr.sync("task-2", _ctx(str(tmp_path)))
    assert len(obs.scheduled) == 1  # 2 重登録しない
    mgr.sync("task-1", None)
    assert obs.unscheduled == []    # task-2 が使用中
    mgr.sync("task-2", None)
    assert len(obs.unscheduled) == 1


def test_same_task_same_path_is_idempotent(tmp_path):
    """同一タスクの同一 workspace 再受信（status 遷移の再 push）で二重登録しない。"""
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx(str(tmp_path)))
    mgr.sync("task-1", _ctx(str(tmp_path)))
    assert len(obs.scheduled) == 1


def test_tilde_workspace_is_expanded(tmp_path, monkeypatch):
    """`~` 前置きの workspace は qu-e ローカルの home で展開して登録する（防御的）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "DevDev" / "xxx"
    repo.mkdir(parents=True)
    obs = _StubObserver()
    mgr = _manager(observer=obs)
    mgr.sync("task-1", _ctx("~/DevDev/xxx"))
    assert obs.scheduled[0][1] == str(repo)


def test_schedule_failure_pushes_escalate_alert(tmp_path):
    """登録失敗（パス不在等）は無音にせず escalate アラートを push する（fail-closed）。"""
    handler = _StubHandler()
    mgr = _manager(observer=_StubObserver(fail_schedule=True), handler=handler)
    mgr.sync("task-1", _ctx(str(tmp_path / "missing")))
    assert len(handler.failure_alerts) == 1
    alert = handler.failure_alerts[0]
    assert alert["event"] == "watch" and "task-1" in alert["reason"]


# ── DynamicWatchManager: pre-commit フック自動導入 ──

def _git_repo(tmp_path):
    """`.git/` を持つ最小リポジトリ構造（hook 導入判定には実 git は不要）。"""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_commit_hook_installed_into_git_repo(tmp_path):
    """git リポジトリの workspace 登録時に .git/hooks/pre-commit を実行可能で配置する。"""
    repo = _git_repo(tmp_path)
    mgr = _manager()
    mgr.sync("task-1", _ctx(str(repo)))
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert os.stat(hook).st_mode & stat.S_IXUSR
    assert "commit_audit_cli.py" in hook.read_text()


def test_existing_hook_is_not_overwritten(tmp_path):
    """既存の pre-commit フックは上書きしない（ユーザーのリポジトリ設定を壊さない）。"""
    repo = _git_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\n# user hook\n")
    mgr = _manager()
    mgr.sync("task-1", _ctx(str(repo)))
    assert (hooks / "pre-commit").read_text() == "#!/bin/sh\n# user hook\n"


def test_hook_not_installed_when_disabled(tmp_path):
    """commit_gate.install_hook=false で自動導入しない。"""
    repo = _git_repo(tmp_path)
    mgr = _manager(commit_gate={"install_hook": False})
    mgr.sync("task-1", _ctx(str(repo)))
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


def test_hook_installed_at_task_end_for_repo_cloned_during_task(tmp_path):
    """登録時に .git 不在（clone 前）でも、タスク終了時の再試行で導入される。"""
    mgr = _manager()
    mgr.sync("task-1", _ctx(str(tmp_path)))
    assert not (tmp_path / ".git").exists()
    (tmp_path / ".git").mkdir()  # タスク中に worker が clone した状況
    mgr.sync("task-1", None)
    assert (tmp_path / ".git" / "hooks" / "pre-commit").exists()


# ── commit_audit_cli: 判定の正規化（fail-closed） ──

def test_normalize_decision_approve_and_deny():
    assert commit_audit_cli._normalize_decision({"decision": "approve"})[0] == "approve"
    assert commit_audit_cli._normalize_decision({"decision": " DENY "})[0] == "deny"


def test_normalize_decision_unknown_values_escalate():
    """未知値・非 dict・キー欠落は approve に倒さない（コミット中断側）。"""
    assert commit_audit_cli._normalize_decision({"decision": "block"})[0] == "escalate"
    assert commit_audit_cli._normalize_decision({})[0] == "escalate"
    assert commit_audit_cli._normalize_decision(["approve"])[0] == "escalate"
    assert commit_audit_cli._normalize_decision("approve")[0] == "escalate"


def test_normalize_decision_reason_falls_back_to_issues():
    decision, reason = commit_audit_cli._normalize_decision(
        {"decision": "deny", "issues": ["hardcoded secret", "rm -rf"]})
    assert decision == "deny"
    assert "hardcoded secret" in reason


# ── commit_audit_cli: staged diff 審査と jsonl 記録 ──

class _FakeReviewer:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def review_diff(self, diff, file_path):
        self.calls.append({"diff": diff, "file_path": file_path})
        return self._result


def _staged_repo(tmp_path):
    """staged 変更を 1 件持つ実 git リポジトリを作る。"""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    target = os.path.join(repo, "app.py")
    with open(target, "w") as f:
        f.write("print('hello')\n")
    subprocess.run(["git", "-C", repo, "add", "app.py"], check=True)
    return repo


def _config(tmp_path):
    return {"file_audit": {"log_dir": str(tmp_path / "logs")}}


def test_audit_commit_approve_writes_jsonl(tmp_path):
    """approve 判定が record と監査 jsonl（event=commit）に残る。"""
    repo = _staged_repo(tmp_path)
    reviewer = _FakeReviewer({"decision": "approve", "issues": [], "severity": "low"})
    record = commit_audit_cli.audit_commit(_config(tmp_path), repo, reviewer=reviewer)
    assert record["decision"] == "approve"
    assert record["files"] == ["app.py"]
    assert "app.py" in reviewer.calls[0]["diff"]
    logs = os.listdir(tmp_path / "logs")
    assert len(logs) == 1
    with open(tmp_path / "logs" / logs[0]) as f:
        row = json.loads(f.readline())
    assert row["event"] == "commit" and row["decision"] == "approve" and row["path"] == repo


def test_audit_commit_deny_blocks(tmp_path):
    """deny 判定は record に deny を返す（main が exit 1 でコミット中断する）。"""
    repo = _staged_repo(tmp_path)
    reviewer = _FakeReviewer({"decision": "deny", "issues": ["secret"], "severity": "high"})
    record = commit_audit_cli.audit_commit(_config(tmp_path), repo, reviewer=reviewer)
    assert record["decision"] == "deny"


def test_audit_commit_malformed_llm_response_escalates(tmp_path):
    """LLM 応答の崩れ（未知 decision）は escalate（＝コミット中断）に倒す。"""
    repo = _staged_repo(tmp_path)
    reviewer = _FakeReviewer({"decision": "LGTM!"})
    record = commit_audit_cli.audit_commit(_config(tmp_path), repo, reviewer=reviewer)
    assert record["decision"] == "escalate"


def test_audit_commit_no_staged_changes_approves_without_llm(tmp_path):
    """staged 変更が無ければ LLM を呼ばず approve（監査対象なし）。"""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    reviewer = _FakeReviewer({"decision": "deny"})
    record = commit_audit_cli.audit_commit(_config(tmp_path), repo, reviewer=reviewer)
    assert record["decision"] == "approve"
    assert reviewer.calls == []


def test_audit_commit_git_failure_raises(tmp_path):
    """git 不達（リポジトリでない）は例外送出（main が fail-closed で exit 1 にする）。"""
    with pytest.raises(Exception):
        commit_audit_cli.audit_commit(_config(tmp_path), str(tmp_path), reviewer=_FakeReviewer({}))
