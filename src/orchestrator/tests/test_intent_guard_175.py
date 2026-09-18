"""意図判定の構造マーカーガード（設計書 §8.4「構造マーカーの決定的ガード」・#175）。

grep では潰せない振る舞いを分離実行で担保する:
  - マーカー 2 個以上の発話は intent=chat でも execute へ倒れる（片方向）
  - マーカー 0〜1 個は LLM 判定のまま（雑談を誤昇格させない）
  - execute 判定・probe 判定は上書きしない（chat→execute のみ）
  - 2026-09-16 17:22 の実依頼文（マーカー 6 個）が確実に発動する
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
from orchestrator import contract as contract_rules

# 2026-09-16 17:22 に chat（confidence 0.98）へ誤判定され捨てられた実依頼の骨格
_REAL_INCIDENT_TEXT = """前提資料場所:
- repo: ~/DevDev/projects/obsidian-auto-stock-trader
- docs: ~/DevDev/projects/obsidian-auto-stock-trader/docs

【依頼名】自動売買システム: 設計文書の整合性是正（第 3 ラウンド・致命/高のみ)

依頼内容:
repo main の docs/consistency/97-consistency-review-round3.md にある指摘 13 件のうち
F1〜F5 の 5 件だけを是正してほしい。

対象ファイル:
- docs/01-technical-constraints-and-requirements.md

成果物:
- 上記 2 ファイルの是正（変更箇所に指摘番号 F1〜F5 を明記）
"""


def test_marker_count_on_real_incident_text():
    assert contract_rules.request_structure_count(_REAL_INCIDENT_TEXT) >= 5


def test_marker_count_casual_chat_is_low():
    assert contract_rules.request_structure_count("昨日の成果物、いい感じだったね") == 1
    assert contract_rules.request_structure_count("了解、ありがとう") == 0
    assert contract_rules.request_structure_count("") == 0
    assert contract_rules.request_structure_count(None) == 0


def test_marker_count_counts_distinct_markers_once():
    # 同じマーカーの繰り返しは 1 と数える（異なり数）
    text = "成果物は成果物として成果物を出す"
    assert contract_rules.request_structure_count(text) == 1


def _run_intent_stage(text: str, llm_action: str) -> str:
    """handle_message の意図判定〜ガード部分と同じ規則を適用した結果の action を返す。

    ガードは conversation.py の handle_message 内にあるため、同一規則
    （chat かつ マーカー >= 2 → execute）をここで検証する。conversation 側の
    配線は test_guard_wiring_in_handle_message が固定する。
    """
    action = llm_action
    if action == "chat" and contract_rules.request_structure_count(text) >= 2:
        action = "execute"
    return action


def test_chat_with_structure_flips_to_execute():
    assert _run_intent_stage(_REAL_INCIDENT_TEXT, "chat") == "execute"


def test_chat_without_structure_stays_chat():
    assert _run_intent_stage("今日は調子どう？", "chat") == "chat"
    assert _run_intent_stage("昨日の成果物、良かったよ", "chat") == "chat"


def test_one_way_only_never_downgrades():
    # execute / probe 判定は構造の有無にかかわらず上書きしない
    assert _run_intent_stage("ありがとう", "execute") == "execute"
    assert _run_intent_stage(_REAL_INCIDENT_TEXT, "execute") == "execute"
    assert _run_intent_stage(_REAL_INCIDENT_TEXT, "probe_task") == "probe_task"


def test_guard_wiring_in_handle_message():
    """conversation.handle_message にガードが実配線されている（規則の二重定義ずれ検出）。"""
    import inspect
    from orchestrator.conversation import ConversationManager
    src = inspect.getsource(ConversationManager.handle_message)
    assert "request_structure_count" in src
    assert 'action == "chat"' in src
    assert "marker_count >= 2" in src
