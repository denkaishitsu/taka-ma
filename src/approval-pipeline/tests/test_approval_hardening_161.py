"""Task #161 ハーネス硬化の approval-pipeline 側テスト。

- 読み取り専用ツールの決定的前置フィルタ（§8.4。固定小リスト完全一致は LLM 判定を
  呼ばず Tier 1 確定 — ToolSearch が Tier 3 判定された 2026-08-29 実測の是正）
- qu-e 審査タイムアウトの再試行 1 回と「審査不能」文言（§8.8 審査の可用性 —
  タイムアウト起因の Tier 3 濫発と「危険と判定された」誤読・2026-08-30 実測の是正）
"""

import asyncio
import json
import subprocess
import types

import pytest

from decide_daemon import DecideDaemon, _READ_ONLY_TOOLS
from tier2_handler import Tier2Handler


# ── 読み取り専用ツールの前置フィルタ（decide_daemon） ──

class _MustNotDecidePipeline:
    """decide() へ到達したらテスト失敗にするパイプライン（前置フィルタの証明）。"""

    async def decide(self, *a, **k):
        raise AssertionError("read-only ツールが LLM 判定（decide）へ到達した")


class _Holder:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def get(self):
        return self.pipeline


def _request(tool_name, tool_input=None):
    return json.dumps({"payload": {"tool_name": tool_name,
                                   "tool_input": tool_input or {}}}).encode() + b"\n"


@pytest.mark.parametrize("tool", sorted(_READ_ONLY_TOOLS))
def test_read_only_tools_allowed_without_llm(tool):
    """固定小リストの read-only ツールは decide() を呼ばず即 allow（Tier 1 確定）。"""
    daemon = DecideDaemon("unused.sock", holder=_Holder(_MustNotDecidePipeline()))
    response = asyncio.run(daemon._decide(_request(tool)))
    assert response["allow"] is True
    assert "読み取り専用ツール" in response["reason"]


def test_non_listed_tool_still_goes_to_pipeline():
    """リスト外（Bash / Write 等）は従来どおり中核 decide() へ進む（フィルタの範囲限定）。"""
    calls = []

    class _Pipeline:
        async def decide(self, pending, **kwargs):
            calls.append(pending.tool_name)
            return types.SimpleNamespace(allow=False, reason="tier3")

    daemon = DecideDaemon("unused.sock", holder=_Holder(_Pipeline()))
    response = asyncio.run(daemon._decide(_request("Bash", {"command": "ls"})))
    assert calls == ["Bash"]
    assert response["allow"] is False


# ── qu-e 審査タイムアウト: 再試行 1 回＋「審査不能」文言（tier2_handler） ──

class _Pending:
    tool_name = "Bash"
    tool_input = {"command": "ls"}
    context = ""


def _handler():
    return Tier2Handler(ssh_host="mbp", timeout_sec=1.0)


def test_tier2_timeout_retries_once_then_escalates_with_examination_wording(monkeypatch):
    """タイムアウト → 1 回だけ再試行し、再失敗は「審査不能（qu-e 応答なし）」で
    escalate する（「危険と判定された」と誤読させない・§8.8）。"""
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _run)
    decision = asyncio.run(_handler().handle(_Pending()))
    assert len(calls) == 2                      # 初回＋再試行の計 2 回
    assert not decision.allow and decision.escalate
    assert "審査不能" in decision.reason
    assert "qu-e 応答なし" in decision.reason


def test_tier2_timeout_then_success_does_not_escalate(monkeypatch):
    """初回タイムアウト → 再試行成功なら escalate しない（一過性の詰まりを吸収）。"""
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"decision": "approve", "reason": "", "risk_score": 0.1}),
            stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    decision = asyncio.run(_handler().handle(_Pending()))
    assert len(calls) == 2
    assert decision.allow


def test_tier2_non_timeout_error_does_not_retry(monkeypatch):
    """タイムアウト以外の失敗（SSH 断等）は再試行せず従来どおり escalate（範囲限定）。"""
    calls = []

    def _run(argv, **kwargs):
        calls.append(argv)
        raise OSError("ssh broken")

    monkeypatch.setattr(subprocess, "run", _run)
    decision = asyncio.run(_handler().handle(_Pending()))
    assert len(calls) == 1
    assert not decision.allow and decision.escalate
