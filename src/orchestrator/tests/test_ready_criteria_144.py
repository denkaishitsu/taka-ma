"""converse.md の ready 判定基準の静的テスト（#taka-ma/144）。

変更の主体はプロンプト（converse.md）のため、判定基準の文言が存在することを
静的に固定する。実効性（脳 LLM が実際に ready=true を返すか）は分離実行の
A/B 実測（2026-08-16・qwen3.6:35b-a3b・think=false・各入力2回）で担保済みで、
本テストは「基準文言の消失・弱体化」という退行を検出する回帰ガード。

固定する基準（設計書 §8.3「ready 判定の方針」）:
- (a) 対象と動作が特定できる明確な依頼は 1 発話で ready=true（判定 3）
- (b) 宣言と判定の一致: 着手宣言の reply は ready=true のときだけ
- (c) 雑談・質問・相談は従来どおり ready=false（判定 1 の維持）
- #145 の出力契約（ready 必須 boolean）が弱体化されていないこと
"""

import os
import re

PROMPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "prompts", "converse.md")


def _read():
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def test_single_utterance_clear_request_fires():
    """(a) 対象＋動作が特定できる依頼は 1 発話で ready=true に倒す基準がある。"""
    text = _read()
    assert "**対象**" in text and "**動作**" in text
    assert "1 発話だけでも成立" in text
    assert "確認質問を挟まずに ready=true" in text
    # 完了条件の欠如を「曖昧」の口実にしない（インシデント同型の取りこぼし防止）
    assert "完了条件が明示されていなくても" in text


def test_incident_example_present():
    """(a) インシデント同型の具体例（README 要約）が例示されている。"""
    text = _read()
    assert "README を要約して" in text


def test_declaration_matches_ready():
    """(b) 着手宣言を書けるのは ready=true のときだけ、と明記されている。"""
    text = _read()
    assert "宣言と判定の一致" in text
    assert "ready=true のときだけ" in text
    assert "ready=false のまま\n着手を宣言してはいけません" in text.replace("\r\n", "\n")


def test_smalltalk_still_not_ready():
    """(c) 判定 1（雑談・質問は ready=false でそのまま答える）が残っている。"""
    text = _read()
    assert "そのまま答える（ready=false）" in text
    assert "開発の話に引き戻したり" in text
    # 過剰発火の抑制: 安易な ready=true を明示的に禁止
    assert "安易に ready=true にするのも誤り" in text


def test_145_output_contract_not_weakened():
    """#145 が入れた出力契約（ready 必須 boolean）が無傷で残っている。"""
    text = _read()
    assert "`ready` は**必須**です" in text
    assert "`true` または `false`" in text
    assert "契約違反" in text
    # 4 キー全出力の契約も維持
    assert re.search(r"`reply` / `ready` / `summary` / `probe` は\*\*毎回すべて\*\*", text)
