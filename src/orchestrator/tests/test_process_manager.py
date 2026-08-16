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


# ── inline レーン（type=local）の HTTP API 実行（#135 レイテンシ改善） ──
#
# 検証する振る舞い: ローカルモデルの単発生成が `ollama run`（CLI 単発・モデル再ロード）ではなく
# ollama の HTTP API 呼び出しになり、keep_alive でモデルを常駐させること。CLI 起動に戻ると
# 呼び出しごとのモデルロードが復活し、inline レーンが再び数十秒級になる。

_GEMMA = {"type": "local", "command": "ollama", "model_id": "gemma4:31b",
          "api_url": "http://localhost:11434/api/generate", "keep_alive_sec": 1800}


def test_local_model_uses_http_api_with_keep_alive(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["input"] = kw.get("input")
        return _R(stdout='{"response": "hello", "done": true}')

    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    out = _mgr().run_model_subprocess("gemma", _GEMMA, "何か書いて")

    assert out == "hello"
    remote = seen["cmd"][-1]
    assert "curl" in remote and _GEMMA["api_url"] in remote
    assert " run " not in remote          # CLI 単発（ollama run）に戻っていないこと
    body = __import__("json").loads(seen["input"])
    assert body["model"] == "gemma4:31b"
    assert body["keep_alive"] == 1800     # 常駐させる（毎回ロードしない）
    assert body["prompt"] == "何か書いて"
    assert body["stream"] is False


def test_local_model_error_response_raises(monkeypatch):
    monkeypatch.setattr(pm.subprocess, "run",
                        lambda cmd, **kw: _R(stdout='{"error": "model not found"}'))
    with pytest.raises(RuntimeError, match="model not found"):
        _mgr().run_model_subprocess("gemma", _GEMMA, "x")


def test_local_model_non_json_response_raises(monkeypatch):
    # ollama 未起動等で HTML/空応答が返ったとき、空文字を生成結果として返さない
    monkeypatch.setattr(pm.subprocess, "run",
                        lambda cmd, **kw: _R(stdout="curl: (7) Failed to connect"))
    with pytest.raises(RuntimeError, match="解釈できません"):
        _mgr().run_model_subprocess("gemma", _GEMMA, "x")


def test_api_model_still_uses_cli(monkeypatch):
    # 外部 API CLI（type=api）は従来どおり CLI 起動＋stdin 渡しのまま（回帰防止）
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        return _R(stdout="ok")

    monkeypatch.setattr(pm.subprocess, "run", fake_run)
    out = _mgr().run_model_subprocess(
        "gemini", {"type": "api", "command": "agy", "model_flag": "-p"}, "prompt本文")
    assert out == "ok" and seen["cmd"][-1] == "agy -p" and seen["input"] == "prompt本文"
