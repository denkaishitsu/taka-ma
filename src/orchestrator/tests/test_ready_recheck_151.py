"""ready 再検査（§8.3 細部質問の検品）の検証。

検証する振る舞い（2026-08-24 E2E で実測した ready 判定取りこぼしの構造的再発防止）:
- 会話継続の返信が「細部への質問」（detail）と選別されたら 1 回だけ再判定し、
  再判定が ready=true+summary を返したときだけ差し替えて着手確認へ進む
- essential（対象・動作そのものの質問）/ other（普通の返答）は素通し
- 選別不能（パース不能・LLM 不達）は "other" へ縮退（従来動作を壊さない）
- force_ready・probe・エラー応答は検品に掛けない

conversation.py は test_exec_contract_149.py と同方式でファイル直ロードする。
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

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_151", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager


class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None, contract_text=None):
        self.confirms.append({"summary": summary})


def _manager(tmp_dir):
    sessions_dir = tempfile.mkdtemp(prefix="sessions-")
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp_dir},
        "conversation": {"sessions_dir": sessions_dir, "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work",
                         "worker_home": "/Users/dev"},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


def _msg(text, cid="c1", force=False):
    return {"conversation_id": cid, "text": text, "force_ready": force,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}


class _InvokeStub:
    """_invoke_llm の呼び出し回数・detail_retry を記録し、応答列を順に返す。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, history, force, progress=None, detail_retry=False):
        self.calls.append({"force": force, "detail_retry": detail_retry})
        return self.results.pop(0)


_QUESTION = {"ready": False, "summary": None, "reply": "何を追記すればよいですか？"}
_READY = {"ready": True, "summary": "READMEに一行追記してpush", "reply": "着手します"}
_CONTRACT = {"directive": None, "constraints": [], "acceptance": [],
             "workspace": None, "needs_repo": False}


def _wire(mgr, monkeypatch, results, verdict):
    stub = _InvokeStub(results)
    monkeypatch.setattr(mgr, "_invoke_llm", stub)
    monkeypatch.setattr(mgr, "_recheck_detail_question",
                        lambda message_text, reply, progress=None: verdict)
    monkeypatch.setattr(mgr, "_build_contract",
                        lambda cid, summary, progress=None: dict(_CONTRACT))
    return stub


# ── 検品 → 再判定の差し替え ──

def test_detail_verdict_retries_and_presents_summary(monkeypatch):
    """detail 選別 → detail_retry=True で再判定 → ready なら着手確認を提示する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    stub = _wire(mgr, monkeypatch, [dict(_QUESTION), dict(_READY)], "detail")
    mgr.handle_message(_msg("README を追記して push しろ"))
    assert [c["detail_retry"] for c in stub.calls] == [False, True]
    assert mgr.slack.confirms and mgr.slack.confirms[0]["summary"] == _READY["summary"]
    # 元の細部質問は Slack へ送られない
    assert all(_QUESTION["reply"] not in n for n in mgr.slack.notes)


def test_detail_verdict_but_retry_still_question_keeps_original(monkeypatch):
    """再判定が依然 ready=false なら元の質問をそのまま返す（二重生成のブレを持ち込まない）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    retry_q = {"ready": False, "summary": None, "reply": "別の聞き方の質問？"}
    stub = _wire(mgr, monkeypatch, [dict(_QUESTION), retry_q], "detail")
    mgr.handle_message(_msg("README を追記して push しろ"))
    assert len(stub.calls) == 2
    assert not mgr.slack.confirms
    assert any(_QUESTION["reply"] in n for n in mgr.slack.notes)


def test_essential_verdict_passes_question_through(monkeypatch):
    """essential（対象・動作そのものの質問）は再判定せず素通し。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    q = {"ready": False, "summary": None, "reply": "どのリポジトリですか？"}
    stub = _wire(mgr, monkeypatch, [q], "essential")
    mgr.handle_message(_msg("README を書け"))
    assert len(stub.calls) == 1
    assert any(q["reply"] in n for n in mgr.slack.notes)


def test_other_verdict_passes_reply_through(monkeypatch):
    """other（普通の返答）は再判定せず素通し。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    a = {"ready": False, "summary": None, "reply": "async は协調・threading は並行です"}
    stub = _wire(mgr, monkeypatch, [a], "other")
    mgr.handle_message(_msg("async と threading の違いは？"))
    assert len(stub.calls) == 1
    assert any(a["reply"] in n for n in mgr.slack.notes)


# ── 検品に掛けない経路 ──

def test_force_ready_skips_recheck(monkeypatch):
    """/taka-ma-go（force_ready）は検品を経ない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    called = []
    monkeypatch.setattr(mgr, "_recheck_detail_question",
                        lambda *a, **k: called.append(1) or "other")
    monkeypatch.setattr(mgr, "_invoke_llm",
                        lambda history, force, progress=None, detail_retry=False:
                        dict(_READY))
    monkeypatch.setattr(mgr, "_build_contract",
                        lambda cid, summary, progress=None: dict(_CONTRACT))
    mgr.handle_message(_msg("やれ", force=True))
    assert not called


def test_probe_skips_recheck(monkeypatch):
    """probe 応答（実測質問の選別）は検品を経ない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    called = []
    monkeypatch.setattr(mgr, "_recheck_detail_question",
                        lambda *a, **k: called.append(1) or "other")
    monkeypatch.setattr(mgr, "_invoke_llm",
                        lambda history, force, progress=None, detail_retry=False: {
                            "ready": False, "summary": None, "reply": "",
                            "probe": "repo_status"})
    monkeypatch.setattr(mgr, "_answer_probe", lambda msg: None)
    mgr.handle_message(_msg("いまのリポジトリの状態は？"))
    assert not called


# ── 選別本体（_recheck_detail_question） ──

def test_recheck_parses_verdicts(monkeypatch):
    """選別は verdict の許可値のみ通し、不正値は other へ落とす。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    for raw, expected in [
        ('{"verdict": "detail"}', "detail"),
        ('{"verdict": "essential"}', "essential"),
        ('{"verdict": "other"}', "other"),
        ('{"verdict": "unknown"}', "other"),
        ('{"nonsense": true}', "other"),
        ('[1, 2]', "other"),
        ('"detail"', "other"),
    ]:
        monkeypatch.setattr(conversation, "run_ollama", lambda *a, **k: raw)
        assert mgr._recheck_detail_question("依頼", "質問") == expected


def test_recheck_degrades_to_other_on_llm_failure(monkeypatch):
    """LLM 不達は other（素通し＝従来動作）へ縮退する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)

    def _boom(*a, **k):
        raise conversation.OllamaConnectionError("接続不可")

    monkeypatch.setattr(conversation, "run_ollama", _boom)
    assert mgr._recheck_detail_question("依頼", "質問") == "other"


def test_detail_retry_appends_instruction(monkeypatch):
    """detail_retry=True は再判定指示をプロンプト末尾に付ける（force とは別文言）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    seen = {}

    def _capture(model, prompt, **kw):
        seen["prompt"] = prompt
        return json.dumps({"reply": "", "ready": True, "summary": "s", "probe": None})

    monkeypatch.setattr(conversation, "run_ollama", _capture)
    mgr._invoke_llm([{"role": "user", "text": "README を追記して push しろ"}],
                    force=False, detail_retry=True)
    assert "細部への質問は禁止" in seen["prompt"]
    assert "明示的に実行を指示しました" not in seen["prompt"]
