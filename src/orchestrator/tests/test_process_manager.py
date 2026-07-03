"""RemoteProcessManager.stop_ollama の成否契約テスト。

検証する振る舞い（存在ではなく挙動）:
- SSH/ps 失敗時に ok=False を返す（Slack §8.10c が「停止しました」を偽報告しない根拠）。
- 稼働モデル無しは ok=True・stopped=[]（停止不要）。
- 全モデル停止成功で ok=True・stopped に列挙。
- 一部モデル停止失敗で ok=False・failed に列挙し、残りは stopped に入る（中断しない・§7.1）。

orchestrator パッケージ本体は pexpect/watchdog 等の重い依存を引くため、process_manager.py を
軽量スタブ（orchestrator / pty_wrapper）下でファイル直ロードし、subprocess.run を差し替える。
"""

import importlib.util
import os
import sys
import types

import pytest

_HERE = os.path.dirname(__file__)
_PM_PATH = os.path.join(_HERE, "..", "process_manager.py")


def _load_pm():
    # 重い __init__ と pexpect を避けるため orchestrator パッケージと pty_wrapper をスタブ化。
    if "orchestrator" not in sys.modules:
        pkg = types.ModuleType("orchestrator")
        pkg.__path__ = [os.path.join(_HERE, "..")]
        sys.modules["orchestrator"] = pkg
    ptw = types.ModuleType("orchestrator.pty_wrapper")
    ptw.ClaudeCodeWrapper = type("ClaudeCodeWrapper", (), {})
    sys.modules["orchestrator.pty_wrapper"] = ptw
    spec = importlib.util.spec_from_file_location("orchestrator.process_manager", _PM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load_pm()


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _mgr():
    return pm.RemoteProcessManager(ssh_host="mbp", ssh_timeout=5)


_PS_HEADER = "NAME    ID    SIZE    PROCESSOR    UNTIL\n"


def test_ps_unreachable_returns_not_ok(monkeypatch):
    def fake_run(cmd, **kw):
        raise OSError("ssh unreachable")
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    r = _mgr().stop_ollama()
    assert r["ok"] is False and r["stopped"] == [] and r["reason"]


def test_ps_nonzero_returns_not_ok(monkeypatch):
    monkeypatch.setattr(pm.subprocess, "run", lambda cmd, **kw: _R(returncode=1, stderr="boom"))
    r = _mgr().stop_ollama()
    assert r["ok"] is False and "ps 失敗" in r["reason"]


def test_no_models_is_ok_empty(monkeypatch):
    monkeypatch.setattr(pm.subprocess, "run", lambda cmd, **kw: _R(stdout=_PS_HEADER))
    r = _mgr().stop_ollama()
    assert r == {"ok": True, "stopped": [], "failed": [], "reason": None}


def test_all_models_stopped(monkeypatch):
    def fake_run(cmd, **kw):
        if "ps" in cmd:
            return _R(stdout=_PS_HEADER + "gemma4:12b x 1G gpu 5m\nqwen3:8b y 1G gpu 5m\n")
        return _R(returncode=0)  # stop
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    r = _mgr().stop_ollama()
    assert r["ok"] is True and r["stopped"] == ["gemma4:12b", "qwen3:8b"] and r["failed"] == []


def test_partial_failure_is_not_ok(monkeypatch):
    def fake_run(cmd, **kw):
        if "ps" in cmd:
            return _R(stdout=_PS_HEADER + "good:1 x 1G gpu 5m\nbad:1 y 1G gpu 5m\n")
        return _R(returncode=0) if "good:1" in cmd else _R(returncode=1, stderr="nope")
    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    r = _mgr().stop_ollama()
    assert r["ok"] is False and r["stopped"] == ["good:1"] and r["failed"] == ["bad:1"]
