"""commands ハンドラの内部ヘルパ単体テスト（Task #89 M8 / M9）。

Bolt App への登録を経ず、モデル管理ヘルパを直接呼んで振る舞いを検証する:
- M8: type:local の install で model_id 未設定なら偽成功せずエラーを返し、pull・再起動をしない。
- M9: 壊れた ya-ta.yaml で get_model→load_models が ValueError を投げても、
      install/uninstall ハンドラが握り潰さず :x: メッセージで応答する（無応答にしない）。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
"""

import pytest

from handlers import commands


@pytest.fixture
def say():
    """say の呼び出し文字列を集めるスタブ。"""
    msgs = []
    def _say(text=None, **kw):
        msgs.append(text)
    _say.msgs = msgs
    return _say


def _yata(tmp_path, monkeypatch, text):
    path = tmp_path / "ya-ta.yaml"
    path.write_text(text)
    monkeypatch.setenv("TAKA_MA_YATA_PATH", str(path))
    return path


def test_local_install_without_model_id_errors_no_restart(tmp_path, monkeypatch, say):
    # M8: type:local だが model_id 未設定 → 偽成功させずエラー、再起動もしない
    _yata(tmp_path, monkeypatch,
          "models:\n  localx:\n    type: local\n    full_name: \"x\"\n")
    called = {"restart": False, "pull": False}
    monkeypatch.setattr(commands, "_restart_core_services",
                        lambda: called.__setitem__("restart", True) or [])
    monkeypatch.setattr(commands.model_ops, "pull_model",
                        lambda mid: called.__setitem__("pull", True))

    commands._model_install_or_uninstall("install", ["localx"], say)

    assert any("model_id 未設定" in m for m in say.msgs)
    assert called["restart"] is False
    assert called["pull"] is False


def test_install_broken_yaml_responds_not_silent(tmp_path, monkeypatch, say):
    # M9: get_model が壊れた yaml で ValueError を投げても :x: 応答（無応答にしない）
    _yata(tmp_path, monkeypatch, "models:\n  a: [unclosed\n")
    commands._model_install_or_uninstall("install", ["a"], say)
    assert len(say.msgs) == 1
    assert say.msgs[0].startswith(":x:")


def test_model_list_broken_yaml_responds_not_silent(tmp_path, monkeypatch, say):
    # M9: 一覧経路も同様に握り潰さず応答する
    _yata(tmp_path, monkeypatch, "models:\n  a: [unclosed\n")
    commands._model_list(say)
    assert len(say.msgs) == 1
    assert say.msgs[0].startswith(":x:")
