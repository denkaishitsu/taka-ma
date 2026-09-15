"""入口ゲート — 依頼⇄契約の独立突合（設計書 §8.10f「入口ゲート」・#173）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
  - 合否のコード導出: LLM の対応宣言を採らず、参照が契約の実体へ解決できたときのみ covered
    （存在しない参照＝捏造対応は uncovered に落ちる）
  - 検証系要求の決定的振り分け: is_verification_text（SSOT）に掛かる要求は mapped_to に
    依らず「出口ゲートが担当」
  - 空の対応表・JSON 逸脱の 1 回リトライと未検査（unchecked）— 着手確認は止めない
  - 遮断: プロンプトに unmapped（契約化脳の自己申告）が入らない
  - 着手確認の対応表ブロック（❌ 行の明示・未検査の注記）のコード組立
  - 受注不備の冒頭注記（misbooking_notice）の決定的条件（❌ 行 × 閉包の実測パス列）
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import json

from orchestrator.entry_gate import (
    STATUS_EXIT_GATE,
    STATUS_FIELD,
    STATUS_UNCOVERED,
    EntryGateChecker,
    EntryGateReport,
    misbooking_notice,
    render_table,
    resolve_reference,
)

_TEMPLATE = "依頼:\n{request_doc}\n要約:\n{summary}\n契約:\n{contract_doc}"

_CONTRACT = {
    "directive": None,
    "constraints": [{"text": "テストは行わない", "forbid": True}],
    "acceptance": [{"kind": "file", "params": {"path": "docs/01.md"}},
                   {"kind": "answered", "params": {"min_chars": 1}}],
    "runbook": [],
    "workspace": "/repo",
    "branch": None,
    "target_paths": ["docs/01.md"],
    "rest_summary": "docs/01 と 02 の指摘を是正する",
    "unmapped": [],
}


def _llm_factory(outputs: list[str], calls: list[str] | None = None):
    seq = list(outputs)

    def run_llm(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return run_llm


def _items_json(items) -> str:
    return json.dumps({"items": items}, ensure_ascii=False)


# ── 参照解決（合否のコード導出の土台） ──

def test_resolve_reference_accepts_only_existing_fields():
    assert resolve_reference(_CONTRACT, "acceptance[0]")
    assert resolve_reference(_CONTRACT, "acceptance[1]")
    assert resolve_reference(_CONTRACT, "constraints[0]")
    assert resolve_reference(_CONTRACT, "target_paths[0]")
    assert resolve_reference(_CONTRACT, "rest_summary")
    assert resolve_reference(_CONTRACT, "workspace")


def test_resolve_reference_rejects_fabricated_and_empty():
    assert not resolve_reference(_CONTRACT, "acceptance[2]")     # 添字が範囲外
    assert not resolve_reference(_CONTRACT, "directive")         # null フィールド
    assert not resolve_reference(_CONTRACT, "branch")            # null フィールド
    assert not resolve_reference(_CONTRACT, "runbook[0]")        # 空リスト
    assert not resolve_reference(_CONTRACT, "unmapped")          # 参照文法外のフィールド
    assert not resolve_reference(_CONTRACT, "summary")           # 存在しないフィールド名
    assert not resolve_reference(_CONTRACT, "workspace[0]")      # スカラーへの添字
    assert not resolve_reference(_CONTRACT, "acceptance[0]; rm") # 文法外
    assert not resolve_reference(_CONTRACT, None)


# ── 合否のコード導出（LLM の対応宣言は採らない） ──

def test_covered_only_when_reference_resolves():
    raw = _items_json([
        {"req": "01/02 の是正", "src": "u1", "mapped_to": ["acceptance[0]"]},
        {"req": "16 件の対応状況表", "src": "u1", "mapped_to": []},
        {"req": "存在しない参照で捏造", "src": "u1", "mapped_to": ["acceptance[9]"]},
    ])
    checker = EntryGateChecker(_llm_factory([raw]), _TEMPLATE)
    report = checker.check("[u1] ユーザー: 依頼", "要約", dict(_CONTRACT))
    assert not report.unchecked
    statuses = {it["req"]: it["status"] for it in report.items}
    assert statuses["01/02 の是正"] == STATUS_FIELD
    assert statuses["16 件の対応状況表"] == STATUS_UNCOVERED
    assert statuses["存在しない参照で捏造"] == STATUS_UNCOVERED
    assert len(report.uncovered()) == 2


def test_verification_request_routed_to_exit_gate_deterministically():
    # is_verification_text に掛かる要求は mapped_to の内容（空・捏造参照）に依らず
    # 出口ゲートへ振り分けられる（語彙 SSOT・#169 の一本化）
    raw = _items_json([
        {"req": "独立検証レポートを添付", "src": "u1", "mapped_to": []},
        {"req": "検証レポート", "src": "u1", "mapped_to": ["acceptance[0]"]},
    ])
    checker = EntryGateChecker(_llm_factory([raw]), _TEMPLATE)
    report = checker.check("view", "要約", dict(_CONTRACT))
    assert all(it["status"] == STATUS_EXIT_GATE for it in report.items)
    assert not report.uncovered()


# ── リトライと未検査（fail-open を無印にしない） ──

def test_empty_items_retries_then_unchecked():
    calls: list[str] = []
    checker = EntryGateChecker(
        _llm_factory([_items_json([]), "こわれた出力"], calls), _TEMPLATE)
    report = checker.check("view", "要約", dict(_CONTRACT))
    assert len(calls) == 2                      # 1 回リトライ
    assert report.unchecked and report.items == []


def test_llm_exception_then_valid_output_succeeds():
    raw = _items_json([{"req": "01 の是正", "src": "u1",
                        "mapped_to": ["acceptance[0]"]}])
    seq = [RuntimeError("SSH 不達"), raw]

    def run_llm(prompt: str) -> str:
        out = seq.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    report = EntryGateChecker(run_llm, _TEMPLATE).check("v", "s", dict(_CONTRACT))
    assert not report.unchecked and report.items[0]["status"] == STATUS_FIELD


def test_invalid_item_shape_is_rejected():
    # req 欠落・mapped_to 非リストは対応表として解釈しない（リトライ→未検査）
    bad1 = _items_json([{"mapped_to": []}])
    bad2 = _items_json([{"req": "a", "mapped_to": "acceptance[0]"}])
    report = EntryGateChecker(_llm_factory([bad1, bad2]), _TEMPLATE).check(
        "v", "s", dict(_CONTRACT))
    assert report.unchecked


# ── 遮断（unmapped＝契約化脳の自己申告をプロンプトに入れない） ──

def test_prompt_excludes_unmapped_and_includes_request_and_contract():
    calls: list[str] = []
    contract = dict(_CONTRACT)
    contract["unmapped"] = ["脳が自覚した落とし物"]
    raw = _items_json([{"req": "01 の是正", "src": "u1", "mapped_to": []}])
    EntryGateChecker(_llm_factory([raw], calls), _TEMPLATE).check(
        "[u1] ユーザー: 01 を直して", "01 を是正する", contract)
    prompt = calls[0]
    assert "脳が自覚した落とし物" not in prompt
    assert "[u1] ユーザー: 01 を直して" in prompt
    assert "docs/01.md" in prompt               # 契約は入っている


# ── 着手確認の対応表ブロック（コード組立） ──

def test_render_table_marks_uncovered_and_exit_gate():
    report = EntryGateReport(items=[
        {"req": "01/02 の是正", "src": "u1", "refs": ["acceptance[0]"],
         "status": STATUS_FIELD},
        {"req": "16 件の対応状況表", "src": "u1", "refs": [],
         "status": STATUS_UNCOVERED},
        {"req": "検証レポート", "src": "u1", "refs": [],
         "status": STATUS_EXIT_GATE},
    ])
    text = render_table(report.to_record(), _CONTRACT)
    assert "契約に載っていない要求が 1 件" in text
    assert "✅ 01/02 の是正 → 完了条件1（file）" in text
    assert "❌ 16 件の対応状況表 → 契約に無い" in text
    assert "✅ 検証レポート → 出口ゲートが担当" in text
    # 部分列挙への保険: 表に無い要求の読み方（契約に無い側へ倒す）を固定注記で示す
    assert "この表に無い要求は「❌ 契約に無い」とみなして" in text


def test_render_table_unchecked_notes_untested():
    record = EntryGateReport(unchecked=True, note="応答を解釈できない").to_record()
    text = render_table(record, _CONTRACT)
    assert "未検査" in text and "応答を解釈できない" in text
    assert "❌" not in text                      # 未検査は ❌（契約に無い）と混同させない


# ── 受注不備の冒頭注記（失敗報告の帰属区別・出口側） ──

def test_misbooking_notice_requires_both_uncovered_and_outside():
    record = EntryGateReport(items=[
        {"req": "16 件の対応状況表", "src": "u1", "refs": [],
         "status": STATUS_UNCOVERED}]).to_record()
    covered_only = EntryGateReport(items=[
        {"req": "01 の是正", "src": "u1", "refs": ["acceptance[0]"],
         "status": STATUS_FIELD}]).to_record()
    outside = ["?? docs/consistency/96-implementation-report.md"]
    assert misbooking_notice(record, []) is None            # 閉包 FAIL なし
    assert misbooking_notice(covered_only, outside) is None  # ❌ 行なし
    assert misbooking_notice({}, outside) is None            # 対応表なし（旧タスク）
    text = misbooking_notice(record, outside)
    assert text is not None
    assert "受注処理に不備の可能性" in text
    assert "worker の逸脱と断定しません" in text
    assert "16 件の対応状況表" in text
    assert outside[0] in text
    assert "未検査" in text and "未達ではありません" in text
