"""task-101（LLM 処理待ちのハートビート進捗通知）の振る舞いテスト。

grep/AST では潰せない振る舞い（NDJSON 逐次受信とトークン数の共有、deadline 打ち切り、
接続失敗のフォールバック契約、間隔ごとの進捗通知と完了時の即時打ち切り、通知失敗の隔離）を
分離実行で担保する。設計書 §8.4「ollama 実行失敗の検知」/ §10.8。

run_ollama は実 HTTP を検証対象に含むため、ローカルにテスト用 HTTP サーバを立てて
/api/generate の NDJSON 応答を模す（ollama 実機は不要）。
_run_with_heartbeat は test_execute_chain_86 と同じく __new__ で本体構築を回避して呼ぶ。
"""
import asyncio
import http.server
import json
import os
import sys
import threading
import time
import types

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway.llm import (  # noqa: E402
    GenerationProgress,
    OllamaTimeoutError,
    run_ollama,
)
from orchestrator import Orchestrator  # noqa: E402


# ── テスト用 ollama サーバ（/api/generate の NDJSON ストリームを模す） ──

def _serve(chunks, status=200, chunk_delay=0.0):
    """指定チャンク列を NDJSON で返す HTTP サーバを起動し、(server, host_url) を返す。"""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for c in chunks:
                if chunk_delay:
                    time.sleep(chunk_delay)
                self.wfile.write((json.dumps(c) + "\n").encode())
                self.wfile.flush()

        def log_message(self, *args):
            pass  # テスト出力を汚さない

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ── run_ollama: NDJSON 逐次受信・進捗共有・失敗契約（§8.4） ──

def test_run_ollama_streams_and_reports_tokens():
    """response 断片が結合され、progress に受信数→eval_count 確定が刻まれる。thinking は本文に混ぜない。"""
    server, host = _serve([
        {"thinking": "考え中"},
        {"response": "{\"a\":"},
        {"response": " 1}"},
        {"done": True, "response": "", "eval_count": 42},
    ])
    try:
        progress = GenerationProgress()
        out = run_ollama("dummy", "p", timeout=5, host=host, progress=progress)
        assert out == '{"a": 1}'
        assert progress.tokens == 42  # done チャンクの eval_count で確定
    finally:
        server.shutdown()


def test_run_ollama_counts_chunks_without_eval_count():
    """eval_count が無くても受信チャンク数（thinking 含む）が進捗として残る。"""
    server, host = _serve([
        {"thinking": "t"},
        {"response": "x"},
        {"response": "y"},
        {"done": True, "response": ""},
    ])
    try:
        progress = GenerationProgress()
        run_ollama("dummy", "p", timeout=5, host=host, progress=progress)
        assert progress.tokens == 3
    finally:
        server.shutdown()


def test_run_ollama_http_error_raises_runtime_error():
    """HTTP エラー応答は RuntimeError（呼び出し側の安全側フォールバック契約）。"""
    server, host = _serve([], status=500)
    try:
        with pytest.raises(RuntimeError):
            run_ollama("dummy", "p", timeout=5, host=host)
    finally:
        server.shutdown()


def test_run_ollama_error_chunk_raises_runtime_error():
    """ストリーム中のエラーチャンク（モデル未 pull 等）は RuntimeError。"""
    server, host = _serve([{"error": "model not found"}])
    try:
        with pytest.raises(RuntimeError):
            run_ollama("dummy", "p", timeout=5, host=host)
    finally:
        server.shutdown()


def test_run_ollama_connection_refused_raises_runtime_error():
    """接続不能（ollama 未起動）は RuntimeError（旧 subprocess 経路の非ゼロ終了と同じ契約）。"""
    with pytest.raises(RuntimeError):
        run_ollama("dummy", "p", timeout=5, host="http://127.0.0.1:9")  # discard port（未使用）


def test_run_ollama_deadline_cuts_off_slow_stream():
    """逐次受信が続いていても生成全体が timeout を超えたら打ち切る（deadline 方式）。"""
    server, host = _serve(
        [{"response": "a"}] * 50 + [{"done": True, "response": ""}], chunk_delay=0.2)
    try:
        start = time.monotonic()
        with pytest.raises(OllamaTimeoutError):
            run_ollama("dummy", "p", timeout=1, host=host)
        assert time.monotonic() - start < 5  # 50 チャンク×0.2 秒を待ちきらず打ち切った
    finally:
        server.shutdown()


# ── _run_with_heartbeat: 間隔ごとの進捗通知・完了で打ち切り・失敗の隔離（§10.8） ──

def _bare(interval=0.05):
    """__init__ を通さず Orchestrator を作り、ハートビートに必要な最小属性だけ与える。"""
    o = Orchestrator.__new__(Orchestrator)
    o._heartbeat_interval = interval
    o.notes = []
    async def _notify(text, channel=None, *, team_id=None, thread_ts=None):
        o.notes.append({"text": text, "channel": channel,
                        "team_id": team_id, "thread_ts": thread_ts})
    o._notify = _notify
    return o


def test_heartbeat_reports_progress_until_completion():
    """処理中は間隔ごとに（ラベル・経過秒・トークン数入りで）通知し、完了後は通知しない。"""
    o = _bare()

    def slow(progress=None):
        progress.tokens = 7
        time.sleep(0.18)
        return "result"

    result = asyncio.run(o._run_with_heartbeat(
        "タスク分解", slow, channel="C1", team_id="T1", thread_ts="1.2"))
    assert result == "result"
    assert len(o.notes) >= 2  # 0.18 秒 / 0.05 間隔 → 完了までに複数回
    n = o.notes[0]
    assert "タスク分解" in n["text"] and "秒経過" in n["text"] and "7 トークン" in n["text"]
    assert (n["channel"], n["team_id"], n["thread_ts"]) == ("C1", "T1", "1.2")
    count_at_done = len(o.notes)
    time.sleep(0.15)  # 完了後にハートビートが生き残っていれば notes が増える
    assert len(o.notes) == count_at_done


def test_heartbeat_fast_completion_sends_nothing():
    """間隔より早く完了した処理には進捗を出さない（ノイズを作らない）。"""
    o = _bare(interval=1)
    result = asyncio.run(o._run_with_heartbeat("会話応答の生成", lambda progress=None: 5))
    assert result == 5
    assert o.notes == []


def test_heartbeat_notify_failure_does_not_break_work():
    """ハートビート送信の失敗は本処理に影響させない（結果はそのまま返る）。"""
    o = _bare()
    async def _broken_notify(*a, **k):
        raise RuntimeError("slack down")
    o._notify = _broken_notify

    def slow(progress=None):
        time.sleep(0.12)
        return "ok"

    assert asyncio.run(o._run_with_heartbeat("タスク分解", slow)) == "ok"


def test_heartbeat_propagates_work_exception():
    """本処理の例外は元のまま呼び出し元へ再送出される（握りつぶさない）。"""
    o = _bare(interval=1)

    def boom(progress=None):
        raise ValueError("decompose failed")

    with pytest.raises(ValueError, match="decompose failed"):
        asyncio.run(o._run_with_heartbeat("タスク分解", boom))
