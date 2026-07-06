"""WorkerPtyWrapper.approve/deny のキー送信契約テスト。

検証する振る舞い（存在ではなく挙動）:
- レガシー y/n テキストプロンプト（YN 等）には "y"/"n" を送る。
- Ink TUI メニュー（MENU/TRUST_DIALOG）には Enter（承認）/ Esc（拒否）を送る
  （実機検証: Claude Code は矢印キー選択式で、"y"/"n" の文字入力を受け付けない。）。

orchestrator パッケージ本体は pexpect 依存を引くため、pty_wrapper.py を軽量スタブ pexpect
下でファイル直ロードする（test_process_manager.py と同方式）。ロード後は sys.modules の
pexpect を元に戻す（本物の pexpect がインストール済みの環境で、他テストファイルの import
まで巻き込んでスタブ化してしまわないため）。
"""

import importlib.util
import os
import sys
import types

_HERE = os.path.dirname(__file__)
_PTW_PATH = os.path.join(_HERE, "..", "pty_wrapper.py")


_spawn_calls = []


def _load_ptw():
    original = sys.modules.get("pexpect")
    stub = types.ModuleType("pexpect")

    def _fake_spawn(cmd, **kw):
        _spawn_calls.append(cmd)
        return None
    stub.spawn = _fake_spawn
    sys.modules["pexpect"] = stub
    try:
        spec = importlib.util.spec_from_file_location("orchestrator.pty_wrapper", _PTW_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if original is not None:
            sys.modules["pexpect"] = original
        else:
            del sys.modules["pexpect"]


ptw = _load_ptw()


class _FakeChild:
    """pexpect.spawn の代わりに送信キーだけを記録するスタブ。"""

    def __init__(self):
        self.sent = []

    def sendline(self, s):
        self.sent.append(("sendline", s))

    def send(self, s):
        self.sent.append(("send", s))


def _wrapper():
    w = ptw.WorkerPtyWrapper("test-instance", command="claude")
    w.child = _FakeChild()
    return w


def test_approve_legacy_yn_sends_y():
    w = _wrapper()
    w.approve("yn")
    assert w.child.sent == [("sendline", "y")]


def test_deny_legacy_yn_sends_n():
    w = _wrapper()
    w.deny("yn")
    assert w.child.sent == [("sendline", "n")]


def test_approve_menu_sends_enter():
    """Ink TUI メニュー（ツール実行承認）は Enter（"\\r" 直接送信）でハイライト済み Yes を確定する。

    sendline("") の既定改行 "\\n" では Ink の生端末モードが Enter と認識しない（実機検証）。
    """
    w = _wrapper()
    w.approve("menu")
    assert w.child.sent == [("send", "\r")]


def test_approve_trust_dialog_sends_enter():
    """workspace 信頼ダイアログも同じ Enter 確定（デフォルトで 1. Yes がハイライトされる）。"""
    w = _wrapper()
    w.approve("trust_dialog")
    assert w.child.sent == [("send", "\r")]


def test_deny_menu_sends_escape():
    """Ink TUI メニューの拒否は Esc（選択肢の文言・番号位置に依存しない）。"""
    w = _wrapper()
    w.deny("menu")
    assert w.child.sent == [("send", "\x1b")]


def test_approve_accepts_prompttype_enum_via_value_attr():
    """PromptType enum を直接渡しても（.value 経由で）同じ判定になる（呼び出し側の実引数）。"""
    class _FakeEnum:
        value = "menu"

    w = _wrapper()
    w.approve(_FakeEnum)
    assert w.child.sent == [("send", "\r")]


def test_approve_default_none_treated_as_legacy():
    """prompt_type 省略（None）はレガシー y/n 扱い（後方互換の既定値）。"""
    w = _wrapper()
    w.approve()
    assert w.child.sent == [("sendline", "y")]


def test_send_task_sends_text_then_carriage_return():
    """タスク文字列を送った後、確定は "\\r" 直接送信（sendline の既定 "\\n" では
    Ink の生端末モード入力欄に文字列が残ったまま送信されない、実機検証で確認）。"""
    w = _wrapper()
    w.send_task("READMEを直して")
    assert w.child.sent == [("send", "READMEを直して"), ("send", "\r")]


def test_start_allocates_pseudo_tty():
    """ssh コマンドに -tt を付け疑似端末を強制割当する。

    実機検証: -tt 無しだと tmux new-session が「open terminal failed: not a terminal」で
    失敗し、worker CLI が一度も起動しないまま stderr がそのまま出力扱いされる。
    """
    _spawn_calls.clear()
    w = ptw.WorkerPtyWrapper("test-instance", command="claude", cwd="/tmp/work")
    w.start()
    assert len(_spawn_calls) == 1
    assert _spawn_calls[0].startswith("ssh -tt ")


def test_start_sets_remote_term():
    """launchd 常駐（TERM 未設定）から起動するため、リモート側に TERM を明示する。

    実機検証: TERM 未設定だと tmux が terminfo を引けず「terminal does not support clear」
    で失敗する。
    """
    _spawn_calls.clear()
    w = ptw.WorkerPtyWrapper("test-instance", command="claude", cwd="/tmp/work")
    w.start()
    assert "export TERM=xterm-256color" in _spawn_calls[0]


def test_reconnect_allocates_pseudo_tty():
    _spawn_calls.clear()
    w = ptw.WorkerPtyWrapper("test-instance", command="claude")
    w.reconnect()
    assert len(_spawn_calls) == 1
    assert _spawn_calls[0].startswith("ssh -tt ")
