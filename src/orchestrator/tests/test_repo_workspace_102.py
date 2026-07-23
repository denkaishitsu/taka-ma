"""`repo:` 実開発リポジトリ指定（§8.13）の配線・検証テスト。

検証する振る舞い:
- parse_workspace: 生文からの抽出・除去と、SSH/cwd に乗るパスの fail-closed 検証
  （絶対パスのみ・安全文字のみ・`..` 不可・`~` 不可・複数指定不可）
- `repo:` と `:モデル名` の共存（repo: を先に除去しないと `:/path` が未登録モデル誤検出）
- 確認レコード → 確定タスク workspace → _resolve_workspace → _push_task_context の伝播
- in_progress push が同一 SSH コマンド内で workspace を先に mkdir する（§8.13 存在保証）

conversation.py は test_conversation_model_override.py と同方式でファイル直ロードする。
Orchestrator は __new__ で最小構成にして対象メソッドのみ検証する。
"""

import importlib.util
import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway.classifier import TaskClassifier  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager
InvalidWorkspaceError = conversation.InvalidWorkspaceError


class _FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append({"text": text, "channel": channel})

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None):
        self.notes.append({"exec_confirm": exec_request_id, "summary": summary,
                           "plan_text": plan_text})


CONFIG = {
    "sa-ru": {"model": "dummy-brain", "ollama_host": "http://localhost:11434",
              "converse_timeout_sec": 120},
    # llm_timeout_sec は ya-ta.yaml 必須キー（コード側既定なし・SSOT）のためテスト config にも供給する
    "ya-ta": {"model": "dummy-classifier-model", "llm_timeout_sec": 180},
    "models": {"opus": {"model_flag": "--model opus"}},
}


def _manager(tmp_dir):
    # sessions_dir は sa-ru.yaml 必須キー（同上）。アサーションが tmp_dir（確認レコード置き場）
    # の空を検査するため、セッション置き場は別の一時 dir に分離する
    config = {**CONFIG, "exec_confirm": {"dir": tmp_dir},
              "conversation": {"sessions_dir": tempfile.mkdtemp(prefix="sessions-"),
                               # session_ttl_sec も #103 で必須キー化（実効値と同値）
                               "session_ttl_sec": 3600}}
    classifier = TaskClassifier(config)
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir, classifier=classifier)


# ── parse_workspace: 抽出と検証（fail-closed） ──

def test_parse_workspace_extracts_and_strips_token():
    clean, ws = ConversationManager.parse_workspace(
        "repo:/Users/u/DevDev/xxx のバグを直して")
    assert ws == "/Users/u/DevDev/xxx"
    assert "repo:" not in clean and "バグを直して" in clean


def test_parse_workspace_none_when_absent():
    clean, ws = ConversationManager.parse_workspace("READMEを直して")
    assert ws is None and clean == "READMEを直して"


def test_parse_workspace_ignores_embedded_repo_in_url():
    """URL 等に埋め込まれた `/repo:tag` はトークンとして扱わない（誤差し戻し防止）。"""
    text = "https://example.com/img/repo:latest を参照して直して"
    clean, ws = ConversationManager.parse_workspace(text)
    assert ws is None and clean == text


def test_parse_workspace_strips_trailing_slash():
    _, ws = ConversationManager.parse_workspace("repo:/a/b/ を見て")
    assert ws == "/a/b"


def test_parse_workspace_rejects_tilde():
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace("repo:~/DevDev/xxx を直して")


def test_parse_workspace_rejects_relative_and_unsafe_chars():
    for bad in ("repo:DevDev/xxx", "repo:/tmp/x;rm", "repo:/tmp/$(x)", "repo:/tmp/a&b"):
        with pytest.raises(InvalidWorkspaceError):
            ConversationManager.parse_workspace(f"{bad} を直して")


def test_parse_workspace_rejects_dotdot():
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace("repo:/opt/../etc を直して")


def test_parse_workspace_rejects_multiple_distinct():
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace("repo:/a repo:/b を直して")


# ── repo: と :モデル名 の共存（handle_message 経由の end-to-end） ──

def test_handle_message_carries_workspace_and_model_into_task(monkeypatch):
    """repo: を先に除去した上で :opus 抽出が効き、両方が確定タスクへ伝播する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    msg = {"conversation_id": "c1", "text": "repo:/Users/u/DevDev/xxx を直して :opus",
           "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": "xxx リポジトリのバグ修正", "reply": ""})

    mgr.handle_message(msg)

    confirm_files = [f for f in os.listdir(tmp) if f.endswith(".json")]
    assert len(confirm_files) == 1
    with open(os.path.join(tmp, confirm_files[0])) as f:
        record = json.load(f)
    assert record["workspace"] == "/Users/u/DevDev/xxx"
    assert record["model_override"] == ["opus"]

    task_id = mgr.create_exec_task(record)
    assert task_id
    task_files = [f for f in os.listdir(tmp) if f.endswith(".json") and f != confirm_files[0]]
    with open(os.path.join(tmp, task_files[0])) as f:
        task = json.load(f)
    assert task["workspace"] == "/Users/u/DevDev/xxx"
    assert task["_model"] == ["opus"]


def test_handle_message_invalid_workspace_notifies_and_skips_confirm(monkeypatch):
    """検証を通らない repo: 指定は着手確認を提示せず差し戻す（fail-closed）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    msg = {"conversation_id": "c2", "text": "repo:~/DevDev/xxx を直して",
           "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "2.3"}
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": "xxx リポジトリのバグ修正", "reply": ""})

    mgr.handle_message(msg)

    assert os.listdir(tmp) == []
    assert any("絶対パス" in n["text"] for n in mgr.slack.notes)


def test_create_exec_task_without_workspace_omits_key():
    """repo: 指定なしのタスクは workspace キーを持たない（既定 {base}/{task_id} に解決される）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    record = {"summary": "s", "user_id": "U", "team_id": "T", "channel_id": "C",
              "thread_ts": None, "model_override": [], "workspace": None}
    mgr.create_exec_task(record)
    task_files = [f for f in os.listdir(tmp) if f.endswith(".json")]
    with open(os.path.join(tmp, task_files[0])) as f:
        task = json.load(f)
    assert "workspace" not in task


# ── Orchestrator: workspace 解決と task_context push（§8.13） ──

from orchestrator import Orchestrator  # noqa: E402


def _orchestrator():
    o = Orchestrator.__new__(Orchestrator)
    o.config = {"task_context": {"remote_dir": "/opt/taka-ma/data/task-context",
                                 "workspace_base": "/opt/taka-ma/work"}}
    return o


def test_resolve_workspace_prefers_explicit_repo():
    o = _orchestrator()
    task = {"task_id": "tid-1", "workspace": "/Users/u/DevDev/xxx"}
    assert o._resolve_workspace(task) == "/Users/u/DevDev/xxx"


def test_resolve_workspace_defaults_to_workspace_base():
    o = _orchestrator()
    assert o._resolve_workspace({"task_id": "tid-1"}) == "/opt/taka-ma/work/tid-1"


class _FakeProcessMgr:
    def __init__(self):
        self.calls = []

    def run_ssh_command(self, command, stdin_text=None):
        self.calls.append({"command": command, "stdin": stdin_text})
        return ""


def test_push_task_context_in_progress_mkdirs_workspace_first():
    """in_progress push は同一 SSH コマンド内で workspace を先に mkdir する（存在保証）。"""
    o = _orchestrator()
    o.process_mgr = _FakeProcessMgr()
    task = {"task_id": "tid-1", "command": "c", "status": "in_progress",
            "workspace": "/Users/u/DevDev/xxx"}
    o._push_task_context(task)
    call = o.process_mgr.calls[0]
    cmd = call["command"]
    assert cmd.startswith("mkdir -p /Users/u/DevDev/xxx && ")
    assert cmd.index("mkdir -p /Users/u/DevDev/xxx") < cmd.index("cat > ")
    payload = json.loads(call["stdin"])
    assert payload["workspace"] == "/Users/u/DevDev/xxx"
    assert payload["status"] == "in_progress"


def test_push_task_context_terminal_status_skips_workspace_mkdir():
    """終了系 push は workspace の mkdir をしない（受信ディレクトリの mkdir のみ）。"""
    o = _orchestrator()
    o.process_mgr = _FakeProcessMgr()
    task = {"task_id": "tid-1", "command": "c", "status": "completed",
            "workspace": "/Users/u/DevDev/xxx"}
    o._push_task_context(task)
    cmd = o.process_mgr.calls[0]["command"]
    assert "mkdir -p /Users/u/DevDev/xxx" not in cmd
    assert json.loads(o.process_mgr.calls[0]["stdin"])["workspace"] == "/Users/u/DevDev/xxx"


def test_push_task_context_default_workspace_in_payload():
    """repo: 指定なしのタスクは payload の workspace が既定 {base}/{task_id} になる。"""
    o = _orchestrator()
    o.process_mgr = _FakeProcessMgr()
    task = {"task_id": "tid-9", "command": "c", "status": "in_progress"}
    o._push_task_context(task)
    payload = json.loads(o.process_mgr.calls[0]["stdin"])
    assert payload["workspace"] == "/opt/taka-ma/work/tid-9"
