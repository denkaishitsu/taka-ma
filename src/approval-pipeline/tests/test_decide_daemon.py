"""decide デーモンの契約テスト（設計 Appendix §2.1）。

検証する振る舞い:
  - リクエスト 1 行 JSON → PendingApproval ＋タスク文脈で中核 decide() を呼び、
    {"allow", "reason"} を 1 行 JSON で返す（producer=decide_client ⇄ consumer 契約）
  - fail-closed: 判定内の例外・不正リクエスト・判定タイムアウトは全て allow=false で
    その接続にのみ返る（デーモンは落ちない＝直後の判定が正常に通ることで確認）
  - 並行性: 遅い判定（Tier3 人間待ち相当）が他の判定をブロックしない

ApprovalPipeline 本体（ya-ta / qu-e / Slack 依存）は FakeHolder で注入し、
「リクエスト → 中核呼び出し → 応答」の変換層だけを分離検証する。
pytest-asyncio に依存せず、各テストは asyncio.run() で同期駆動する。

構築手順書: docs/procedures/08-approval-pipeline.md
"""

import asyncio
import json
import os
import tempfile
import time

from decide_daemon import DecideDaemon


class FakeDecision:
    def __init__(self, allow: bool, reason: str = ""):
        self.allow = allow
        self.reason = reason


class FakePipeline:
    """decide() の呼び出しを記録し、指定の Decision / 例外 / 遅延を返す。"""

    def __init__(self, allow: bool = True, reason: str = "ok",
                 error: Exception | None = None, delay_by_tool: dict | None = None):
        self.allow = allow
        self.reason = reason
        self.error = error
        self.delay_by_tool = delay_by_tool or {}
        self.calls: list[tuple] = []

    async def decide(self, pending, **ctx):
        delay = self.delay_by_tool.get(pending.tool_name, 0)
        if delay:
            await asyncio.sleep(delay)
        if self.error:
            raise self.error
        self.calls.append((pending, ctx))
        return FakeDecision(self.allow, self.reason)


class FakeHolder:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def get(self):
        return self.pipeline


def _socket_path(tmpdir: str) -> str:
    return os.path.join(tmpdir, "decide.sock")


async def _send(socket_path: str, line: bytes) -> dict:
    """テスト用クライアント: 1 リクエストを送り応答 JSON を返す。"""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(line)
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    return json.loads(raw)


async def _with_server(daemon: DecideDaemon, coro):
    """daemon.start()（本番と同一の待ち受け生成）の下でテスト本体 coro を実行する。"""
    server = await daemon.start()
    try:
        return await coro
    finally:
        server.close()
        await server.wait_closed()


def _request_line(**over) -> bytes:
    req = {
        "payload": {"tool_name": "Bash", "tool_input": {"command": "ls"},
                    "tool_use_id": "tu1", "cwd": "/opt/taka-ma/work/task-9"},
        "task_id": "task-1", "team_id": "T1", "channel": "C1",
        "thread_ts": "th1", "instance_id": "i1",
    }
    req.update(over)
    return json.dumps(req).encode() + b"\n"


def test_allow_roundtrip_and_context_passthrough():
    """payload とタスク文脈が中核 decide() へそのまま届き、allow が返る。"""
    pipeline = FakePipeline(allow=True, reason="tier1 auto")
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline))
        response = asyncio.run(_with_server(daemon, _send(daemon.socket_path, _request_line())))
    assert response == {"allow": True, "reason": "tier1 auto"}
    pending, ctx = pipeline.calls[0]
    assert (pending.tool_name, pending.tool_input, pending.tool_use_id) == \
        ("Bash", {"command": "ls"}, "tu1")
    # deadline はデーモンが decide_timeout から算出して渡す（Tier3 の人間待ちを外側
    # タイムアウトの内側に収めるための締切・monotonic 基準）。値は実行時刻依存のため
    # 「未来の時刻である」ことだけ検証し、他のコンテキストは完全一致で検証する。
    deadline = ctx.pop("deadline")
    assert isinstance(deadline, float) and deadline > time.monotonic()
    assert ctx == {"instance_id": "i1", "team_id": "T1", "channel": "C1",
                   "task_id": "task-1", "thread_ts": "th1"}


def test_deny_roundtrip():
    pipeline = FakePipeline(allow=False, reason="always_deny")
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline))
        response = asyncio.run(_with_server(daemon, _send(daemon.socket_path, _request_line())))
    assert response == {"allow": False, "reason": "always_deny"}


def test_task_id_falls_back_to_cwd_basename():
    """argv の task_id 未指定時は cwd（/opt/taka-ma/work/{task_id}）末尾から補う。"""
    pipeline = FakePipeline()
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline))
        asyncio.run(_with_server(daemon, _send(daemon.socket_path, _request_line(task_id=""))))
    assert pipeline.calls[0][1]["task_id"] == "task-9"


def test_pipeline_exception_is_denied_and_daemon_survives():
    """判定内の例外はその接続への deny に閉じ、デーモンは次の判定を正常に捌ける。"""
    broken = FakePipeline(error=RuntimeError("ya-ta down"))
    healthy = FakePipeline(allow=True, reason="ok")
    holder = FakeHolder(broken)

    async def scenario(daemon):
        first = await _send(daemon.socket_path, _request_line())
        holder.pipeline = healthy  # 障害復旧を模擬
        second = await _send(daemon.socket_path, _request_line())
        return first, second

    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=holder)
        first, second = asyncio.run(_with_server(daemon, scenario(daemon)))
    assert first["allow"] is False and "fail-safe deny" in first["reason"]
    assert second == {"allow": True, "reason": "ok"}


def test_invalid_request_json_is_denied():
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(FakePipeline()))
        response = asyncio.run(
            _with_server(daemon, _send(daemon.socket_path, b"not-json\n")))
    assert response["allow"] is False and "fail-safe deny" in response["reason"]


def test_decide_timeout_is_denied():
    """判定側のハングは decide_timeout で打ち切り、deny で確定する。"""
    pipeline = FakePipeline(delay_by_tool={"Bash": 0.5})
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline),
                              decide_timeout=0.05)
        response = asyncio.run(_with_server(daemon, _send(daemon.socket_path, _request_line())))
    assert response["allow"] is False and "fail-safe deny" in response["reason"]


def test_large_tool_input_is_not_denied():
    """Write 等の大きな tool_input（>64KB）が、ストリーム limit 起因で誤 deny されない。

    asyncio ストリームの既定 limit は 64KB で、超過行は LimitOverrunError → fail-safe deny に
    落ちてしまう（正当なツールが内容の大きさだけで拒否される欠陥）。daemon.start() が limit を
    引き上げていることを、実際に大きな payload の往復で確認する。
    """
    pipeline = FakePipeline(allow=True, reason="ok")
    big_content = "x" * 200_000  # 64KB 既定 limit を確実に超えるサイズ
    line = _request_line(payload={"tool_name": "Write", "tool_use_id": "w1",
                                  "tool_input": {"file_path": "/tmp/a", "content": big_content}})
    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline))
        response = asyncio.run(_with_server(daemon, _send(daemon.socket_path, line)))
    assert response == {"allow": True, "reason": "ok"}
    assert pipeline.calls[0][0].tool_input["content"] == big_content


def test_slow_decision_does_not_block_others():
    """Tier3 人間待ち相当の遅い判定中も、他の判定は並行して先に返る（Appendix §2.1）。"""
    pipeline = FakePipeline(delay_by_tool={"SlowTool": 0.4})

    async def scenario(daemon):
        slow = asyncio.create_task(_send(daemon.socket_path, _request_line(
            payload={"tool_name": "SlowTool", "tool_input": {}, "tool_use_id": "s1"})))
        await asyncio.sleep(0.05)  # slow の判定開始を待ってから fast を投入
        start = time.monotonic()
        fast = await _send(daemon.socket_path, _request_line())
        fast_elapsed = time.monotonic() - start
        return fast, fast_elapsed, await slow

    with tempfile.TemporaryDirectory() as tmp:
        daemon = DecideDaemon(_socket_path(tmp), holder=FakeHolder(pipeline))
        fast, fast_elapsed, slow = asyncio.run(_with_server(daemon, scenario(daemon)))
    assert fast["allow"] is True and slow["allow"] is True
    # 直列処理なら fast は slow の残り（>0.3 秒）を待たされる。並行なら即時に返る。
    assert fast_elapsed < 0.3
