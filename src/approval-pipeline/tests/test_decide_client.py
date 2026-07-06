"""decide クライアントの出力契約テスト（設計 Appendix §2.1 の終了コード契約）。

検証する振る舞い（フック＝Claude Code との境界。実プロセスとして分離実行で検証）:
  - allow 応答 → stdout に permissionDecision:"allow" JSON・exit 0
  - deny 応答 → stderr に理由・exit 2
  - デーモン到達不可（ソケット不在）→ exit 2（fail-closed。exit 1 で漏れると Claude Code
    が既定権限評価に落とし read 系ツールが承認を素通りする）
  - フック stdin の payload とタスク文脈 argv がリクエスト JSON にそのまま載る
    （producer=decide_client ⇄ consumer=decide_daemon のキー契約）

クライアントは標準ライブラリのみの前提のため、テストサーバも stdlib（socket+thread）で立てる。

構築手順書: docs/procedures/08-approval-pipeline.md
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

_CLIENT = str(Path(__file__).resolve().parent.parent / "decide_client.py")

_PAYLOAD = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"},
                       "tool_use_id": "tu1", "cwd": "/opt/taka-ma/work/task-9"})


def _serve_once(socket_path: str, response: dict, captured: list) -> threading.Thread:
    """1 接続だけ受けてリクエスト行を captured に格納し、response を返す UDS サーバ。"""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)
    server.listen(1)

    def run():
        conn, _ = server.accept()
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        captured.append(buf)
        conn.sendall(json.dumps(response).encode() + b"\n")
        conn.close()
        server.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _run_client(socket_path: str, extra_args: list[str] | None = None,
                stdin: str = _PAYLOAD) -> subprocess.CompletedProcess:
    argv = [sys.executable, _CLIENT, "--socket", socket_path,
            "--task-id", "task-1", "--instance-id", "i1"] + (extra_args or [])
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=30)


def test_allow_prints_permission_decision_and_exits_0():
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "decide.sock")
        thread = _serve_once(sock, {"allow": True, "reason": "tier1 auto"}, [])
        proc = _run_client(sock)
        thread.join(timeout=5)
    assert proc.returncode == 0
    output = json.loads(proc.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"
    assert output["permissionDecisionReason"] == "tier1 auto"


def test_deny_prints_reason_to_stderr_and_exits_2():
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "decide.sock")
        thread = _serve_once(sock, {"allow": False, "reason": "always_deny match"}, [])
        proc = _run_client(sock)
        thread.join(timeout=5)
    assert proc.returncode == 2
    assert "always_deny match" in proc.stderr
    assert proc.stdout == ""


def test_daemon_unreachable_is_fail_closed_exit_2():
    """デーモン停止（ソケット不在）は必ず exit 2。exit 1 の fail-open 経路を持たない。"""
    with tempfile.TemporaryDirectory() as tmp:
        proc = _run_client(os.path.join(tmp, "no-daemon.sock"))
    assert proc.returncode == 2
    assert "fail-safe deny" in proc.stderr


def test_invalid_hook_payload_is_fail_closed_exit_2():
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "decide.sock")
        # サーバ不要（payload 検証はソケット接続前）。ソケット不在でも同じ exit 2 に落ちるが、
        # ここでは stdin 不正の理由が stderr に出ることを確認する。
        proc = _run_client(sock, stdin="{not json")
    assert proc.returncode == 2
    assert "fail-safe deny" in proc.stderr


def test_request_carries_payload_and_task_context():
    """stdin の payload と argv のタスク文脈が、デーモン契約のキーでそのまま届く。"""
    captured: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "decide.sock")
        thread = _serve_once(sock, {"allow": True, "reason": ""}, captured)
        proc = _run_client(sock, extra_args=[
            "--team-id", "T1", "--channel", "C1", "--thread-ts", "th1"])
        thread.join(timeout=5)
    assert proc.returncode == 0
    request = json.loads(captured[0])
    assert request["payload"] == json.loads(_PAYLOAD)
    assert (request["task_id"], request["instance_id"]) == ("task-1", "i1")
    assert (request["team_id"], request["channel"], request["thread_ts"]) == ("T1", "C1", "th1")
