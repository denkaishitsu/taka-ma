"""model_args 単体テスト — /taka-ma-model の引数パースと既定値補完。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
"""

import pytest

from services.model_args import parse_model_opts, build_model_conf


def test_parse_basic_api():
    fields = parse_model_opts(
        ["--full-name", "claude-opus-4.7", "--vendor", "anthropic",
         "--methods", "pty", "--model-flag", "--model opus-4.7"]
    )
    assert fields["full_name"] == "claude-opus-4.7"
    assert fields["vendor"] == "anthropic"
    assert fields["methods"] == ["pty"]            # 配列化
    assert fields["model_flag"] == "--model opus-4.7"  # 値に -- を含んでも値として扱う


def test_parse_methods_csv():
    fields = parse_model_opts(["--methods", "pty,subprocess"])
    assert fields["methods"] == ["pty", "subprocess"]


def test_parse_unknown_flag_raises():
    with pytest.raises(ValueError):
        parse_model_opts(["--bogus", "x"])


def test_parse_missing_value_raises():
    with pytest.raises(ValueError):
        parse_model_opts(["--vendor"])


def test_build_conf_api_infers_type_and_command():
    conf = build_model_conf({"full_name": "x", "vendor": "anthropic", "methods": ["pty"]})
    assert conf["type"] == "api"
    assert conf["command"] == "claude"           # vendor から推測


def test_build_conf_google_command():
    conf = build_model_conf({"full_name": "x", "vendor": "google"})
    assert conf["command"] == "agy"


def test_build_conf_unknown_vendor_uses_vendor_name():
    conf = build_model_conf({"full_name": "x", "vendor": "suno"})
    assert conf["command"] == "suno"


def test_build_conf_local_infers_from_model_id():
    conf = build_model_conf({"full_name": "x", "model_id": "gemma4:31b"})
    assert conf["type"] == "local"
    assert conf["command"] == "ollama"           # local は ollama 既定


def test_build_conf_command_override_wins():
    conf = build_model_conf({"full_name": "x", "vendor": "anthropic", "command": "claude-beta"})
    assert conf["command"] == "claude-beta"


def test_build_conf_type_override_wins():
    conf = build_model_conf({"full_name": "x", "model_id": "m", "type": "api", "vendor": "v"})
    assert conf["type"] == "api"


def test_build_conf_undeterminable_type_raises():
    with pytest.raises(ValueError):
        build_model_conf({"full_name": "x"})       # vendor も model_id も type も無い
