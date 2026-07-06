"""ボタン押下ハンドラの thread_ts 解決テスト。

検証する振る舞い（存在ではなく挙動）:
- Bolt の say はボタン押下ハンドラでも thread_ts を自動継承しない。ボタンを含む
  メッセージがスレッド内なら thread_ts、それ自身がスレッド起点なら ts を使って
  押下確認メッセージ（「着手を承認しました」等）を同じスレッドへ返す
  （未対応だと常に通常投稿になる欠陥を実機検証で確認・是正）。
"""

import handlers.actions as actions


def test_thread_ts_prefers_message_thread_ts():
    """ボタンを含むメッセージが既にスレッド内なら、その thread_ts を使う。"""
    body = {"message": {"ts": "222.333", "thread_ts": "111.222"}}
    assert actions._thread_ts(body) == "111.222"


def test_thread_ts_falls_back_to_message_ts():
    """ボタンを含むメッセージがスレッド起点（thread_ts 無し）なら、そのメッセージ自身の ts を使う。"""
    body = {"message": {"ts": "111.222"}}
    assert actions._thread_ts(body) == "111.222"


def test_thread_ts_handles_missing_message():
    """message キー自体が無くても例外を出さず None を返す。"""
    assert actions._thread_ts({}) is None


class _FakeApp:
    def __init__(self):
        self.handlers = {}

    def action(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


def test_exec_confirm_replies_in_same_thread(monkeypatch):
    """着手ボタン押下の確認メッセージが、ボタンを含むメッセージと同じスレッドへ返る。"""
    monkeypatch.setattr(actions, "authorize", lambda user, role, say: True)
    monkeypatch.setattr(actions, "resolve_exec_confirm", lambda req_id, status, decided_by: True)

    app = _FakeApp()
    actions.register_actions(app)

    said = []
    body = {
        "actions": [{"value": "req-1"}],
        "user": {"id": "U1"},
        "message": {"ts": "555.666", "thread_ts": "111.222"},
    }
    app.handlers["exec_confirm"](ack=lambda: None, body=body, say=lambda text, **kw: said.append((text, kw)))

    assert len(said) == 1
    text, kwargs = said[0]
    assert "着手を承認しました" in text
    assert kwargs["thread_ts"] == "111.222"
