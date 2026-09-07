"""意図判定基準の静的テスト（判定基準は intent.md が正本）。

判定基準は converse.md から intent.md（IntentClassifier・§8.4）へ移った。converse.md は
返信生成専用になり、判定の基準文言は intent.md 側で回帰固定する。converse.md 側は
「宣言をしない」「細部を尋ねない」の返信規律を固定する。
"""

import os

_P = os.path.join(os.path.dirname(__file__), "..", "..", "ai_gateway", "prompts", "intent.md")
_C = os.path.join(os.path.dirname(__file__), "..", "prompts", "converse.md")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_single_utterance_clear_request_fires():
    """対象＋動作が特定できる依頼は 1 発話で execute に倒す基準がある。"""
    text = _read(_P)
    assert "**対象**" in text and "**動作**" in text
    assert "1 発話だけでも成立" in text
    assert "完了条件が未明示でも" in text


def test_incident_examples_present():
    """インシデント同型の具体例（README 要約・9/7 の probe 誤爆事例）が例示されている。"""
    text = _read(_P)
    assert "README を要約して" in text
    assert "問題がないこと確認して欲しい" in text     # 2026-09-07 実発話 → execute


def test_details_are_not_ambiguity():
    """実装の細部の未指定は「曖昧」の理由にならない、が明記されている。"""
    text = _read(_P)
    assert "細部" in text and "曖昧」の理由になりません" in text


def test_smalltalk_is_chat():
    """雑談・知識質問は chat（会話継続）に落ちる基準がある。"""
    text = _read(_P)
    assert "雑談" in text and "知識" in text


def test_converse_prompt_forbids_declarations_and_detail_questions():
    """converse.md（返信生成専用）が着手宣言と細部質問を禁じている。"""
    text = _read(_C)
    assert "宣言をしない" in text
    assert "細部" in text and "尋ねてはいけません" in text
    assert "返信文の生成だけ" in text


def test_intent_output_contract():
    """intent.md の出力契約（action 4 値・confidence・evidence 逐語）が明記されている。"""
    text = _read(_P)
    for token in ('"action"', '"confidence"', '"evidence"', "一字一句そのまま"):
        assert token in text
