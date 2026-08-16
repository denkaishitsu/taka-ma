"""脳 LLM 応答の ready キー契約逸脱の振る舞いテスト（#taka-ma/145）。

検証する振る舞い（存在ではなく挙動）:
- ready キー欠落の JSON 応答は会話継続（着手確認を提示しない）へ明示的に縮退し、
  契約逸脱を warning ログへ記録する（発生率の観測点。実測: 2026-08-16 分離実行で
  qwen3.6:35b-a3b・think=false が ready 欠落 JSON を返した）
- ready が boolean 以外（文字列・数値・null）でも同じく会話継続 + warning。
  特に文字列 "false" が従来の bool() 変換で truthy → 誤って着手確認へ進む事故を塞ぐ
- 正常な boolean の true/false は従来どおり（着手確認 / 会話返信）で、契約逸脱の
  warning を出さない（無退行）
- 逸脱応答でも reply は通常の会話返信として届く（会話が止まらない）
"""

import json
import logging
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


def _deviation_records(caplog):
    """契約逸脱の warning ログレコードのみを返す。"""
    return [r for r in caplog.records
            if r.name == "sa-ru.conversation" and r.levelno == logging.WARNING
            and "契約逸脱" in r.getMessage()]


def test_missing_ready_key_falls_back_to_conversation(tmp_path, monkeypatch, caplog):
    """ready キー欠落（実測の逸脱形）→ 会話継続。着手確認は提示されない。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "README を要約します", "summary": None, "probe": None})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" not in m.slack.sent, "ready 欠落で着手確認へ進んでいる"
    assert m.slack.sent[-1] == "README を要約します", "逸脱応答の reply が会話返信に届いていない"
    records = _deviation_records(caplog)
    assert records, "ready キー欠落が warning ログに記録されていない（発生率を観測できない）"
    assert "ready キー欠落" in records[0].getMessage()


def test_missing_ready_with_summary_does_not_execute(tmp_path, monkeypatch, caplog):
    """ready 欠落 + summary あり（実行に見える壊れ形）でも実行確認へは進まない。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "着手します", "summary": "X を実装する", "probe": None})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" not in m.slack.sent, \
        "ready 無しの summary で着手確認へ進んでいる（契約逸脱で実行に進む事故）"
    assert not os.path.isdir(tmp_path / "confirm") or not os.listdir(tmp_path / "confirm")
    assert _deviation_records(caplog)


def test_string_true_ready_falls_back_to_conversation(tmp_path, monkeypatch, caplog):
    """ready="true"（文字列型不正）→ 会話継続 + warning。実行確認へ進まない。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "了解です", "ready": "true", "summary": "X をやる"})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" not in m.slack.sent, "文字列 'true' で着手確認へ進んでいる"
    records = _deviation_records(caplog)
    assert records, "ready 型不正が warning ログに記録されていない"
    assert "boolean でない" in records[0].getMessage()


def test_string_false_ready_not_truthy(tmp_path, monkeypatch, caplog):
    """ready="false"（文字列）が truthy 誤変換で実行へ進まない（旧 bool() 変換の穴）。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "確認します", "ready": "false", "summary": "X をやる"})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" not in m.slack.sent, \
        "文字列 'false' が truthy と誤解釈され着手確認へ進んでいる"
    assert _deviation_records(caplog)


def test_null_ready_falls_back_with_warning(tmp_path, monkeypatch, caplog):
    """ready=null（キーはあるが型不正）→ 会話継続 + warning（欠落とは別の逸脱として記録）。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "どうしますか", "ready": None, "summary": None})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" not in m.slack.sent
    records = _deviation_records(caplog)
    assert records and "boolean でない" in records[0].getMessage()


def test_valid_ready_true_still_presents_confirm(tmp_path, monkeypatch, caplog):
    """正常な boolean true + summary は従来どおり着手確認を提示し、warning を出さない（無退行）。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "", "ready": True, "summary": "X を実装する"})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert "exec_confirm" in m.slack.sent, "正常な ready=true が着手確認に進まない（退行）"
    assert not _deviation_records(caplog), "正常応答に契約逸脱 warning が出ている（誤検知）"


def test_valid_ready_false_replies_without_warning(tmp_path, monkeypatch, caplog):
    """正常な boolean false は従来どおり会話返信で、warning を出さない（無退行）。"""
    caplog.set_level(logging.WARNING, logger="sa-ru.conversation")
    raw = json.dumps({"reply": "どの repo ですか？", "ready": False, "summary": None})
    m = _run_with_stdout(tmp_path, monkeypatch, raw)
    assert m.slack.sent[-1] == "どの repo ですか？"
    assert "exec_confirm" not in m.slack.sent
    assert not _deviation_records(caplog)
