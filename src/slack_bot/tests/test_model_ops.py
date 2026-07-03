"""model_ops 単体テスト — SSH コマンド組み立てのインジェクション防止。

実 SSH は張らず subprocess.run を差し替えて、worker へ渡るコマンド文字列を検査する。
狙い: 管理者入力由来の model_id にシェルメタ文字が含まれても 1 引数に確定すること。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
"""

import shlex
import subprocess

import pytest

from services import model_ops


@pytest.fixture
def captured_ssh(monkeypatch):
    """subprocess.run を差し替え、最後に渡された argv を記録する（rc=0 を返す）。"""
    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(model_ops.subprocess, "run", fake_run)
    return calls


def test_pull_model_quotes_model_id(captured_ssh):
    model_ops.pull_model("x; rm -rf /opt/taka-ma")
    argv = captured_ssh[-1]
    # argv = ["ssh", host, remote]。remote 内で危険な ; は引用符の内側に閉じ込められる
    remote = argv[-1]
    assert remote == f"ollama pull {shlex.quote('x; rm -rf /opt/taka-ma')}"
    # 単独トークンとして 1 語に確定している（quote 後を除けばメタ文字が露出しない）
    assert remote.replace(shlex.quote("x; rm -rf /opt/taka-ma"), "") == "ollama pull "


def test_remove_worker_model_quotes_model_id(captured_ssh):
    model_ops.remove_worker_model("a && reboot")
    remote = captured_ssh[-1][-1]
    assert remote == f"ollama rm {shlex.quote('a && reboot')}"


def test_ssh_nonzero_raises_runtimeerror(monkeypatch):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(model_ops.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(RuntimeError):
        model_ops.pull_model("m:1")
