"""worker ホスト認証プリフライト（AuthPreflight）の振る舞いテスト。

検証する振る舞い（存在ではなく挙動）:
- 検査順序と打ち切り: SSH → git → Anthropic の順で、最初の不合格で後続を実行しない
  （原因を 1 経路に確定させる — SSH と Anthropic の混同診断の再発防止）。
- 原因種別: 各経路の失敗が kind（ssh / git / anthropic）とエラー実出力の該当行つきで返る。
- 対象外の扱い: workspace が repo でない / origin 未設定（番兵 exit code）は不合格にしない。
- キャッシュ: PASS は TTL 内で再検査しない（Anthropic プローブを毎回払わない）。FAIL は
  cached 印つきで再送出し（Slack 通知の重複抑止の根拠）、TTL 経過後は再検査する。
- 伏字化: エラー出力に紛れたトークン形式が detail に生のまま載らない。
- seam: _run_worker_headless がプリフライト不合格時に worker を起動せず、初回のみ通知する。

preflight.py 単体は軽量なためファイル直ロードで検証する（重い orchestrator パッケージ
import を避ける）。seam テストのみ orchestrator パッケージを import する
（例外クラスの同一性が捕捉の前提のため、パッケージ側のクラスを使う必要がある）。
"""

import asyncio
import importlib.util
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PF_PATH = os.path.join(_HERE, "..", "preflight.py")


def _load_preflight():
    spec = importlib.util.spec_from_file_location("preflight_under_test", _PF_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = _load_preflight()

_CONF = {"ssh_timeout_sec": 15, "git_timeout_sec": 30, "anthropic_timeout_sec": 120,
         "pass_ttl_sec": 600, "fail_ttl_sec": 60}


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _FakeRun:
    """subprocess.run の差し替え。応答列を順に返し、呼び出し argv を記録する。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append({"argv": argv, "timeout": kw.get("timeout")})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _preflight(responses, clock=None):
    fake = _FakeRun(responses)
    pre = pf.AuthPreflight(ssh_host="mbp", conf=_CONF, clock=clock or _Clock())
    return pre, fake


# ── 検査順序・打ち切り ──

def test_all_pass_runs_checks_in_order(monkeypatch):
    pre, fake = _preflight([_R(0), _R(0), _R(0, stdout="ok")])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    pre.check("/opt/taka-ma/work/t1", "claude")
    assert len(fake.calls) == 3
    assert fake.calls[0]["argv"][-1] == "true"                      # SSH 到達性
    assert "git ls-remote origin HEAD" in fake.calls[1]["argv"][-1]  # git 到達性
    assert "/opt/taka-ma/work/t1" in fake.calls[1]["argv"][-1]
    assert "claude -p ok" in fake.calls[2]["argv"][-1]               # Anthropic プローブ
    assert "-tt" in fake.calls[2]["argv"]                            # 孤児回収（SIGHUP 伝播）
    assert all("BatchMode=yes" in " ".join(c["argv"]) for c in fake.calls)


def test_no_workspace_skips_git_check(monkeypatch):
    pre, fake = _preflight([_R(0), _R(0)])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    pre.check(None, "claude")
    assert len(fake.calls) == 2
    assert "git ls-remote" not in " ".join(fake.calls[1]["argv"])


def test_ssh_failure_stops_before_git_and_anthropic(monkeypatch):
    pre, fake = _preflight([_R(255, stderr="user@mbp: Permission denied (publickey).")])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as ei:
        pre.check("/opt/taka-ma/work/t1")
    assert ei.value.kind == "ssh"
    assert "Permission denied (publickey)." in ei.value.detail
    assert len(fake.calls) == 1  # git / Anthropic には進まない（原因を 1 経路に確定）


def test_ssh_timeout_classified_as_ssh(monkeypatch):
    pre, fake = _preflight([subprocess.TimeoutExpired(cmd="ssh", timeout=15)])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as ei:
        pre.check(None)
    assert ei.value.kind == "ssh"
    assert "タイムアウト" in ei.value.detail


# ── git 検査 ──

def test_git_failure_reports_git_kind_with_error_line(monkeypatch):
    pre, fake = _preflight(
        [_R(0), _R(128, stderr="git@github.com: Permission denied (publickey).\n"
                               "fatal: Could not read from remote repository.")])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as ei:
        pre.check("/opt/taka-ma/work/t1")
    assert ei.value.kind == "git"
    # 該当行 = 最後の非空行（エラーの結論）。Anthropic プローブには進まない
    assert "Could not read from remote repository" in ei.value.detail
    assert len(fake.calls) == 2
    # 報告文は切り分け事実のみで、鍵の再作成・再登録の提案を含まない
    assert "Anthropic 認証の問題ではない" in ei.value.report()
    assert "再作成" not in ei.value.report() and "再登録" not in ei.value.report()


def test_git_skip_sentinel_is_not_a_failure(monkeypatch):
    # workspace が repo でない / origin 未設定（新規 clone 運用）→ 対象外として Anthropic へ進む
    pre, fake = _preflight([_R(0), _R(42), _R(0, stdout="ok")])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    pre.check("/opt/taka-ma/work/t1")
    assert len(fake.calls) == 3


# ── Anthropic 検査 ──

def test_anthropic_failure_reports_anthropic_kind(monkeypatch):
    msg = "Your organization has disabled Claude subscription access for Claude Code"
    pre, fake = _preflight([_R(0), _R(0), _R(1, stdout=msg)])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as ei:
        pre.check("/opt/taka-ma/work/t1")
    assert ei.value.kind == "anthropic"
    assert "disabled Claude subscription access" in ei.value.detail
    assert "SSH は正常" in ei.value.report()  # SSH との混同を防ぐ切り分け事実


# ── キャッシュ ──

def test_pass_cache_skips_reprobe_within_ttl_and_expires(monkeypatch):
    clock = _Clock()
    pre, fake = _preflight([_R(0), _R(0), _R(0), _R(0), _R(0), _R(0)], clock=clock)
    monkeypatch.setattr(pf.subprocess, "run", fake)
    pre.check("/opt/taka-ma/work/t1")
    assert len(fake.calls) == 3
    clock.t += 10          # TTL 内 → 全検査スキップ（プローブ課金なし）
    pre.check("/opt/taka-ma/work/t1")
    assert len(fake.calls) == 3
    clock.t += _CONF["pass_ttl_sec"]  # TTL 経過 → 再検査
    pre.check("/opt/taka-ma/work/t1")
    assert len(fake.calls) == 6


def test_fail_cache_marks_cached_and_expires(monkeypatch):
    clock = _Clock()
    pre, fake = _preflight(
        [_R(255, stderr="Permission denied"), _R(255, stderr="Permission denied")],
        clock=clock)
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as e1:
        pre.check(None)
    assert e1.value.cached is False   # 初回 → 呼び出し側が Slack 通知する
    clock.t += 10
    with pytest.raises(pf.PreflightFailure) as e2:
        pre.check(None)
    assert e2.value.cached is True    # TTL 内の再検出 → 通知を重ねない
    assert len(fake.calls) == 1       # 再検査もしない
    clock.t += _CONF["fail_ttl_sec"]
    with pytest.raises(pf.PreflightFailure):
        pre.check(None)
    assert len(fake.calls) == 2       # TTL 経過 → 再検査（復旧後の再試行を塞がない）


# ── 伏字化 ──

def test_token_like_output_is_redacted(monkeypatch):
    pre, fake = _preflight([_R(255, stderr="auth failed: sk-ant-api03-abcdef0123456789")])
    monkeypatch.setattr(pf.subprocess, "run", fake)
    with pytest.raises(pf.PreflightFailure) as ei:
        pre.check(None)
    assert "sk-ant-" not in ei.value.detail
    assert "[REDACTED]" in ei.value.detail


# ── seam（_run_worker_headless が起動前に検査し、不合格なら起動しない） ──

def test_seam_blocks_worker_and_notifies_once():
    import orchestrator as orch

    o = orch.Orchestrator.__new__(orch.Orchestrator)
    failure = orch.PreflightFailure("ssh", "Permission denied (publickey).")
    checked = []

    class _StubPreflight:
        def check(self, workspace, cli_command="claude"):
            checked.append((workspace, cli_command))
            raise failure

    o.preflight = _StubPreflight()
    notes = []

    async def _notify(text, channel=None, team_id=None, thread_ts=None):
        notes.append(text)

    o._notify = _notify
    # process_mgr / config を持たせない — プリフライトを素通りして worker 起動処理へ進めば
    # AttributeError で落ち、このテストが「起動していない」ことの反証になる

    async def _go():
        await o._run_worker_headless(
            "iid", "claude", "do task", "", "/opt/taka-ma/work/t1",
            channel="C1", team_id=None, task_id="t1", thread_ts=None)

    with pytest.raises(orch.PreflightFailure):
        asyncio.run(_go())
    assert checked == [("/opt/taka-ma/work/t1", "claude")]
    assert len(notes) == 1
    assert "認証プリフライト不合格" in notes[0] and "worker を起動しません" in notes[0]

    # キャッシュ再検出（cached=True）は通知を重ねない
    failure.cached = True
    with pytest.raises(orch.PreflightFailure):
        asyncio.run(_go())
    assert len(notes) == 1
