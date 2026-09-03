"""Task #161: スレッド会話の明示エスケープ `go` 記法のテスト（§8.3 (B)）。

slash command `/taka-ma-go` は Slack 仕様で thread_ts を運ばず、スレッド会話を強制前進
できない（2026-08-30 実測: スレッドの会話脳誤出力からの回復手段が無かった）。メンション
本文が空白除去後に逐語で `go` / `/taka-ma-go` のとき force_ready=true で当該スレッドへ
投入する — 決定的な逐語一致＝記法であり、語列挙の言語理解ではない。
"""

import handlers.events as events
from handlers.events import _is_go_mention


# ── 逐語判定（決定的一致・記法） ──

def test_go_literals_accepted():
    assert _is_go_mention("<@U0BOT> go")
    assert _is_go_mention("<@U0BOT> /taka-ma-go")
    assert _is_go_mention("<@U0BOT>   go  ")      # 空白は除去して照合
    assert _is_go_mention("go")                    # メンショントークン無し（本文のみ）でも成立


def test_non_literal_utterances_rejected():
    """記法は逐語一致のみ。言い回し（「go しろ」等）は通常の会話として脳に委ねる。"""
    assert not _is_go_mention("<@U0BOT> go しろ")
    assert not _is_go_mention("<@U0BOT> going")
    assert not _is_go_mention("<@U0BOT> please go")
    assert not _is_go_mention("<@U0BOT> 残りを直して")
    assert not _is_go_mention("")


# ── メンションハンドラへの配線（force_ready がスレッド付きで投入される） ──

class _FakeApp:
    def __init__(self):
        self.handlers = {}

    def event(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class _FakeClient:
    def reactions_add(self, **kw):
        pass


def _register(monkeypatch, captured):
    monkeypatch.setattr(events, "authorize", lambda user, role, say: True)

    def fake_enqueue(source, text, *, user_id, team_id, channel_id,
                     thread_ts=None, force_ready=False):
        captured.update(thread_ts=thread_ts, force_ready=force_ready, text=text)
        return "msg-id"

    monkeypatch.setattr(events, "enqueue_conversation_message", fake_enqueue)
    app = _FakeApp()
    events.register_events(app)
    return app


def test_go_mention_in_thread_enqueues_force_ready(monkeypatch):
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["app_mention"](
        event={"user": "U1", "text": "<@U0BOT> go", "team": "T1", "channel": "C1",
               "ts": "222.333", "thread_ts": "111.222"},
        body={"event_id": "ev-go-1"},
        say=lambda *a, **kw: None,
        client=_FakeClient())
    assert captured["force_ready"] is True
    assert captured["thread_ts"] == "111.222"   # 当該スレッド会話へ投入される


def test_normal_mention_not_force_ready(monkeypatch):
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["app_mention"](
        event={"user": "U1", "text": "<@U0BOT> README を直して", "team": "T1",
               "channel": "C1", "ts": "222.333", "thread_ts": "111.222"},
        body={"event_id": "ev-go-2"},
        say=lambda *a, **kw: None,
        client=_FakeClient())
    assert captured["force_ready"] is False
