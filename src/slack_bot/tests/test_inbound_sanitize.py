"""§8.3 上り発話の正規化の振る舞いテスト。

grep では潰せない振る舞い（app_id で対象を切り分けるか、末尾完全一致でのみ落とすか、
未知の表記でドリフトを鳴らすか、原文が同じ文字列で終わっても人手発話は守られるか）を
分離実行で担保する。実測した汚染文字列（2026-07-29）をそのまま検体に使う。
"""

import logging
import re
import textwrap

import pytest

from services.inbound_sanitize import load_sanitize_apps, strip_app_attribution

APP_ID = "A0BGPDZMFR9"
SUFFIX = " *使用して送信されました* taka-ma-mcp"
APPS = [{"app_id": APP_ID, "attribution_suffixes": [SUFFIX]}]


def test_app_post_loses_trailing_attribution():
    """実測した汚染そのもの。会話キューへは原文だけが渡らなければならない。"""
    assert strip_app_attribution("接続確認。実行不要。" + SUFFIX, APP_ID, APPS) == "接続確認。実行不要。"


def test_multiline_strips_from_overall_tail():
    """Slack は改行を挟まず最終行へ連結する（実測）。行単位ではなく全体末尾で判定する。"""
    assert strip_app_attribution("1行目\n2行目の末尾。" + SUFFIX, APP_ID, APPS) == "1行目\n2行目の末尾。"


def test_human_post_is_untouched_even_with_same_tail():
    """誤除去の構造的排除。app_id が無い＝人が打った発話なので触ってはならない。"""
    text = "この件は" + SUFFIX
    assert strip_app_attribution(text, None, APPS) == text
    assert strip_app_attribution(text, "", APPS) == text


def test_unregistered_app_is_untouched():
    """他社アプリ（Zapier 等）の投稿へ干渉しない。"""
    text = "他アプリの発話" + SUFFIX
    assert strip_app_attribution(text, "A_OTHER_APP", APPS) == text


def test_match_is_tail_only_not_substring():
    """末尾完全一致であること。中間一致で削ると原文が壊れる。"""
    text = SUFFIX + " のあとに続きがある"
    assert strip_app_attribution(text, APP_ID, APPS) == text


def test_simple_correction_notation_becomes_parsable_again():
    """本欠陥の実害はここ。plan.py の _SIMPLE_RE は行全体をアンカーするため、
    汚染が残ると必ず不一致になり決定的経路が失われる（§10.2.1）。"""
    simple_re = re.compile(
        r"\A(?P<targets>all|全部|\d+(?:\s*,\s*\d+)*)\s+(?P<value>\S+)\s*\Z", re.IGNORECASE)
    polluted = "2 opus" + SUFFIX
    assert simple_re.match(polluted) is None, "汚染されたままだと不一致になる（前提の確認）"
    assert simple_re.match(strip_app_attribution(polluted, APP_ID, APPS)) is not None


def test_no_trailing_whitespace_after_strip():
    """rstrip して原文の見た目を保つ。"""
    assert strip_app_attribution("依頼です。 " + SUFFIX, APP_ID, APPS) == "依頼です。"


def test_silent_on_normal_path(caplog):
    """毎発話ぶんのログは既存の受信ログと重複する純粋なノイズになるため出さない。"""
    with caplog.at_level(logging.DEBUG, logger="u-zu.sanitize"):
        strip_app_attribution("依頼です。" + SUFFIX, APP_ID, APPS)
    assert caplog.records == []


def test_warns_when_target_app_has_unknown_attribution(caplog):
    """Slack の文言変更・アプリ改名の検知。放置すると原文汚染が静かに復活する。"""
    text = "依頼です。 *Sent using* taka-ma-mcp"
    with caplog.at_level(logging.WARNING, logger="u-zu.sanitize"):
        got = strip_app_attribution(text, APP_ID, APPS)
    assert got == text, "落とせないなら本文は変えない（推測で削らない）"
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_empty_apps_disables_sanitize():
    """`apps: []` を明示的な無効化手段とする。"""
    text = "依頼です。" + SUFFIX
    assert strip_app_attribution(text, APP_ID, []) == text


def test_config_is_loaded_from_yaml_and_registers_relay_app():
    """コード側に既定値を置かず yaml を唯一の源とする（#103）。"""
    apps = load_sanitize_apps()
    assert isinstance(apps, list)
    assert any(a["app_id"] == APP_ID for a in apps), "リレー用アプリが未登録"
    for a in apps:
        assert a["attribution_suffixes"], "suffix が空だと除去できずドリフト警告が出続ける"


def test_missing_config_key_fails_at_startup(tmp_path):
    """正規化なしのまま常駐する偽正常を許さない（#103 の方針）。"""
    p = tmp_path / "u-zu.yaml"
    p.write_text(textwrap.dedent("""
        watchdog:
          check_interval_sec: 60
    """), encoding="utf-8")
    with pytest.raises(KeyError):
        load_sanitize_apps(str(p))


def test_yaml_value_matches_observed_pollution():
    """設定値そのものが検体と一致することを確かめる（写経ミスで無言に効かなくなるため）。"""
    entry = next(a for a in load_sanitize_apps() if a["app_id"] == APP_ID)
    assert SUFFIX in entry["attribution_suffixes"]
    # yaml 経由でも前方の半角空白が保たれていること（strip されると末尾一致に失敗する）
    assert entry["attribution_suffixes"][0].startswith(" ")


def test_silent_when_app_post_has_no_attribution_at_all(caplog):
    """ユーザー本人のトークンで投稿した発話（G2 リレー）には帰属表記が付かない。

    app_id は付くので従来は毎発話が WARNING になり、実機のログが警告で埋まった。
    表記が最初から無いのは正常であり、鳴らしてはならない。
    """
    text = "/tmp。に。hello8.txt。を作ってくれ。"
    with caplog.at_level(logging.DEBUG, logger="u-zu.sanitize"):
        got = strip_app_attribution(text, APP_ID, APPS)
    assert got == text
    assert caplog.records == [], "帰属表記が無い発話で警告を出さない"


def test_warns_only_when_attribution_shaped_tail_is_unknown(caplog):
    """文言が変わっても『*…* アプリ名』の形は保たれる。その形のときだけ検知する。"""
    text = "依頼です。 *Posted via* taka-ma-mcp"
    with caplog.at_level(logging.WARNING, logger="u-zu.sanitize"):
        strip_app_attribution(text, APP_ID, APPS)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_bold_in_body_alone_does_not_warn(caplog):
    """本文中の強調は帰属表記ではない（末尾がアプリ名の形になっていない）。"""
    text = "*重要* な件を進めてくれ。"
    with caplog.at_level(logging.DEBUG, logger="u-zu.sanitize"):
        strip_app_attribution(text, APP_ID, APPS)
    assert caplog.records == []
