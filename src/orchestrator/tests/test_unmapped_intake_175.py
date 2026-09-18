"""unmapped の突き返し廃止 — 受理の不変条件（設計書 §8.10f 閉包規則・#175 R5）。

2026-09-16/17 実運用事故の再発防止を固定する: 実依頼（パス無し成果物「対応状況表」を
含む）が unmapped 申告 → 契約不成立 → 「解釈できませんでした。言い直すか取り下げて
ください」で 3 回連続不受理になった。是正後の不変条件:

- 写像できない指定は契約不成立の理由にならない（突き返しの出口が存在しない）
- 検証済み proposal_path は target_paths へ機械追記（既定 file 検査に乗る）
- 検証不合格・旧形式・quote 非逐語は既定質問へ決定的に縮退（縮退先も突き返しでない）
- 全要素は着手確認の行として提示される（_format_contract）
- 承認対象の保存先は分解入力へ定型行で機械追記される（_build_plan）

実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import contract as contract_rules  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_175u", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager

# 2026-09-16 17:22 の実依頼の骨格（パス無し成果物「対応状況表」を含む）
SOURCE = ("依頼内容: F1〜F5 の 5 件だけを是正してほしい。\n"
          "対象ファイル: docs/01-technical-constraints-and-requirements.md\n"
          "成果物: 上記ファイルの是正と、5 件の対応状況表")


def _raw(**overrides):
    base = {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "workspace": None, "branch": None, "target_paths": [],
            "needs_repo": False, "rest_summary": None, "unmapped": []}
    base.update(overrides)
    return base


# ── validate_contract: 解釈案の検証と target_paths への合流 ──

def test_valid_proposal_merges_into_target_paths():
    """検証済み proposal_path は target_paths へ機械追記され、契約は成立する。"""
    v, problems = contract_rules.validate_contract(
        _raw(unmapped=[{"quote": "対応状況表",
                        "proposal_path": "docs/consistency/98-f1-f5-status.md"}]),
        SOURCE)
    assert problems == []
    assert v["unmapped"] == [{"quote": "対応状況表",
                              "proposal_path": "docs/consistency/98-f1-f5-status.md"}]
    assert "docs/consistency/98-f1-f5-status.md" in v["target_paths"]


def test_unsafe_proposal_degrades_to_question():
    """不正な proposal_path（絶対パス・`..`）は質問へ縮退する — 突き返しに落ちない。"""
    for bad in ("/etc/passwd", "docs/../../secret.md", "docs/x;rm.md"):
        v, problems = contract_rules.validate_contract(
            _raw(unmapped=[{"quote": "対応状況表", "proposal_path": bad}]), SOURCE)
        assert problems == []
        assert v["unmapped"] == [{"quote": "対応状況表",
                                  "question": contract_rules.UNMAPPED_DEFAULT_QUESTION}]
        assert v["target_paths"] == []


def test_non_verbatim_quote_degrades_to_question():
    """quote が発話の逐語でない要素は提案を採らず質問行へ縮退（でっち上げ検出）。"""
    v, problems = contract_rules.validate_contract(
        _raw(unmapped=[{"quote": "発話に無い指定",
                        "proposal_path": "docs/x.md"}]), SOURCE)
    assert problems == []
    assert v["unmapped"] == [{"quote": "発話に無い指定",
                              "question": contract_rules.UNMAPPED_DEFAULT_QUESTION}]
    assert v["target_paths"] == []


def test_question_form_is_kept():
    """question つきの要素はそのまま契約に載る。"""
    v, problems = contract_rules.validate_contract(
        _raw(unmapped=[{"quote": "対応状況表",
                        "question": "対応状況表はどの文書に含めますか？"}]), SOURCE)
    assert problems == []
    assert v["unmapped"] == [{"quote": "対応状況表",
                              "question": "対応状況表はどの文書に含めますか？"}]


def test_proposal_beyond_target_paths_cap_degrades():
    """target_paths が上限のとき proposal は質問へ縮退する（契約の肥大より人の裁定）。"""
    paths = [f"docs/{c}.md" for c in "abcde"]
    v, problems = contract_rules.validate_contract(
        _raw(target_paths=list(paths),
             unmapped=[{"quote": "対応状況表", "proposal_path": "docs/f.md"}]),
        SOURCE + "\n" + " ".join(paths))
    assert problems == []
    assert v["target_paths"] == paths
    assert v["unmapped"][0]["question"] == contract_rules.UNMAPPED_DEFAULT_QUESTION


def test_directive_contract_degrades_proposal_to_question():
    """directive 型（分解しない）契約では提案を質問へ縮退する — 分解を通らず保存先を
    worker へ届ける経路が無いのに file 検査だけが付くと誤未達になる（§8.10f）。"""
    v, problems = contract_rules.validate_contract(
        _raw(directive="git push", rest_summary=None,
             unmapped=[{"quote": "対応状況表",
                        "proposal_path": "docs/consistency/98-f1-f5-status.md"}]),
        SOURCE + "\ngit push")
    assert problems == []
    assert v["unmapped"] == [{"quote": "対応状況表",
                              "question": contract_rules.UNMAPPED_DEFAULT_QUESTION}]
    assert v["target_paths"] == []


def test_proposal_path_does_not_trigger_stale_binding():
    """提案追記パスは発話由来でないため stale 束縛検査の発火条件に数えない（§8.10f）。

    発話由来フィールドが全て空で unmapped 提案のみの契約（quote は過去発話から）が、
    提案の target_paths 追記を理由に「現在ターン引用なし」で不成立になってはならない。
    """
    utterances = {"u1": SOURCE, "u2": "go"}
    v, problems = contract_rules.validate_contract(
        _raw(unmapped=[{"quote": "対応状況表", "src": "u1",
                        "proposal_path": "docs/consistency/98-f1-f5-status.md"}]),
        "\n".join(utterances.values()), utterances=utterances, current_id="u2")
    assert problems == []
    assert v["unmapped"][0]["proposal_path"] == "docs/consistency/98-f1-f5-status.md"
    assert "docs/consistency/98-f1-f5-status.md" in v["target_paths"]


def test_unmapped_never_invalidates_contract():
    """どの形の unmapped も契約不成立の理由にならない（受理の不変条件）。"""
    for u in (["素の文字列"], [{"quote": "対応状況表"}], [{}], ["", None], [42]):
        v, problems = contract_rules.validate_contract(_raw(unmapped=u), SOURCE)
        assert problems == [], f"unmapped={u!r} で不成立になった: {problems}"
        assert v is not None


# ── 着手確認の提示（_format_contract）──

def _contract(**overrides):
    base = {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "workspace": None, "branch": None, "target_paths": [],
            "needs_repo": False, "rest_summary": "是正作業", "unmapped": []}
    base.update(overrides)
    return base


def test_format_contract_shows_proposal_row():
    """解釈案は「quote → path（保存先未指定のためシステム提案）」の行で提示される。

    固定済み合否テスト（実機）の Slack 表示面: 「対応状況表 → docs/consistency/…
    （システム提案）」の行が出ることの分離テスト側の担保。
    """
    text = ConversationManager._format_contract(_contract(
        target_paths=["docs/consistency/98-f1-f5-status.md"],
        unmapped=[{"quote": "対応状況表",
                   "proposal_path": "docs/consistency/98-f1-f5-status.md"}]))
    assert ("「対応状況表」 → docs/consistency/98-f1-f5-status.md"
            "（保存先未指定のためシステム提案）") in text
    assert "写像できなかった指定" in text


def test_format_contract_shows_question_row():
    """質問型・縮退要素は「quote → 質問: …」の行で提示される。"""
    text = ConversationManager._format_contract(_contract(
        unmapped=[{"quote": "独自の進め方",
                   "question": "この進め方の意図を教えてください"}]))
    assert "「独自の進め方」 → 質問: この進め方の意図を教えてください" in text


def test_format_contract_without_unmapped_adds_no_section():
    """unmapped 空の契約には当該セクションを出さない（提示面を汚さない）。"""
    text = ConversationManager._format_contract(_contract())
    assert "写像できなかった指定" not in text


# ── 分解入力への伝搬（_build_plan）と突き返し分岐の不在 ──

class _CapturePlanService:
    def __init__(self):
        self.summaries = []

    def build(self, summary, progress=None, docs=None):
        self.summaries.append(summary)
        return [{"step": 1, "command": summary, "execution": "agent",
                 "depth": None, "confidence": 0.9, "depends_on": []}]


def test_build_plan_appends_proposal_line_for_worker():
    """承認対象の保存先は分解入力へ定型行で機械追記される（worker が保存先を知る）。"""
    mgr = ConversationManager.__new__(ConversationManager)
    mgr.plan_service = _CapturePlanService()
    mgr.process_mgr = None
    mgr._build_plan("F1〜F5 の是正と対応状況表の作成", contract=_contract(
        unmapped=[{"quote": "対応状況表",
                   "proposal_path": "docs/consistency/98-f1-f5-status.md"}]))
    sent = mgr.plan_service.summaries[0]
    assert ("成果物「対応状況表」の保存先: docs/consistency/98-f1-f5-status.md"
            "（着手確認で承認）") in sent


def test_build_plan_without_proposals_keeps_summary():
    """解釈案が無ければ分解入力は不変（既存動作の維持）。"""
    mgr = ConversationManager.__new__(ConversationManager)
    mgr.plan_service = _CapturePlanService()
    mgr.process_mgr = None
    mgr._build_plan("F1〜F5 の是正", contract=_contract())
    assert mgr.plan_service.summaries[0] == "F1〜F5 の是正"


def test_rejection_text_is_gone_from_conversation():
    """突き返し文（「言い直すか、取り下げてください」）が実行準備の経路に存在しない。

    ソース走査による不在の固定 — 分岐が別の形で復活したら fail する。
    """
    import inspect
    src = inspect.getsource(ConversationManager._prepare_execution)
    assert "取り下げ" not in src
    assert "解釈できませんでした" not in src
