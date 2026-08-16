"""#143 repo: 指定の配線ギャップ解消（§8.13）の検証。

検証する振る舞い（2026-08-14 Wave1-B 回帰検証レポート 検証2 / 2026-08-10 インシデント根本原因 1）:
- 自然文のリポジトリ指定（インシデント発端メッセージと同型の `#Repo ~/DevDev/...`）が
  workspace に配線される
- 抽出がセッション単位で持続する（冒頭で指定 → 後の発話で着手、でも指定が落ちない。
  再起動＝マネージャ再生成後もセッション永続化ファイルから回復する）
- `~/` 前置きは worker ホスト HOME（task_context.worker_home）へ展開してから検証する。
  worker_home 未設定時は従来どおり差し戻す（fail-closed）
- workspace 未指定の着手確認に「未指定（既定の空作業場）」が明示される
- 自然文候補の検証不合格は会話を止めず repo: 記法での再指定を促す
- `repo:` 記法の不正は発話時点で差し戻し、脳 LLM 呼び出しへ進めない

conversation.py は test_repo_workspace_102.py と同方式でファイル直ロードする。
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
    spec = importlib.util.spec_from_file_location("conversation_143", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager
InvalidWorkspaceError = conversation.InvalidWorkspaceError

WORKER_HOME = "/Users/dev"
# インシデント発端メッセージと同型の自然文指定（2026-08-10 D0BE24T5P9C）
INCIDENT_TEXT = ("#Repo ~/DevDev/projects/obsidian-auto-stock-trader\n"
                 "Obsidian連携型 株式自動売買システムの要件定義を作成して")
INCIDENT_RESOLVED = "/Users/dev/DevDev/projects/obsidian-auto-stock-trader"


class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append({"text": text, "channel": channel})

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None):
        self.confirms.append({"exec_confirm": exec_request_id, "summary": summary,
                              "plan_text": plan_text, "workspace_text": workspace_text})


def _config(tmp_dir, sessions_dir, worker_home=WORKER_HOME):
    task_context = {"remote_dir": "/opt/taka-ma/data/task-context",
                    "workspace_base": "/opt/taka-ma/work"}
    if worker_home is not None:
        task_context["worker_home"] = worker_home
    return {
        "sa-ru": {"model": "dummy-brain", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        # llm_timeout_sec は ya-ta.yaml 必須キー（コード側既定なし・SSOT）のためテスト config にも供給する
        "ya-ta": {"model": "dummy-classifier-model", "llm_timeout_sec": 180},
        "models": {"opus": {"model_flag": "--model opus"}},
        "exec_confirm": {"dir": tmp_dir},
        "conversation": {"sessions_dir": sessions_dir, "session_ttl_sec": 3600},
        "task_context": task_context,
    }


def _manager(tmp_dir, sessions_dir=None, worker_home=WORKER_HOME):
    sessions_dir = sessions_dir or tempfile.mkdtemp(prefix="sessions-")
    config = _config(tmp_dir, sessions_dir, worker_home)
    classifier = TaskClassifier(config)
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir,
                               classifier=classifier)


def _ready(mgr, monkeypatch, summary="要件定義の作成"):
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": summary, "reply": ""})


def _not_ready(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": False, "summary": None, "reply": "了解しました"})


def _msg(text, cid="c1"):
    return {"conversation_id": cid, "text": text,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}


def _confirm_record(tmp_dir):
    files = [f for f in os.listdir(tmp_dir) if f.endswith(".json")]
    assert len(files) == 1
    with open(os.path.join(tmp_dir, files[0])) as f:
        return json.load(f)


# ── 自然文指定の配線（インシデント発端メッセージと同型） ──

def test_incident_style_natural_mention_resolves_workspace(monkeypatch):
    """`#Repo ~/DevDev/...` の自然文指定が実リポジトリの絶対パスへ解決される。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)

    mgr.handle_message(_msg(INCIDENT_TEXT))

    record = _confirm_record(tmp)
    assert record["workspace"] == INCIDENT_RESOLVED
    assert mgr.slack.confirms[0]["workspace_text"] == INCIDENT_RESOLVED

    task_id = mgr.create_exec_task(record)
    assert task_id
    task_files = [f for f in os.listdir(tmp)
                  if f.endswith(".json") and f != f"{record['exec_request_id']}.json"]
    with open(os.path.join(tmp, task_files[0])) as f:
        task = json.load(f)
    assert task["workspace"] == INCIDENT_RESOLVED


def test_natural_mention_japanese_marker(monkeypatch):
    """「リポジトリ: /path」形式のマーカーも配線される。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    mgr.handle_message(_msg("リポジトリは /Users/dev/DevDev/xxx です。直して"))
    assert _confirm_record(tmp)["workspace"] == "/Users/dev/DevDev/xxx"


def test_find_repo_mention_ignores_url_embedded():
    """URL 内の `.../repo:tag` や `my-repo` はマーカーとして扱わない（誤配線防止）。"""
    assert ConversationManager.find_repo_mention(
        "https://example.com/img/repo:latest を参照して") is None
    assert ConversationManager.find_repo_mention("my-repo /tmp/x の話") is None


def test_find_repo_mention_last_wins():
    text = "repo: /a/b と言ったが、やはり リポジトリ: /c/d にして"
    assert ConversationManager.find_repo_mention(text) == "/c/d"


# ── セッション持続（冒頭で指定 → 後の発話で着手） ──

def test_workspace_persists_across_turns(monkeypatch):
    """ready を発火させた最終発話に repo 指定が無くても、冒頭の指定が workspace に乗る。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _not_ready(mgr, monkeypatch)
    mgr.handle_message(_msg(INCIDENT_TEXT))
    assert mgr.slack.confirms == []  # まだ着手確認は出ない

    _ready(mgr, monkeypatch)
    mgr.handle_message(_msg("要件定義を作成して。着手して"))
    assert _confirm_record(tmp)["workspace"] == INCIDENT_RESOLVED


def test_workspace_recovers_after_restart(monkeypatch):
    """マネージャ再生成（sa-ru 再起動相当）後もセッション永続化ファイルから指定を回復する。"""
    tmp = tempfile.mkdtemp()
    sessions = tempfile.mkdtemp(prefix="sessions-")
    mgr1 = _manager(tmp, sessions_dir=sessions)
    _not_ready(mgr1, monkeypatch)
    mgr1.handle_message(_msg(INCIDENT_TEXT))

    mgr2 = _manager(tmp, sessions_dir=sessions)  # 再起動相当（メモリ状態なし）
    _ready(mgr2, monkeypatch)
    mgr2.handle_message(_msg("着手して"))
    assert _confirm_record(tmp)["workspace"] == INCIDENT_RESOLVED


def test_last_specification_wins(monkeypatch):
    """同一セッションで指定し直したら最後の指定が勝つ。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _not_ready(mgr, monkeypatch)
    mgr.handle_message(_msg("repo:/Users/dev/DevDev/aaa を見て"))
    _ready(mgr, monkeypatch)
    mgr.handle_message(_msg("やはり repo:/Users/dev/DevDev/bbb で着手して"))
    assert _confirm_record(tmp)["workspace"] == "/Users/dev/DevDev/bbb"


# ── ~ 前置きの扱い（worker_home 展開 or 差し戻し） ──

def test_parse_workspace_expands_tilde_with_worker_home():
    clean, ws = ConversationManager.parse_workspace(
        "repo:~/DevDev/xxx を直して", worker_home=WORKER_HOME)
    assert ws == "/Users/dev/DevDev/xxx"
    assert "repo:" not in clean


def test_parse_workspace_expansion_result_still_validated():
    """展開後パスも同じ fail-closed 検証に通す（危険文字は展開しても差し戻し）。"""
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace(
            "repo:~/DevDev/$(x) を直して", worker_home=WORKER_HOME)


def test_parse_workspace_rejects_tilde_without_worker_home():
    """worker_home 未設定なら従来どおり差し戻す（既存挙動の維持）。"""
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace("repo:~/DevDev/xxx を直して")


def test_parse_workspace_rejects_tilde_user_form():
    """`~user/` 形式は worker_home があっても展開できないため差し戻す。"""
    with pytest.raises(InvalidWorkspaceError):
        ConversationManager.parse_workspace(
            "repo:~other/DevDev/xxx を直して", worker_home=WORKER_HOME)


# ── 未指定の明示（着手確認の提示文） ──

def test_unspecified_workspace_is_explicit_in_confirm(monkeypatch):
    """workspace 未指定の着手確認に「未指定（既定の空作業場）」と repo: 案内が明示される。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    mgr.handle_message(_msg("READMEを直して"))
    ws_text = mgr.slack.confirms[0]["workspace_text"]
    assert "未指定（既定の空作業場）" in ws_text
    assert "repo:/絶対パス" in ws_text
    assert _confirm_record(tmp)["workspace"] is None


def test_notifier_renders_workspace_block():
    """SlackNotifier が workspace_text を提示ブロックに載せる（None なら行を出さない）。"""
    from orchestrator.slack_notifier import SlackNotifier

    class _FakeClient:
        def __init__(self):
            self.sent = None

        def chat_postMessage(self, **kw):
            self.sent = kw

    n = SlackNotifier.__new__(SlackNotifier)
    client = _FakeClient()
    n._client_for = lambda team_id: client
    n._channel_for = lambda team_id, channel: "C1"

    n.send_exec_confirm_request("id1", "要約", workspace_text="/Users/dev/DevDev/xxx")
    texts = [b["text"]["text"] for b in client.sent["blocks"] if b["type"] == "section"]
    assert any(t == "*workspace:* /Users/dev/DevDev/xxx" for t in texts)

    n.send_exec_confirm_request("id2", "要約")  # 非会話経路の互換（行なし）
    texts = [b["text"]["text"] for b in client.sent["blocks"] if b["type"] == "section"]
    assert not any("workspace" in t for t in texts)


# ── 検証不合格の扱い ──

def test_invalid_natural_mention_prompts_repospec_without_blocking(monkeypatch):
    """自然文候補が検証不合格でも会話は止めず、repo: 記法での再指定を促す。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp, worker_home=None)  # ~ を展開できない環境で自然文 ~ 指定
    _ready(mgr, monkeypatch)
    mgr.handle_message(_msg(INCIDENT_TEXT))
    # 案内が出て、着手確認は workspace 未指定として提示される（会話は止まらない）
    assert any("repo:/絶対パス" in n["text"] for n in mgr.slack.notes)
    assert len(mgr.slack.confirms) == 1
    assert "未指定（既定の空作業場）" in mgr.slack.confirms[0]["workspace_text"]


def test_invalid_repo_token_bounces_before_llm(monkeypatch):
    """`repo:` 記法の不正は発話時点で差し戻し、脳 LLM 呼び出しへ進めない（fail-closed）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    called = []
    monkeypatch.setattr(mgr, "_invoke_llm",
                        lambda *a, **kw: called.append(1) or {"ready": False})
    mgr.handle_message(_msg("repo:relative/path を直して"))
    assert called == []
    assert any("絶対パス" in n["text"] for n in mgr.slack.notes)
    assert [f for f in os.listdir(tmp) if f.endswith(".json")] == []
