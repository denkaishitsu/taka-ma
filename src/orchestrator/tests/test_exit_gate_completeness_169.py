"""出口ゲートの取りこぼし是正（設計書 §8.10f / §10.2）の振る舞いテスト。

0.14.0 の出口ゲートが 2026-09-14 の実運用で機能しなかった 2 経路を塞ぐ:
  (A) 判定表の形式的完全性を機械検査し、不足なら上位モデルで再実行する（昇格の第 3 の
      引き金）。正常終了した不完全な検証を素通りさせない。昇格先が無い・再実行しても
      なお不足なら fail-closed（exit_gate_unverified）。
  (B) 依頼文が検証を求めても、分解は検証サブタスクを作らない（検証は出口ゲートの経路
      だけに落とす）。落とした step に依存していた後続は依存を引き継ぐ。

grep では潰せない振る舞い（件数判定・昇格の発火と停止・依存の張り直し）を分離実行で
担保する。実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import types

from orchestrator import Orchestrator, _expected_verdict_count
from orchestrator.exit_gate import ExitGateReport
from orchestrator.plan import (PlanService, drop_verification_subtasks,
                               is_verification_subtask)


def _report(n_items: int, cause=None) -> ExitGateReport:
    """判定表 n 件の報告を作る（中身は完全性判定に影響しない）。"""
    items = [{"req": f"指摘{i}", "verdict": "resolved", "evidence": f"a.md:{i}"}
             for i in range(1, n_items + 1)]
    return ExitGateReport(ok=not cause, note="", text="【独立検証】…", cause=cause,
                          items=items, new_issues=[])


def _orch(ladder=("haiku", "sonnet", "opus"), gate_model="haiku") -> Orchestrator:
    o = Orchestrator.__new__(Orchestrator)
    o.config = {
        "exit_gate": {"model": gate_model, "timeout_sec": 5, "max_reinject": 1},
        "routing": {"escalation": {"ladder": list(ladder)}},
    }
    return o


# ── (A-1) 期待件数の機械的な数え方 ──

def test_expected_count_counts_explicit_markers_plus_contract_fields():
    task = {"command": "指摘1『A』を解消し、指摘2『B』を解消する。",
            "acceptance": [{"kind": "file_changed", "params": {}}]}
    assert _expected_verdict_count(task) == (3, True)  # 指摘 2 件 + 完了条件 1 件・宣言あり


def test_expected_count_does_not_double_count_repeated_markers():
    # 同じ番号の再言及は 1 件（distinct）
    task = {"command": "指摘1 を直す。なお指摘1 は前回も出ている。指摘2 も直す。"}
    assert _expected_verdict_count(task) == (2, True)


def test_bullet_lines_are_not_counted_as_declared_items():
    # 実運用の依頼は箇条書きに前提・決定事項・成果物が混ざる。これを件数とみなすと
    # 期待値が膨らみ、済んだ仕事を未達と呼ぶ。数えるのは宣言された番号だけ
    task = {"command": "前提:\n- repo: ~/x\n- docs: ~/x/docs\n成果物:\n- 手順書\n- 一覧表",
            "constraints": [{"text": "テストはしない", "forbid": True}]}
    assert _expected_verdict_count(task) == (2, False)  # 1（下限）+ 拘束条件 1・宣言なし


def test_expected_count_is_at_least_one_for_prose():
    assert _expected_verdict_count({"command": "README を直す"}) == (1, False)


# ── (A-2) 昇格先の解決（ラダーが唯一の源） ──

def test_next_model_is_the_one_above_in_the_ladder():
    assert _orch(gate_model="haiku")._exit_gate_next_model() == "sonnet"
    assert _orch(gate_model="sonnet")._exit_gate_next_model() == "opus"


def test_next_model_is_none_at_top_or_outside_the_ladder():
    assert _orch(gate_model="opus")._exit_gate_next_model() is None
    assert _orch(gate_model="fable")._exit_gate_next_model() is None   # ラダー外


# ── (A-3) 完全性検査と第 3 の引き金 ──

def test_complete_table_passes_through_untouched():
    o = _orch()
    task = {"command": "指摘1 と 指摘2 を直す"}   # 件数の宣言あり
    report = _report(2)
    called = []
    out = o._complete_exit_gate(task, report, lambda m=None: called.append(m))
    assert out is report and not called                # 再実行しない


def test_incomplete_table_reruns_with_the_next_model():
    o = _orch(gate_model="haiku")
    task = {"command": "指摘1 と 指摘2 を直す"}
    used = []

    def verify(model_name=None):
        used.append(model_name)
        return _report(2)                              # 上位モデルは全件そろえて返す

    out = o._complete_exit_gate(task, _report(1), verify)
    assert used == ["sonnet"]                          # 昇格先で 1 回だけ取り直す
    assert out.ok and out.cause is None
    assert "sonnet で再実行" in out.text                # 昇格の事実がレポートに残る


def test_still_incomplete_after_escalation_fails_closed_when_count_declared():
    # 依頼が「指摘1・指摘2」と件数を宣言しているときだけ未達まで倒す
    o = _orch(gate_model="haiku")
    task = {"command": "指摘1 と 指摘2 を直す"}
    out = o._complete_exit_gate(task, _report(1), lambda model_name=None: _report(1))
    assert not out.ok and out.cause == "exit_gate_unverified"
    assert "独立検証が不完全" in out.note and "依頼が件数を宣言" in out.note


def test_prose_request_is_never_failed_for_count_shortfall():
    # 件数の宣言が無い散文依頼（実運用の大多数）。期待値は推定にすぎないため
    # 不足しても未達にはせず、注記だけ残して判定表の verdict に委ねる
    o = _orch(gate_model="opus")                       # 昇格先なし
    task = {"command": "設計書を読んで整合性を直してほしい",
            "acceptance": [{"kind": "file_changed", "params": {}},
                           {"kind": "pushed", "params": {}}]}
    report = _report(1)
    out = o._complete_exit_gate(task, report, lambda m=None: _report(1))
    assert out is report and out.ok and out.cause is None   # 未達にしない
    assert "未達判定には用いない" in out.text                # 注記は残る


def test_prose_request_still_escalates_once_before_accepting():
    o = _orch(gate_model="haiku")
    task = {"command": "設計書を直す", "acceptance": [{"kind": "file_changed"},
                                                     {"kind": "pushed"}]}
    used = []

    def verify(model_name=None):
        used.append(model_name)
        return _report(1)

    out = o._complete_exit_gate(task, _report(1), verify)
    assert used == ["sonnet"]                          # 上位モデルでの取り直しは行う
    assert out.ok and out.cause is None                # それでも未達にはしない


def test_incomplete_without_escalation_target_fails_closed():
    o = _orch(gate_model="opus")                       # ラダー最上位＝昇格先なし
    task = {"command": "指摘1 と 指摘2 を直す"}        # 件数の宣言あり
    called = []
    out = o._complete_exit_gate(task, _report(1), lambda m=None: called.append(m))
    assert not called                                  # 再実行しようがない
    assert not out.ok and out.cause == "exit_gate_unverified"
    assert "昇格先のモデルが無い" in out.note


def test_unparsable_report_is_left_as_is():
    # 判定表が取れていない報告は exit_gate.py 側で既に fail-closed 済み
    o = _orch()
    report = ExitGateReport(ok=False, note="解釈できない", text="…",
                            cause="exit_gate_unverified")
    out = o._complete_exit_gate({"command": "指摘1 を直す"}, report,
                                lambda m=None: _report(9))
    assert out is report


# ── (B) 検証サブタスクを分解結果から落とす ──

def test_detects_only_exit_gate_vocabulary():
    assert is_verification_subtask({"command": "独立検証レポートを作成する"})
    assert is_verification_subtask({"command": "指摘の全件突合を行う"})
    assert not is_verification_subtask({"command": "テストを実行して結果を報告する"})
    assert not is_verification_subtask({"command": "docs/spec.md を改版する"})


def test_drops_verification_subtask_and_rewires_dependents():
    subtasks = [
        {"step": 1, "command": "docs/spec.md を改版する", "depends_on": []},
        {"step": 2, "command": "独立検証レポートを作成する", "depends_on": [1]},
        {"step": 3, "command": "結果を要約する", "depends_on": [2]},
    ]
    out = drop_verification_subtasks(subtasks)
    assert [s["step"] for s in out] == [1, 3]
    assert out[1]["depends_on"] == [1]                 # 落とした step の依存を引き継ぐ


def test_rewires_through_consecutive_dropped_steps():
    subtasks = [
        {"step": 1, "command": "実装する", "depends_on": []},
        {"step": 2, "command": "判定表を作る", "depends_on": [1]},
        {"step": 3, "command": "検証レポートをまとめる", "depends_on": [2]},
        {"step": 4, "command": "納品物を整える", "depends_on": [3]},
    ]
    out = drop_verification_subtasks(subtasks)
    assert [s["step"] for s in out] == [1, 4]
    assert out[1]["depends_on"] == [1]                 # 推移的に解決


def test_keeps_plan_when_every_subtask_is_verification():
    # 依頼そのものが検証の依頼。落とすと実行する物が無くなるため入力を返す
    subtasks = [{"step": 1, "command": "独立検証レポートを作成する", "depends_on": []}]
    assert drop_verification_subtasks(subtasks) is subtasks


def test_no_op_when_nothing_matches():
    subtasks = [{"step": 1, "command": "README を直す", "depends_on": []}]
    assert drop_verification_subtasks(subtasks) is subtasks


def test_plan_service_build_applies_the_filter():
    decomposer = types.SimpleNamespace(decompose=lambda summary, progress=None: [
        {"step": 1, "command": "docs/spec.md を改版する", "depends_on": []},
        {"step": 2, "command": "独立検証レポートを作成する", "depends_on": [1]},
    ])
    service = PlanService(decomposer, corrector=None, resolve=None, valid_models=[])
    assert [s["step"] for s in service.build("依頼")] == [1]


# ── (B-2) 依頼が「独立検証レポート」を成果物に書いても自己検証を誘発しない ──

def _mixed_report(verification_unresolved=True, other_bad=False, new_issues=()):
    items = [{"req": "docs/plan-b.md を新規作成する", "verdict": "resolved", "evidence": "ls"},
             {"req": "成果物(2): 独立検証レポート（判定表）を提出する",
              "verdict": "unresolved" if verification_unresolved else "resolved",
              "evidence": "証跡に実体なし"}]
    if other_bad:
        items.append({"req": "リリース手順を 3 行で書く", "verdict": "partial", "evidence": "2 行"})
    return ExitGateReport(ok=False, note="未達", text="【独立検証】…", cause="exit_gate_failed",
                          items=items, new_issues=list(new_issues))


def test_request_for_the_verification_report_itself_is_not_a_worker_shortfall():
    # これを未達のままにすると差し戻され、worker が自前の検証レポートを書き始める
    o = _orch()
    task = {"command": "docs/plan-b.md を作る。成果物: 独立検証レポート"}
    out = o._complete_exit_gate(task, _mixed_report(), lambda m=None: None)
    assert out.ok and out.cause is None
    assert all(i["verdict"] == "resolved" for i in out.items)
    assert "本レポートで充足済み" in out.text


def test_real_shortfall_is_never_relaxed_by_the_self_reference_rule():
    o = _orch()
    task = {"command": "docs/plan-b.md を作る。成果物: 独立検証レポート"}
    out = o._complete_exit_gate(task, _mixed_report(other_bad=True), lambda m=None: None)
    assert not out.ok and out.cause == "exit_gate_failed"


def test_new_issues_are_never_relaxed_by_the_self_reference_rule():
    o = _orch()
    task = {"command": "docs/plan-b.md を作る。成果物: 独立検証レポート"}
    report = _mixed_report(new_issues=[{"issue": "章番号の重複", "evidence": "a.md:3"}])
    out = o._complete_exit_gate(task, report, lambda m=None: None)
    assert not out.ok and out.cause == "exit_gate_failed"


# ── (A-4) 「N 件」の総数宣言と個別番号の読み分け ──

def test_declared_total_is_read_as_a_total_not_as_an_id():
    # 「指摘 16 件」を番号 16 番と読むと 1 件に潰れる（2026-09-14 実運用の依頼で実測）
    task = {"command": "前回の是正結果を独立検証した指摘 16 件（残余 4 件 + 新規 12 件）に対応する",
            "acceptance": [{"kind": "file"}, {"kind": "file"}, {"kind": "answered"}],
            "constraints": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}
    expected, strict = _expected_verdict_count(task)
    assert expected == 22          # 16 + acceptance 3 + constraints 3
    assert strict is False         # 総数宣言だけでは未達まで倒さない（まとめは正当）


def test_individual_numbering_stays_strict():
    task = {"command": "指摘1 と 指摘2 と 指摘3 を直す"}
    assert _expected_verdict_count(task) == (3, True)


def test_total_and_individual_numbering_take_the_larger():
    task = {"command": "指摘 5 件を直す。まず 指摘1、次に 指摘2。"}
    expected, strict = _expected_verdict_count(task)
    assert expected == 5           # 総数 5 > 個別 2
    assert strict is True          # 個別番号があるので厳格


def test_declared_total_shortfall_escalates_but_never_fails():
    o = _orch(gate_model="haiku")
    task = {"command": "指摘 16 件に対応する"}
    used = []

    def verify(model_name=None):
        used.append(model_name)
        return _report(3)

    out = o._complete_exit_gate(task, _report(3), verify)
    assert used == ["sonnet"]      # 取り直しはする
    assert out.ok and out.cause is None   # 総数宣言だけでは未達にしない
