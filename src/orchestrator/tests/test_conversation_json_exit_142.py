"""会話出口の内部 JSON フィルタの振る舞いテスト（#taka-ma/142）。

検証する振る舞い（存在ではなく挙動）:
- _invoke_llm の JSONDecodeError フォールバックで、契約 JSON {reply, ready, summary} の
  断片が混じったパース不能出力が Slack 返信へ生のまま出ない（2026-08-10 Slack DM
  インシデント F2 の生 JSON 漏出の再発防止）
- 検出時もタスクを止めず会話継続（ready=false）が維持される（着手確認は提示されない）
- 縮退文言はシステムメッセージとして履歴に残さない（脳がオウム返しする実機再現
  2026-07-14 と同じ扱い）
- 契約キーを含まない散文のパース不能出力は従来どおり素の stdout が返る（無退行）
- 正常な契約 JSON 出力は従来どおり reply / 着手確認へ流れる（無退行）
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import orchestrator.conversation as conv_mod
from orchestrator.conversation import ConversationManager


class SlackStub:
    def __init__(self):
        self.sent = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.sent.append(text)

    def send_exec_confirm_request(self, *a, **kw):
        self.sent.append("exec_confirm")


def make_manager(tmp_path):
    config = {
        "sa-ru": {"model": "test-model", "converse_timeout_sec": 5,
                  "ollama_host": "http://localhost:11434"},
        "exec_confirm": {"dir": str(tmp_path / "confirm")},
        "conversation": {
            "session_ttl_sec": 3600,
            "sessions_dir": str(tmp_path / "sessions"),
            "history_head_turns": 4,
            "history_tail_turns": 16,
        },
    }
    return ConversationManager(config, SlackStub(), task_dir=str(tmp_path / "tasks"))


def msg(text, cid="T1:C1:111.222"):
    return {"conversation_id": cid, "text": text,
            "channel_id": "C1", "team_id": "T1", "thread_ts": "111.222"}


def _run_with_stdout(tmp_path, monkeypatch, stdout):
    """脳 LLM の生 stdout を固定して 1 発話を処理し、マネージャを返す。"""
    monkeypatch.setattr(conv_mod, "run_ollama", lambda *a, **kw: stdout)
    m = make_manager(tmp_path)
    m.handle_message(msg("進めて"))
    return m


def test_truncated_contract_json_not_leaked_to_slack(tmp_path, monkeypatch):
    """切断された契約 JSON（F2 と同型の壊れ出力）が Slack 返信に生で出ない。"""
    # 閉じ括弧欠落 → extract_json でも救えず JSONDecodeError フォールバックへ落ちる
    raw = '{"reply": "リポジトリを確認します", "ready": false, "summary": null'
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    reply = m.slack.sent[-1]
    assert '"reply"' not in reply and '"ready"' not in reply and '"summary"' not in reply, \
        "契約 JSON が Slack 返信へ生で漏れている"
    assert reply == "（応答の整形に失敗しました。もう一度お願いします）"


def test_prose_mixed_broken_contract_json_not_leaked(tmp_path, monkeypatch):
    """散文＋壊れ契約 JSON の混在出力（多重出力の断片）も人向け文言へ縮退する。"""
    raw = ('今すぐ git remote -v を実行し結果のみ報告します\n'
           '{"reply": "実行します", "ready": true,')
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    reply = m.slack.sent[-1]
    assert '"ready"' not in reply and "git remote" not in reply
    assert "exec_confirm" not in m.slack.sent, "壊れ出力の ready 断片で実行へ進んでいる"


def test_python_dict_style_contract_not_leaked(tmp_path, monkeypatch):
    """Python dict 風（シングルクォート）の契約断片も縮退する（json.loads 不能な壊れ形）。"""
    raw = "{'reply': '調査します', 'ready': False, 'summary': None}"
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    reply = m.slack.sent[-1]
    assert "'reply'" not in reply and "'ready'" not in reply, \
        "dict 風の契約断片が Slack 返信へ生で漏れている"
    assert reply == "（応答の整形に失敗しました。もう一度お願いします）"


def test_detection_keeps_conversation_alive(tmp_path, monkeypatch):
    """検出時もタスクを止めず会話継続（ready=false）。着手確認は提示されない。"""
    raw = '"summary": "内部指示書", "ready": true ←不正な裸キー出力'
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert m.slack.sent, "返信自体が出ておらず会話が止まっている"
    assert "exec_confirm" not in m.slack.sent
    assert not os.path.isdir(tmp_path / "confirm") or not os.listdir(tmp_path / "confirm"), \
        "確認レコードが作られている（ready=false が維持されていない）"


def test_degraded_reply_not_persisted_to_history(tmp_path, monkeypatch):
    """縮退文言はシステムメッセージ扱いで履歴に残さない（オウム返し防止）。"""
    raw = '{"reply": "x", "ready": false,'
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    with m._sessions_lock:
        history = m._load_or_create_session("T1:C1:111.222")
    assert all("整形に失敗" not in t["text"] for t in history), "縮退文言が履歴に残っている"
    assert all('"ready"' not in t["text"] for t in history), "壊れ出力が履歴に残っている"


def test_plain_prose_fallback_unchanged(tmp_path, monkeypatch):
    """契約キーを含まない散文のパース不能出力は従来どおり素の stdout が返る（無退行）。"""
    raw = "了解しました。どのリポジトリを対象にしますか？"
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert m.slack.sent[-1] == raw, "従来のフォールバック（素の散文返信）が退行している"


def test_valid_contract_json_unchanged(tmp_path, monkeypatch):
    """正常な契約 JSON はフィルタ対象外: reply がそのまま返る（無退行）。"""
    raw = json.dumps({"reply": "どの repo ですか？", "ready": False, "summary": None})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert m.slack.sent[-1] == "どの repo ですか？"


def test_valid_ready_summary_still_presents_confirm(tmp_path, monkeypatch):
    """正常な ready=true + summary は従来どおり着手確認を提示する（無退行）。"""
    raw = json.dumps({"reply": "", "ready": True, "summary": "X を実装する"})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" in m.slack.sent
