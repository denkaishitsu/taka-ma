"""app_mention / message ハンドラの thread_ts 解決・受付リアクションのテスト。

検証する振る舞い（存在ではなく挙動）:
- 既存スレッド内の発話は、その thread_ts をそのまま会話キューへ引き継ぐ。
- スレッド外のフラットな新規メンション/DM は、発話自身の ts をスレッド起点として使う
  （未対応だと sa-ru の返信が常に通常投稿になり、conversation_id のスレッド単位分離
  ＝設計書 §8.3 が機能しない。実機検証で確認・是正）。
- 認可後、会話キュー投入前に 👀 リアクションで受付を即時に示す（sa-ru の脳が要約/着手確認を
  返すまでの無反応区間が長く感じるという実運用フィードバックを受けて追加）。
"""

import handlers.events as events


class _FakeApp:
    """register_events が @app.event(...) で登録するハンドラだけを捕まえるスタブ。"""

    def __init__(self):
        self.handlers = {}

    def event(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class _FakeClient:
    """reactions_add の呼び出しだけを記録するスタブ。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def reactions_add(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("reactions:write scope 不足")


def _register(monkeypatch, captured):
    monkeypatch.setattr(events, "authorize", lambda user, role, say: True)

    def fake_enqueue(source, text, *, user_id, team_id, channel_id,
                      thread_ts=None, force_ready=False):
        captured["thread_ts"] = thread_ts
        captured["source"] = source
        return "msg-id"

    monkeypatch.setattr(events, "enqueue_conversation_message", fake_enqueue)
    app = _FakeApp()
    events.register_events(app)
    return app


def test_mention_flat_message_anchors_thread_on_own_ts(monkeypatch):
    """thread_ts 無しの新規メンションは、そのメッセージ自身の ts をスレッド起点にする。"""
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["app_mention"](
        event={"user": "U1", "text": "hi", "team": "T1", "channel": "C1", "ts": "111.222"},
        body={"event_id": "ev-mention-flat"},
        say=lambda *a, **kw: None,
        client=_FakeClient())
    assert captured["thread_ts"] == "111.222"


def test_mention_in_existing_thread_keeps_thread_ts(monkeypatch):
    """既にスレッド内の発話は、その thread_ts をそのまま使う（自身の ts に差し替えない）。"""
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["app_mention"](
        event={"user": "U1", "text": "hi", "team": "T1", "channel": "C1",
               "ts": "222.333", "thread_ts": "111.222"},
        body={"event_id": "ev-mention-thread"},
        say=lambda *a, **kw: None,
        client=_FakeClient())
    assert captured["thread_ts"] == "111.222"


def test_dm_flat_message_anchors_thread_on_own_ts(monkeypatch):
    """DM でも同様に、フラットな新規発話は自身の ts をスレッド起点にする。"""
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["message"](
        event={"user": "U1", "text": "hi", "team": "T1", "channel": "D1",
               "channel_type": "im", "ts": "444.555"},
        body={"event_id": "ev-dm-flat"},
        say=lambda *a, **kw: None,
        logger=__import__("logging").getLogger("test"),
        client=_FakeClient())
    assert captured["thread_ts"] == "444.555"


def test_mention_acks_with_eyes_reaction_on_own_message(monkeypatch):
    """認可後、会話キュー投入前に元メッセージへ 👀 リアクションを付ける。"""
    captured = {}
    app = _register(monkeypatch, captured)
    client = _FakeClient()
    app.handlers["app_mention"](
        event={"user": "U1", "text": "hi", "team": "T1", "channel": "C1", "ts": "111.222"},
        body={"event_id": "ev-mention-ack"},
        say=lambda *a, **kw: None,
        client=client)
    assert client.calls == [{"channel": "C1", "timestamp": "111.222", "name": "eyes"}]


def test_mention_reaction_failure_does_not_block_enqueue(monkeypatch):
    """リアクション付与が失敗（scope 不足等）しても、会話キュー投入は続行する。"""
    captured = {}
    app = _register(monkeypatch, captured)
    app.handlers["app_mention"](
        event={"user": "U1", "text": "hi", "team": "T1", "channel": "C1", "ts": "111.222"},
        body={"event_id": "ev-mention-failack"},
        say=lambda *a, **kw: None,
        client=_FakeClient(fail=True))
    assert captured["thread_ts"] == "111.222"


def test_unauthorized_mention_does_not_ack():
    """未認可ユーザーにはリアクションも付けない（会話キューへも流さない）。"""
    captured = {}

    def fake_enqueue(*a, **kw):
        captured["called"] = True

    class _Denied:
        pass

    import handlers.events as ev
    original_authorize = ev.authorize
    ev.authorize = lambda user, role, say: False
    ev.enqueue_conversation_message = fake_enqueue
    try:
        app = _FakeApp()
        ev.register_events(app)
        client = _FakeClient()
        app.handlers["app_mention"](
            event={"user": "U1", "text": "hi", "team": "T1", "channel": "C1", "ts": "111.222"},
            body={"event_id": "ev-mention-unauth"},
            say=lambda *a, **kw: None,
            client=client)
        assert client.calls == []
        assert "called" not in captured
    finally:
        ev.authorize = original_authorize


def _register_passive(monkeypatch, captured, authorized=True):
    """channel メッセージ（passive 経路・§8.3 (C)）検証用の登録ヘルパ。"""
    import services.event_dedup as dedup
    monkeypatch.setattr(dedup, "_seen", {})
    monkeypatch.setattr(events, "check_role", lambda user, role: authorized)

    def fake_enqueue(source, text, *, user_id, team_id, channel_id,
                     thread_ts=None, force_ready=False, passive=False):
        captured.update({"source": source, "thread_ts": thread_ts, "passive": passive})
        return "msg-id"

    monkeypatch.setattr(events, "enqueue_conversation_message", fake_enqueue)
    app = _FakeApp()
    events.register_events(app)
    return app


def _channel_event(**kw):
    base = {"user": "U1", "text": "補足です", "team": "T1", "channel": "C1",
            "channel_type": "channel", "ts": "222.333", "thread_ts": "111.222"}
    base.update(kw)
    return base


def _post_message(app, event, body=None, said=None):
    app.handlers["message"](
        event=event, body=body or {"event_id": "ev-passive-1"},
        say=(said.append if said is not None else (lambda *a, **kw: None)),
        logger=__import__("logging").getLogger("test"),
        client=_FakeClient())


def test_thread_reply_without_mention_enqueued_passive(monkeypatch):
    """既存スレッド内の非メンション返信は passive として会話キューへ入る（§8.3 (C)）。"""
    captured = {}
    app = _register_passive(monkeypatch, captured)
    _post_message(app, _channel_event())
    assert captured == {"source": "slack_thread_passive",
                        "thread_ts": "111.222", "passive": True}


def test_channel_message_outside_thread_not_enqueued(monkeypatch):
    """スレッド外のチャンネル発話は従来どおり収集しない。"""
    captured = {}
    app = _register_passive(monkeypatch, captured)
    ev = _channel_event()
    del ev["thread_ts"]
    _post_message(app, ev)
    assert captured == {}


def test_mention_text_in_thread_not_passively_enqueued(monkeypatch):
    """メンション付き発話は app_mention 側で能動処理されるため passive 投入しない（二重防止）。"""
    captured = {}
    app = _register_passive(monkeypatch, captured)
    _post_message(app, _channel_event(text="<@UBOT> お願いします"),
                  body={"event_id": "ev-passive-2",
                        "authorizations": [{"user_id": "UBOT"}]})
    assert captured == {}


def test_bot_own_thread_message_not_enqueued(monkeypatch):
    """bot 自身の投稿（bot_id 付き）は文脈記録しない。"""
    captured = {}
    app = _register_passive(monkeypatch, captured)
    _post_message(app, _channel_event(bot_id="B1"))
    assert captured == {}


def test_unauthorized_passive_dropped_silently(monkeypatch):
    """未認可ユーザーの passive 発話は拒否メッセージを返さず黙って捨てる（§8.3 (C)）。"""
    captured = {}
    said = []
    app = _register_passive(monkeypatch, captured, authorized=False)
    _post_message(app, _channel_event(), said=said)
    assert captured == {} and said == []


def test_resent_passive_not_enqueued_twice(monkeypatch):
    """同一 event_id の再送 passive 発話は二重投入されない（§8.3 再送の冪等化）。"""
    captured = {}
    calls = {"n": 0}
    app = _register_passive(monkeypatch, captured)
    original = events.enqueue_conversation_message

    def counting(*a, **kw):
        calls["n"] += 1
        return original(*a, **kw)

    monkeypatch.setattr(events, "enqueue_conversation_message", counting)
    for _ in range(2):
        _post_message(app, _channel_event(), body={"event_id": "resend-passive"})
    assert calls["n"] == 1


def test_resent_mention_not_enqueued_twice(monkeypatch):
    """Task #89 H13: 同一 event_id の再送メンションは会話キューへ二重投入されない。"""
    import services.event_dedup as dedup
    monkeypatch.setattr(dedup, "_seen", {})
    calls = {"n": 0}

    def counting_enqueue(*a, **kw):
        calls["n"] += 1
        return "msg-id"

    monkeypatch.setattr(events, "authorize", lambda user, role, say: True)
    monkeypatch.setattr(events, "enqueue_conversation_message", counting_enqueue)
    app = _FakeApp()
    events.register_events(app)
    ev = {"user": "U1", "text": "hi", "team": "T1", "channel": "C1", "ts": "111.222"}
    for _ in range(2):   # 同一 event_id を 2 回配信
        app.handlers["app_mention"](
            event=ev, body={"event_id": "resend-1"},
            say=lambda *a, **kw: None, client=_FakeClient())
    assert calls["n"] == 1
