"""build_hook_settings のフックコマンド組み立て契約テスト。

検証する振る舞い（設計 Appendix §2.1 の判定実行系・終了コード契約）:
  - フックコマンドが Mac mini の decide クライアント（薄い入口）を SSH で起動し、
    デーモンのソケットとタスク文脈を argv で引き渡す
  - SSH は ControlMaster で多重化される（承認レイテンシの handshake 累積を防ぐ）
  - フックコマンド全体が `|| exit 2` で fail-closed に集約される。SSH 失敗（255）等が
    exit 2 以外で漏れると、Claude Code はフックエラーとして既定権限評価に落とし、
    read 系ツールが承認パイプラインを素通りする（fail-open）
"""

import asyncio
import json

from orchestrator.headless_runner import WorkerHeadlessRunner, build_hook_settings

_CLIENT = "/opt/taka-ma/sa-ru/approval-pipeline/decide_client.py"
_SOCKET = "/opt/taka-ma/data/decide.sock"


def _hook_command(settings: dict) -> str:
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _build(**kwargs) -> str:
    # timeout_sec / python_bin は #103 で yaml SSOT 化され必須引数になった（実効値と同値を渡す）
    kwargs.setdefault("timeout_sec", 310)
    kwargs.setdefault("python_bin", "/opt/taka-ma-env/bin/python3")
    settings = build_hook_settings(
        "mac-mini", _CLIENT, _SOCKET,
        task_id="t1", instance_id="t1-step1-opus", **kwargs)
    return _hook_command(settings)


def test_hook_command_invokes_thin_client_with_socket():
    command = _build()
    assert command.startswith("ssh ")
    assert _CLIENT in command
    assert f"--socket {_SOCKET}" in command
    assert "--task-id t1" in command
    assert "--instance-id t1-step1-opus" in command


def test_hook_command_multiplexes_ssh_with_controlmaster():
    command = _build()
    assert "-o ControlMaster=auto" in command
    assert "-o ControlPersist=600" in command
    assert "-o ControlPath=" in command


def test_hook_command_aggregates_all_failures_to_exit2():
    # SSH 失敗（255）・リモート起動失敗（127）を含む全異常が deny（exit 2）に集約されること。
    assert _build().endswith("|| exit 2")


def test_hook_command_uses_venv_python_by_default():
    # 素の "python3" ではなく、存在が deploy で保証される venv バイナリ
    # （sa-ru.yaml headless.python_bin の実効値）を使う。
    assert "/opt/taka-ma-env/bin/python3" in _build()


def test_hook_command_respects_explicit_python_bin():
    assert "/custom/bin/python3" in _build(python_bin="/custom/bin/python3")


def test_hook_command_bakes_task_context_for_tier3():
    # Tier3 承認リクエストの応答先（§8.10）が argv に焼き込まれること。
    command = _build(team_id="T123", channel="C456", thread_ts="167.89")
    assert "--team-id T123" in command
    assert "--channel C456" in command
    assert "--thread-ts 167.89" in command


def test_hook_timeout_stays_outside_client_wait():
    # フック timeout（sa-ru.yaml 実効値 310 秒）はクライアント応答待ち 308 秒・Tier3 300 秒の外側。
    settings = build_hook_settings("mac-mini", _CLIENT, _SOCKET, timeout_sec=310,
                                   python_bin="/opt/taka-ma-env/bin/python3")
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 310


def test_hook_matcher_is_empty_string_for_all_tools():
    # 全ツール一致は空文字（仕様保証）。"*" が無マッチのバージョンだとフック不発＝
    # 承認ゲート無効（default 許可の read 系が素通り）になるため空文字に固定する。
    settings = build_hook_settings("mac-mini", _CLIENT, _SOCKET, timeout_sec=310,
                                   python_bin="/opt/taka-ma-env/bin/python3")
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == ""


# ── _consume_stream の ANSI 除去 ──

class _FakeStdout:
    """stream-json の各行（bytes）を async iterate する擬似 stdout。"""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()


def test_consume_stream_strips_ansi_from_result():
    """result フィールドに混じった ANSI 制御シーケンスを除去して返す。"""
    runner = WorkerHeadlessRunner("t1-step1-fable")
    # カーソル左移動 \x1b[1D と行クリア \x1b[K を含む result（PTY 由来の混入を模す）
    raw = json.dumps({"type": "result",
                      "result": "\x1b[1DThinking...\x1b[K 完了しました"}).encode() + b"\n"
    result = asyncio.run(runner._consume_stream(_FakeProc([raw])))
    assert "\x1b" not in result.text          # ANSI エスケープが残らない
    assert "完了しました" in result.text       # 本文は保持


def test_consume_stream_strips_ansi_from_assistant_text():
    """result 空フォールバック時、assistant text ブロックも ANSI 除去済みで返る。"""
    runner = WorkerHeadlessRunner("t1-step1-fable")
    assistant = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "\x1b[Khello"}]}}).encode() + b"\n"
    result_line = json.dumps({"type": "result", "result": ""}).encode() + b"\n"
    result = asyncio.run(runner._consume_stream(_FakeProc([assistant, result_line])))
    assert "\x1b" not in result.text
    assert result.text == "hello"


def test_consume_stream_tolerates_null_text_block():
    """text が JSON null の text ブロックでも strip_ansi(None) で落ちない（回帰防止）。"""
    runner = WorkerHeadlessRunner("t1-step1-fable")
    assistant = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": None}, {"type": "text", "text": "ok"}]}}).encode() + b"\n"
    result_line = json.dumps({"type": "result", "result": None}).encode() + b"\n"
    # null text/result でクラッシュせず、有効テキストだけ拾う
    result = asyncio.run(runner._consume_stream(_FakeProc([assistant, result_line])))
    assert result.text == "ok"
