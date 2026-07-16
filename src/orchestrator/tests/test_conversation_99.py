"""会話セッション永続化・エラー区別・結果還流・完了通知の振る舞いテスト。

検証する振る舞い（存在ではなく挙動）:
- セッションはターン追記のたびにディスクへ永続化され、新しいマネージャ（再起動相当）や
  TTL アンロード後も文脈がファイルから回復する（設計書 §8.3）
- 会話 LLM の失敗は原因別に扱われる: タイムアウト/接続失敗は 1 回リトライし、最終失敗の
  返信に原因が明示される。「（内部エラーが発生しました）」は返らない（設計書 §8.3）
- タスク完了結果が発生元セッションへ assistant ターンとして還流される（設計書 §8.9）
- 完了通知は切り詰めず分割送信され、極端な長文のみ打ち切りを明示する（設計書 §8.9）
- _update_status は completed/failed でアーカイブ後の実パスを返す（通知のパス併記の前提）
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest

import ai_gateway.llm as llm_mod
from ai_gateway.llm import OllamaConnectionError, OllamaTimeoutError
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


def test_session_persisted_and_recovered(tmp_path, monkeypatch):
    """ターンがディスクへ永続化され、別インスタンス（再起動相当）で回復する。"""
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: json.dumps({"reply": "了解です", "ready": False}))
    m1 = make_manager(tmp_path)
    m1.handle_message(msg("こんにちは"))

    files = os.listdir(tmp_path / "sessions")
    assert len(files) == 1, "セッションファイルが作られていない"

    m2 = make_manager(tmp_path)  # 再起動相当（in-memory は空）
    with m2._sessions_lock:
        history = m2._load_or_create_session("T1:C1:111.222")
    texts = [t["text"] for t in history]
    assert "こんにちは" in texts and "了解です" in texts, "再起動後に文脈が回復しない"


def test_ttl_unload_keeps_file(tmp_path, monkeypatch):
    """TTL でメモリからアンロードされても永続化ファイルは残る。"""
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: json.dumps({"reply": "x", "ready": False}))
    m = make_manager(tmp_path)
    m.handle_message(msg("最初の発話"))
    m._last_seen["T1:C1:111.222"] -= 999999  # TTL 超過を再現
    with m._sessions_lock:
        m._evict_idle_sessions(__import__("time").monotonic())
    assert "T1:C1:111.222" not in m.sessions, "アンロードされていない"
    assert os.listdir(tmp_path / "sessions"), "永続化ファイルまで消えている"


def test_llm_timeout_retries_once_and_reports_cause(tmp_path, monkeypatch):
    """タイムアウトは 1 回リトライし、最終失敗の返信に原因（秒数）が明示される。
    エラー文言は会話履歴に残さない（脳がオウム返しする実機再現 2026-07-14）。"""
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        raise OllamaTimeoutError("timeout")

    monkeypatch.setattr(conv_mod, "run_ollama", fake_run)
    m = make_manager(tmp_path)
    m.handle_message(msg("遅い質問"))
    assert len(calls) == 2, "リトライが 1 回行われていない"
    reply = m.slack.sent[-1]
    assert "内部エラー" not in reply, "包括表現が残っている"
    assert "5 秒" in reply, "原因（タイムアウト秒数）が明示されていない"
    with m._sessions_lock:
        history = m._load_or_create_session("T1:C1:111.222")
    assert all("秒の上限" not in t["text"] for t in history), "エラー文言が履歴に残っている"


def test_llm_connection_error_reports_cause(tmp_path, monkeypatch):
    """接続失敗はタイムアウトと別の文言で原因が返る。"""
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: (_ for _ in ()).throw(OllamaConnectionError("接続拒否")))
    m = make_manager(tmp_path)
    m.handle_message(msg("質問"))
    reply = m.slack.sent[-1]
    assert "接続できませんでした" in reply and "内部エラー" not in reply


def test_append_task_result_reflows_to_session(tmp_path):
    """タスク完了結果が conversation_id 復元先のセッションへ還流される。"""
    m = make_manager(tmp_path)
    task = {"team_id": "T1", "channel_id": "C1", "thread_ts": "111.222"}
    m.append_task_result(task, "結果本文" * 10, "/opt/taka-ma/data/tasks/done/2026-07-13/x.json")
    with m._sessions_lock:
        history = m._load_or_create_session("T1:C1:111.222")
    assert history and history[-1]["role"] == "assistant"
    assert "/opt/taka-ma/data/tasks/done/2026-07-13/x.json" in history[-1]["text"]


def test_append_task_result_skips_non_conversation_task(tmp_path):
    """会話由来でない（channel 等を欠く）タスクは還流しない。"""
    m = make_manager(tmp_path)
    m.append_task_result({"team_id": "", "channel_id": ""}, "r", "/p")
    assert not os.listdir(tmp_path / "sessions")


def _capture_server():
    """受信 payload を記録し NDJSON の done チャンク 1 行を返すテスト用 ollama サーバ。

    run_ollama は http.client で実 HTTP を張るため（urllib モックでは経路を通らない）、
    ローカルにサーバを立てて request body と応答契約を検証する（test_heartbeat_101 と同方式）。
    """
    import http.server
    import threading

    captured = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            captured["payload"] = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write((json.dumps({"response": "ok", "done": True}) + "\n").encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", captured


def test_run_ollama_sends_numeric_negative_keep_alive():
    """keep_alive は数値の負値で送る（単位なし文字列 "-1" は ollama がパース不能で
    リクエストごと 400 拒否するため。数値・負=無期限常駐が正）。"""
    server, host, captured = _capture_server()
    try:
        assert llm_mod.run_ollama("m", "p", timeout=5, host=host) == "ok"
        ka = captured["payload"]["keep_alive"]
        assert isinstance(ka, (int, float)) and not isinstance(ka, bool) and ka < 0
    finally:
        server.shutdown()


def test_run_ollama_think_param():
    """think は指定時のみ payload に載る（None は省略＝think 非対応モデル互換）。"""
    server, host, captured = _capture_server()
    try:
        llm_mod.run_ollama("m", "p", timeout=5, host=host)
        assert "think" not in captured["payload"], "None 時に think が載っている"
        llm_mod.run_ollama("m", "p", timeout=5, host=host, think=False)
        assert captured["payload"]["think"] is False
    finally:
        server.shutdown()


def test_conversation_passes_think_from_config(tmp_path, monkeypatch):
    """sa-ru.yaml の llm_think が run_ollama まで配線される。"""
    seen = {}

    def fake_run(model, prompt, timeout, host=None, think=None, progress=None):
        seen["think"] = think
        return json.dumps({"reply": "x", "ready": False})

    monkeypatch.setattr(conv_mod, "run_ollama", fake_run)
    m = make_manager(tmp_path)
    m.think = False  # config 由来値の注入（make_manager の config には未設定のため直接）
    m.handle_message(msg("テスト"))
    assert seen["think"] is False


def test_run_ollama_maps_errors():
    """HTTP 層の失敗が原因別例外（RuntimeError 派生）に写像される。"""
    # 接続不能（listener なしポート）→ OllamaConnectionError
    with pytest.raises(OllamaConnectionError):
        llm_mod.run_ollama("m", "p", timeout=1, host="http://127.0.0.1:9")
    # 従来の except RuntimeError（decomposer 等の安全側フォールバック）でも捕捉できる
    with pytest.raises(RuntimeError):
        llm_mod.run_ollama("m", "p", timeout=1, host="http://127.0.0.1:9")
    # タイムアウト例外が RuntimeError 派生であること（フォールバック契約の両立）
    assert issubclass(OllamaTimeoutError, RuntimeError)


def _bare_orchestrator():
    """__init__（SSH/Slack 実接続）を通さずに通知系メソッドだけ検証する。"""
    from orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.slack = SlackStub()
    return o


def test_notify_chunked_no_truncation():
    """上限内の長文は全文が分割送信され、打ち切り文言は出ない。"""
    o = _bare_orchestrator()
    body = "あ" * 8000  # 3500 × 2 + 1000 = 3 通
    asyncio.run(o._notify_chunked("タスク完了（結果ファイル: /p/x.json）:", body))
    sent = o.slack.sent
    assert len(sent) == 3
    assert "/p/x.json" in sent[0]
    joined = "".join(sent)
    assert joined.count("あ") == 8000, "本文が切り詰められている"
    assert "省略" not in joined


def test_notify_chunked_extreme_length_points_to_file():
    """上限超の極端な長文は打ち切りを明示し、結果ファイルへ誘導する。"""
    o = _bare_orchestrator()
    body = "い" * 40000  # 12 チャンク > 上限 8
    asyncio.run(o._notify_chunked("タスク完了（結果ファイル: /p/x.json）:", body))
    assert len(o.slack.sent) == 9  # 8 チャンク + 打ち切り通知
    assert "結果ファイル" in o.slack.sent[-1]


def test_update_status_returns_archived_path(tmp_path):
    """completed でアーカイブ後の done/{日付}/ パスが返る。"""
    from orchestrator import Orchestrator
    o = Orchestrator.__new__(Orchestrator)
    o.task_dir = str(tmp_path)
    o._push_task_context = lambda task: None
    task_file = tmp_path / "20260713_x.json"
    task_file.write_text(json.dumps({"task_id": "x", "status": "in_progress"}))

    dest = asyncio.run(o._update_status(str(task_file), "completed", result="done"))
    assert "/done/" in dest and os.path.exists(dest), "アーカイブ先パスが返らない"
    assert not task_file.exists()
    with open(dest) as f:
        assert f.read().find("done") != -1
