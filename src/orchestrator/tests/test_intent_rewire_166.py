"""会話層の意図判定配線（#taka-ma/166.2）の振る舞いテスト。

検証する振る舞い:
- 2026-09-07 の実発話（probe 誤爆で 3 回失敗した実行依頼）が、実 IntentClassifier の
  配線（LLM はスタブ）を通って着手確認の提示まで到達する。
- 確定タスクの要約は発話の逐語（脳 LLM の言い換え要約を廃止）。
- probe_repo / probe_task 判定では会話脳（_invoke_llm）を呼ばない。
- force_ready（/taka-ma-go）は判定器を経由しない明示エスケープ。
- 判定器の fail-closed（action=chat）は会話返信に落ちる。
"""

import json
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway import intent_classifier as ic_mod  # noqa: E402
from orchestrator import conversation as conv_mod  # noqa: E402
from orchestrator.conversation import ConversationManager  # noqa: E402

UTTER_0907 = ("要件定義書と詳細設計に問題がないこと確認して欲しい。"
              "repo:/Users/dev/trader 配下のファイルを見なさい。"
              "そして、実装する機能一覧を箇条書きで教えて")


class _Notifier:
    def __init__(self):
        self.notes, self.confirms = [], []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, *a, **k):
        self.confirms.append(k or a)


def _manager(tmp, with_intent=True):
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp},
        "conversation": {"sessions_dir": tempfile.mkdtemp(prefix="s-"),
                         "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work", "worker_home": "/Users/dev"},
        "ya-ta": {"model": "local-dummy", "llm_timeout_sec": 60,
                  "contractor": {"model": "opus"}},
        "routing": {"confidence_threshold": 0.8},
    }
    if not with_intent:
        del config["routing"]
    return ConversationManager(config, _Notifier(), task_dir=tmp)


def _msg(text=UTTER_0907, **kw):
    return {"conversation_id": "c1", "text": text, "user_id": "U1",
            "team_id": "T1", "channel_id": "C1", "thread_ts": "1.0", **kw}


def test_0907_utterance_reaches_exec_confirm(monkeypatch):
    """9/7 実発話が実 IntentClassifier 配線（LLM スタブ）で着手確認の提示まで到達する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    assert mgr.intent is not None
    monkeypatch.setattr(
        ic_mod, "run_ollama",
        lambda *a, **k: json.dumps({"action": "execute", "confidence": 0.95,
                                    "evidence": "機能一覧を箇条書きで教えて"},
                                   ensure_ascii=False))

    def _no_chat_llm(*a, **k):
        raise AssertionError("execute 判定で会話脳を呼んではならない")
    mgr._invoke_llm = _no_chat_llm
    mgr.handle_message(_msg())
    files = [f for f in os.listdir(tmp) if f.endswith(".json")]
    assert len(files) == 1, "着手確認レコードが作られていない"
    record = json.load(open(os.path.join(tmp, files[0])))
    assert record["summary"] == UTTER_0907          # 要約は発話の逐語（言い換え廃止）
    assert record["workspace"] == "/Users/dev/trader"


def test_probe_action_skips_chat_llm(monkeypatch):
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(
        ic_mod, "run_ollama",
        lambda *a, **k: json.dumps({"action": "probe_task", "confidence": 0.9,
                                    "evidence": "終わった"}, ensure_ascii=False))
    mgr._invoke_llm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("呼ぶな"))
    mgr._answer_task_status = lambda msg: mgr.slack.notes.append("実測回答")
    mgr.handle_message(_msg("さっきのは終わった？"))
    assert mgr.slack.notes == ["実測回答"]


def test_force_ready_bypasses_classifier(monkeypatch):
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)

    def _no_classify(*a, **k):
        raise AssertionError("force_ready は判定器を経由しない")
    mgr.intent = type("I", (), {"classify": _no_classify})()
    mgr.handle_message(_msg("go", force_ready=True))
    assert len([f for f in os.listdir(tmp) if f.endswith(".json")]) == 1


def test_fail_closed_chat_falls_to_reply(monkeypatch):
    """判定器が fail-closed（chat）を返したら会話返信（確認質問）に落ちる。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.intent = type("I", (), {"classify": staticmethod(
        lambda h, t: dict(ic_mod.FAIL_CLOSED))})()
    mgr._invoke_llm = lambda *a, **k: {"reply": "どのリポジトリの話でしょうか？"}
    mgr._claims_check = lambda *a, **k: {"progress": False, "state": False}
    mgr.handle_message(_msg("あれやっといて"))
    assert mgr.slack.notes[-1] == "どのリポジトリの話でしょうか？"
    assert os.listdir(tmp) == []


def test_partial_config_degrades_to_chat(monkeypatch):
    """routing キー欠落の部分構成では intent=None となり chat へ縮退する（段階導入）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp, with_intent=False)
    assert mgr.intent is None
    mgr._invoke_llm = lambda *a, **k: {"reply": "承知しました"}
    mgr._claims_check = lambda *a, **k: {"progress": False, "state": False}
    mgr.handle_message(_msg("こんにちは"))
    assert mgr.slack.notes[-1] == "承知しました"
