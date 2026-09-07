"""意図判定 IntentClassifier（設計書 §8.4「意図判定の呼び出し」・#taka-ma/166.1）の振る舞いテスト。

検証する振る舞い:
- 一次（ローカル）が合格すれば昇格せず、その判定を採用する（origin=一次モデル）。
- スキーマ不合格は同段 1 回再試行し、再不合格で二次へ昇格する。
- evidence の逐語照合不合格・低 confidence は再試行せず即昇格する。
- 二段目の前に preflight.check_ssh を通し、不達なら昇格せず fail-closed（action=chat）。
- escalate_runner 未注入・全段不合格も fail-closed（action=chat）。例外は外に漏らさない。
- 一次と二次の判定が食い違ったら二次を採用し、判定ログに mismatch として両方記録する。
- プロンプトが 2026-09-07 の実発話（実行依頼→probe誤爆した事例）を execute の例として持つ。
"""

import json
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway import intent_classifier as ic_mod  # noqa: E402
from ai_gateway.intent_classifier import IntentClassifier  # noqa: E402

UTTER = ("要件定義書と詳細設計に問題がないこと確認して欲しい。"
         "repo: ~/DevDev/projects/x 配下のファイルを見なさい。"
         "そして、実装する機能一覧を箇条書きで教えて")


def _config():
    return {"ya-ta": {"model": "local-dummy", "llm_timeout_sec": 60,
                      "contractor": {"model": "opus"}},
            "sa-ru": {"ollama_host": "http://localhost:11434"},
            "routing": {"confidence_threshold": 0.8}}


class _Log:
    def __init__(self):
        self.entries = []

    def log_intent(self, **kw):
        self.entries.append(kw)


def _clf(monkeypatch, local_outputs, runner=None, preflight=None):
    """run_ollama を応答列で差し替えた IntentClassifier を作る。"""
    outs = list(local_outputs)

    def _fake_ollama(*a, **k):
        v = outs.pop(0)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(ic_mod, "run_ollama", _fake_ollama)
    c = IntentClassifier(_config(), escalate_runner=runner, preflight=preflight)
    c.logger = _Log()
    return c


def _ok(action="execute", conf=0.95, evidence="機能一覧を箇条書きで教えて"):
    return json.dumps({"action": action, "confidence": conf, "evidence": evidence},
                      ensure_ascii=False)


# ── 一次合格 ──

def test_local_pass_no_escalation(monkeypatch):
    calls = []
    c = _clf(monkeypatch, [_ok()], runner=lambda m, p: calls.append(m) or _ok())
    r = c.classify("履歴", UTTER)
    assert r["action"] == "execute" and r["origin"] == "local-dummy"
    assert r["escalated"] is False and r["fail_closed"] is False
    assert calls == []                                    # 二次は呼ばれない
    assert c.logger.entries[-1]["escalated"] is False


# ── 昇格条件 1: スキーマ不合格 → 同段 1 回再試行 → 昇格 ──

def test_schema_failure_retries_once_then_escalates(monkeypatch):
    runner_calls = []
    c = _clf(monkeypatch, ["not json at all", '{"action": "fly", "confidence": 2}'],
             runner=lambda m, p: runner_calls.append(m) or _ok(action="execute"))
    r = c.classify("履歴", UTTER)
    assert r["action"] == "execute" and r["escalated"] is True and r["origin"] == "opus"
    assert runner_calls == ["opus"]


# ── 昇格条件 2: evidence 逐語照合不合格 → 即昇格（再試行しない） ──

def test_fabricated_evidence_escalates_immediately(monkeypatch):
    c = _clf(monkeypatch, [_ok(evidence="この文言は発話に存在しない")],
             runner=lambda m, p: _ok(action="execute"))
    r = c.classify("履歴", UTTER)
    assert r["escalated"] is True and r["action"] == "execute"


# ── 昇格条件 3: 低 confidence → 即昇格。食い違いは二次採用 + mismatch 記録 ──

def test_low_confidence_escalates_and_mismatch_is_logged(monkeypatch):
    c = _clf(monkeypatch, [_ok(action="probe_repo", conf=0.5)],
             runner=lambda m, p: _ok(action="execute"))
    r = c.classify("履歴", UTTER)
    assert r["action"] == "execute" and r["escalated"] is True
    last = c.logger.entries[-1]
    assert last["mismatch"] is True and last["first_action"] == "probe_repo"


# ── 昇格条件 4: 到達不能 → 昇格せず fail-closed(chat) ──

class _Unreachable:
    def check_ssh(self):
        raise RuntimeError("SSH 応答なし")


class _Reachable:
    def check_ssh(self):
        return None


def test_unreachable_cli_fails_closed_to_chat(monkeypatch):
    called = []
    c = _clf(monkeypatch, [_ok(conf=0.3)],
             runner=lambda m, p: called.append(m) or _ok(),
             preflight=_Unreachable())
    r = c.classify("履歴", UTTER)
    assert r["action"] == "chat" and r["fail_closed"] is True
    assert called == []                                   # opus は呼ばれない


def test_reachable_cli_is_used(monkeypatch):
    c = _clf(monkeypatch, [_ok(conf=0.3)], runner=lambda m, p: _ok(),
             preflight=_Reachable())
    assert c.classify("履歴", UTTER)["action"] == "execute"


# ── fail-closed: 昇格手段なし・全段不合格 ──

def test_no_runner_fails_closed(monkeypatch):
    r = _clf(monkeypatch, [_ok(conf=0.3)]).classify("履歴", UTTER)
    assert r["action"] == "chat" and r["fail_closed"] is True


def test_all_stages_fail_returns_chat_without_raising(monkeypatch):
    def _broken_runner(m, p):
        raise RuntimeError("CLI エラー")
    c = _clf(monkeypatch, ["garbage", "garbage"], runner=_broken_runner)
    r = c.classify("履歴", UTTER)
    assert r["action"] == "chat" and r["fail_closed"] is True


def test_local_execution_failure_escalates(monkeypatch):
    """一次の呼び出し自体の失敗（ollama 不達等）は再試行せず昇格する。"""
    c = _clf(monkeypatch, [ic_mod.OllamaConnectionError("connect refused")],
             runner=lambda m, p: _ok())
    r = c.classify("履歴", UTTER)
    assert r["action"] == "execute" and r["escalated"] is True


# ── 二次の不合格も機械検証される ──

def test_second_stage_fabricated_evidence_fails_closed(monkeypatch):
    c = _clf(monkeypatch, [_ok(conf=0.3)],
             runner=lambda m, p: _ok(evidence="でっちあげ"))
    r = c.classify("履歴", UTTER)
    assert r["action"] == "chat" and r["fail_closed"] is True


# ── プロンプトの回帰固定 ──

def test_prompt_contains_0907_incident_as_execute_example():
    text = (ic_mod.PROMPTS_DIR / "intent.md").read_text()
    assert "問題がないこと確認して欲しい" in text and "execute" in text
    assert "機能一覧を箇条書きで教えて" in text
