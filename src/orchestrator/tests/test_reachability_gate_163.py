"""到達性ゲートと固定行の前置（設計書 §8.3「到達性の機械付与」・§8.4「到達性ゲート」・
ADR 0002・#taka-ma/163.2）の振る舞いテスト。

検証する振る舞い:
- AuthPreflight.check_ssh は ssh 検査だけを行い（git / Anthropic プローブを走らせない）、
  到達状態（ssh_reachable / last_ssh_ok）を更新し、キャッシュを check() と共用する。
- MBP 不達のとき、ready=true の依頼は契約化（Contractor）を呼ばずに固定文で止まり、
  発話から返信までが 20 秒以内（TCP タイムアウト 75 秒 x 2 + ローカル生成の 5 分を廃す）。
- 不達のとき、会話継続の返信は先頭に固定行を持つが、会話履歴（脳の文脈）には残らない。
- 到達可能・preflight 未注入では固定行を付けず、従来どおり動く。
"""

import importlib.util
import os
import sys
import tempfile
import time

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import conversation as conv_mod  # noqa: E402
from orchestrator.conversation import (  # noqa: E402
    UNREACHABLE_STOP_TEXT, ConversationManager)
from orchestrator.preflight import AuthPreflight, PreflightFailure  # noqa: E402

_CONF = {"ssh_timeout_sec": 15, "git_timeout_sec": 30, "anthropic_timeout_sec": 120,
         "pass_ttl_sec": 600, "fail_ttl_sec": 60}


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class _FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _preflight(responses, clock=None, wall=None):
    pf = AuthPreflight("mbp", _CONF, clock=clock or _Clock(),
                       wall_clock=wall or (lambda: 1_700_000_000.0))
    fake = _FakeRun(responses)
    pf._run = lambda remote_cmd, timeout, tty=False: fake(
        ["ssh", "mbp", remote_cmd], timeout=timeout)
    return pf, fake


# ── AuthPreflight.check_ssh ──

def test_check_ssh_runs_only_ssh_probe_and_records_reachability():
    pf, fake = _preflight([_R(0)])
    pf.check_ssh()
    assert [c[2] for c in fake.calls] == ["true"]        # git / Anthropic は走らない
    assert pf.ssh_reachable is True
    assert pf.last_ssh_ok == 1_700_000_000.0


def test_check_ssh_failure_sets_unreachable_and_keeps_last_ok():
    import subprocess
    wall = _Clock(1_700_000_000.0)
    clock = _Clock()
    pf, fake = _preflight([_R(0), subprocess.TimeoutExpired("ssh", 15)],
                          clock=clock, wall=wall)
    pf.check_ssh()
    clock.t += 700                                      # pass_ttl 経過
    wall.t += 700
    with pytest.raises(PreflightFailure) as ei:
        pf.check_ssh()
    assert ei.value.kind == "ssh"
    assert pf.ssh_reachable is False
    assert pf.last_ssh_ok == 1_700_000_000.0             # 最終疎通は合格時のまま


def test_check_ssh_shares_cache_with_check():
    """不達の fail_ttl 内は再検査せず即時に同じ不合格（連続ターンで 15 秒を払わない）。"""
    import subprocess
    pf, fake = _preflight([subprocess.TimeoutExpired("ssh", 15)])
    with pytest.raises(PreflightFailure):
        pf.check_ssh()
    with pytest.raises(PreflightFailure) as ei:
        pf.check_ssh()
    assert ei.value.cached is True
    assert len(fake.calls) == 1


# ── ConversationManager: 到達性ゲート ──

class _Notifier:
    def __init__(self):
        self.notes, self.confirms = [], []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, *a, **k):
        self.confirms.append((a, k))


class _UnreachablePreflight:
    last_ssh_ok = 1_700_000_000.0

    def check_ssh(self):
        raise PreflightFailure("ssh", "SSH 応答なし（15秒でタイムアウト）")


class _ReachablePreflight:
    last_ssh_ok = 1_700_000_000.0

    def check_ssh(self):
        return None


class _MustNotContract:
    def contract(self, *a, **k):
        raise AssertionError("不達中に契約化を呼んではならない（§8.4 到達性ゲート）")


class _IntentStub:
    def __init__(self, action="execute"):
        self.action = action

    def classify(self, history_text, latest_text):
        return {"action": self.action, "confidence": 1.0, "evidence": latest_text[:10],
                "origin": "stub", "escalated": False, "fail_closed": False}


def _manager(preflight, llm_result):
    tmp = tempfile.mkdtemp(prefix="conv-")
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp},
        "conversation": {"sessions_dir": tempfile.mkdtemp(prefix="sessions-"),
                         "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work"},
        "ya-ta": {"model": "dummy", "llm_timeout_sec": 60},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    mgr = ConversationManager(config, _Notifier(), task_dir=tmp, preflight=preflight)
    # 判定は IntentClassifier。旧 llm_result の ready から intent を組み立てる
    mgr.intent = _IntentStub("execute" if llm_result.get("ready") else "chat")
    mgr._invoke_llm = lambda *a, **k: {"reply": llm_result.get("reply", "")}
    mgr._claims_check = lambda *a, **k: {"progress": False, "state": False}
    mgr.contractor = _MustNotContract()
    return mgr


def _msg(text="README を更新して push して"):
    return {"conversation_id": "c1", "text": text, "channel_id": "C1",
            "team_id": "T1", "thread_ts": "1.0"}


def test_ready_request_stops_with_fixed_text_when_unreachable():
    mgr = _manager(_UnreachablePreflight(),
                   {"reply": "", "ready": True, "summary": "README 更新と push"})
    t0 = time.monotonic()
    mgr.handle_message(_msg())
    elapsed = time.monotonic() - t0
    assert elapsed < 20, elapsed                         # 完了条件 (i): 20 秒以内
    assert mgr.slack.confirms == []                       # 着手確認を出さない
    text = mgr.slack.notes[-1]
    assert text.startswith("⛔ MBP 到達不能（最終疎通 ")
    assert UNREACHABLE_STOP_TEXT in text


def test_conversation_reply_is_prefixed_but_history_is_not():
    mgr = _manager(_UnreachablePreflight(),
                   {"reply": "どのファイルですか？", "ready": False})
    mgr.handle_message(_msg("ちょっと相談"))
    text = mgr.slack.notes[-1]
    assert text.startswith("⛔ MBP 到達不能（最終疎通 ")
    assert text.endswith("どのファイルですか？")
    with mgr._sessions_lock:
        history = mgr._load_or_create_session("c1")
    assert [t["text"] for t in history if t["role"] == "assistant"] == ["どのファイルですか？"]


def test_reachable_has_no_prefix():
    mgr = _manager(_ReachablePreflight(),
                   {"reply": "承知しました", "ready": False})
    mgr.handle_message(_msg("ちょっと相談"))
    assert mgr.slack.notes[-1] == "承知しました"


def test_no_preflight_means_no_gate():
    mgr = _manager(None, {"reply": "承知しました", "ready": False})
    mgr.handle_message(_msg("ちょっと相談"))
    assert mgr.slack.notes[-1] == "承知しました"


def test_notice_label_when_never_reached_since_boot():
    class _NeverOk(_UnreachablePreflight):
        last_ssh_ok = None
    mgr = _manager(_NeverOk(), {"reply": "x", "ready": False})
    assert mgr._reachability_notice() == "⛔ MBP 到達不能（最終疎通 起動後の疎通なし）"


def test_notice_unknown_error_does_not_block():
    class _Broken:
        def check_ssh(self):
            raise ValueError("bug")
    mgr = _manager(_Broken(), {"reply": "x", "ready": False})
    assert mgr._reachability_notice() is None


def test_unreachable_provenance_after_gate_passed_uses_fixed_text():
    """ゲート合格後（pass_ttl 内）に CLI 不達へ転じた場合も、抽出失敗の定型で誤誘導しない。"""
    class _UnreachableContractor:
        def contract(self, *a, **k):
            return None, {"origin": None, "backend": "worker_cli", "unreachable": True,
                          "attempts": []}
    mgr = _manager(_ReachablePreflight(),
                   {"reply": "", "ready": True, "summary": "README 更新と push"})
    mgr._contract_enabled = True
    mgr.contractor = _UnreachableContractor()
    mgr.handle_message(_msg())
    assert mgr.slack.confirms == []
    assert mgr.slack.notes[-1] == UNREACHABLE_STOP_TEXT
