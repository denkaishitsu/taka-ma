"""会話フロントエンドの `:モデル名` 明示指定の配線テスト。

検証する振る舞い（存在ではなく挙動）:
- TaskClassifier.parse_model は既存だったが呼び出し元が無く、`:opus` 指定が実行に一切
  反映されない未配線状態だった（実機検証で確認）。要約対象の生文（msg["text"]）から
  抽出し、確認レコード → 確定タスクの "_model" へ伝播することを保証する。
- 未登録モデル指定（InvalidModelError）は着手確認を提示せずエラー通知で止める。

conversation.py は orchestrator パッケージの重い __init__（pexpect/watchdog 等）を経由せず
ファイル直ロードする（test_process_manager.py と同方式）。ai_gateway は軽量な namespace
package のため src/ を sys.path に通すだけで正規 import できる。
"""

import importlib.util
import json
import os
import sys
import tempfile

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


class _FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append({"text": text, "channel": channel, "team_id": team_id, "thread_ts": thread_ts})

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None, team_id=None, thread_ts=None):
        self.notes.append({"exec_confirm": exec_request_id, "summary": summary})


CONFIG = {
    "sa-ru": {"model": "dummy-brain", "ollama_host": "http://localhost:11434",
              "converse_timeout_sec": 120},
    # llm_timeout_sec は ya-ta.yaml 必須キー（コード側既定なし・SSOT）のためテスト config にも供給する
    "ya-ta": {"model": "dummy-classifier-model", "llm_timeout_sec": 180},
    "models": {"opus": {"model_flag": "--model opus"}, "gemini": {"model_flag": ""}},
}


def _manager(tmp_dir):
    # sessions_dir は sa-ru.yaml 必須キー（同上）。既存アサーションが tmp_dir（確認レコード置き場）
    # の空を検査するため、セッション置き場は別の一時 dir に分離する
    config = {**CONFIG, "exec_confirm": {"dir": tmp_dir},
              "conversation": {"sessions_dir": tempfile.mkdtemp(prefix="sessions-"),
                               # session_ttl_sec も #103 で必須キー化（実効値と同値）
                               "session_ttl_sec": 3600}}
    classifier = TaskClassifier(config)
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir, classifier=classifier)


def test_present_summary_carries_explicit_model_into_task():
    """`:opus` を含む生文から抽出したモデル指定が確定タスクの _model まで伝播する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    msg = {"conversation_id": "c1", "text": "READMEを直して :opus", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "111.222"}

    models = mgr.classifier.parse_model(msg["text"])[1]
    mgr._present_summary(msg, "READMEの誤字を修正する", models)

    confirm_files = [f for f in os.listdir(tmp) if f.endswith(".json")]
    assert len(confirm_files) == 1
    with open(os.path.join(tmp, confirm_files[0])) as f:
        record = json.load(f)
    assert record["model_override"] == ["opus"]

    task_id = mgr.create_exec_task(record)
    assert task_id
    task_files = [f for f in os.listdir(tmp) if f.endswith(".json") and f != confirm_files[0]]
    assert len(task_files) == 1
    with open(os.path.join(tmp, task_files[0])) as f:
        task = json.load(f)
    assert task["_model"] == ["opus"]


def test_present_summary_without_model_marker_leaves_model_none():
    """`:モデル名` 指定が無ければ _model は None（既存の category_defaults フォールバックに任せる）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    msg = {"conversation_id": "c2", "text": "READMEを直して", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "222.333"}

    models = mgr.classifier.parse_model(msg["text"])[1]
    assert models == []
    mgr._present_summary(msg, "READMEの誤字を修正する", models)
    confirm_files = [f for f in os.listdir(tmp) if f.endswith(".json")]
    with open(os.path.join(tmp, confirm_files[0])) as f:
        record = json.load(f)
    assert record["model_override"] == []

    task_id = mgr.create_exec_task(record)
    task_files = [f for f in os.listdir(tmp) if f.endswith(".json") and f != confirm_files[0]]
    with open(os.path.join(tmp, task_files[0])) as f:
        task = json.load(f)
    assert task["_model"] is None


def test_handle_message_invalid_model_notifies_and_skips_confirm(monkeypatch):
    """未登録モデル指定は着手確認を提示せず、エラーを通知して止める。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    msg = {"conversation_id": "c3", "text": "READMEを直して :gpt5", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "333.444"}

    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": "READMEの誤字を修正する", "reply": "",
    })

    mgr.handle_message(msg)

    assert os.listdir(tmp) == []  # 確認レコードは作られない
    assert any("登録されていません" in n["text"] for n in mgr.slack.notes)
