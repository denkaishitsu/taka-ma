"""回答型依頼の背骨（§8.10f 依頼の一生）の固定テスト。

2026-09-07 実測の是正: 「設計を確認して機能一覧を教えて」の依頼で
(5) 完了条件が空のまま契約が成立し、(6) 「完了条件: なし」のまま実行へ進み、
(9) worker が頼まれていない 36KB の文書をリポジトリへ作成し、
(10) 依頼の寿命が無検査で achieved に閉じられ・追加質問に結果の 1 行目しか
返せなかった。各是正を工程 (2)〜(5) の関門として固定する。
"""

import asyncio
import json
import tempfile

from orchestrator import (ANSWER_DISCRETION_NOTE, DEFAULT_DISCRETION_NOTE,
                          Orchestrator)
from orchestrator import contract as contract_rules
from orchestrator.grounding import GroundingReport, GroundingVerifier


# ── 工程 (2) 約束: answered カタログと完了条件の必須化 ──

def test_answered_kind_registered_and_transitional():
    """answered はカタログに登録され、遷移型（着手前 PASS しない）に分類される。"""
    assert "answered" in contract_rules.ACCEPTANCE_KINDS
    assert contract_rules.ACCEPTANCE_KINDS["answered"]["required"] == set()
    assert {"min_chars", "tree_baseline"} == (
        contract_rules.ACCEPTANCE_KINDS["answered"]["optional"])
    assert "answered" in contract_rules.TRANSITION_KINDS


def test_validate_contract_accepts_answered():
    """answered を含む契約はスキーマ検証を通る。"""
    raw = {"directive": None, "constraints": [],
           "acceptance": [{"kind": "answered", "params": {"min_chars": 100}}],
           "runbook": [], "workspace": None, "branch": None, "target_paths": [],
           "needs_repo": False, "rest_summary": "機能一覧を回答で返す", "unmapped": []}
    validated, problems = contract_rules.validate_contract(raw, "一覧を教えて")
    assert problems == []
    assert validated["acceptance"] == [
        {"kind": "answered", "params": {"min_chars": 100}}]


# ── 工程 (5) 検査: GroundingVerifier の answered 分岐 ──

class _Probe:
    """SSH probe スタブ。git status --porcelain の現状出力を差し替え可能にする。"""

    def __init__(self, status_out=""):
        self.status_out = status_out
        self.commands = []

    def __call__(self, cmd, timeout):
        self.commands.append(cmd)
        if "status --porcelain" in cmd:
            return 0, self.status_out
        return 0, ""


def _verify(acceptance, ctx, probe=None):
    v = GroundingVerifier(probe or _Probe())
    return v.verify_acceptance("/repo", acceptance, answered_ctx=ctx)


def test_answered_passes_with_body_and_unchanged_tree():
    """回答本文が閾値以上・作業ツリー不変（クリーン→クリーン）なら PASS。"""
    report = _verify(
        [{"kind": "answered",
          "params": {"min_chars": 10, "tree_baseline": ""}}],
        {"result_chars": 500}, probe=_Probe(status_out=""))
    assert report.ok


def test_answered_fails_without_result_context():
    """回答本文へ到達できない（ctx なし）は判定不能 = FAIL（fail-closed）。"""
    report = _verify([{"kind": "answered", "params": {}}], None)
    assert not report.ok
    assert "answered" in report.note
    assert report.cause == "acceptance_failed:answered"


def test_answered_fails_on_short_body():
    """回答本文が min_chars 未満は未達。"""
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 100}}],
        {"result_chars": 3})
    assert not report.ok


def test_answered_fails_when_tree_changed():
    """約束の外のファイル出現 = 頼まれていない成果物の検出（2026-09-07 の是正）。"""
    report = _verify(
        [{"kind": "answered",
          "params": {"min_chars": 1, "tree_baseline": ""}}],
        {"result_chars": 500}, probe=_Probe(status_out="?? junk/unrequested.md\n"))
    assert not report.ok
    assert "約束の外の変化" in report.note


def test_answered_allows_promised_file_creation():
    """複合依頼: 契約が約束したファイル（file 系検査の path）の作成は正当な差分として
    許す（§8.10f 検査 (3) の一般化。kind 単発の「不変」判定が複合依頼で正当な成果物を
    誤検知した 2026-09-09 の欠陥の是正）。"""
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": ""}},
         {"kind": "file", "params": {"path": "docs/90-review.md"}}],
        {"result_chars": 500},
        probe=_Probe(status_out="?? docs/90-review.md\n"))
    # file 検査自体は実在プローブが 0/"" を返すスタブでは通らないため、answered の
    # 判定行だけを見る（約束パスの変化が「約束の外」に数えられていないこと）
    assert "約束の外の変化" not in report.note


def test_answered_no_closure_with_modification_promise():
    """回答＋変える約束（file_changed 等）はツリー閉包を課さない（§8.10f 4 行表）。

    「直して、教えて」型で worker の正当な波及（未約束ファイルへの修正）を
    誤検知しない。ファイル側の規律は file 系検査・遵守照合が担保する。"""
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": ""}},
         {"kind": "file_changed", "params": {"path": "src/x.py", "baseline": "abc0"}}],
        {"result_chars": 500},
        probe=_Probe(status_out=" M src/x.py\n M src/helper.py\n"))
    assert "約束の外の変化" not in report.note
    assert "ツリー閉包は課さない" in report.text


def test_answered_closure_forced_off_by_caller_ctx():
    """呼び出し側の閉包判定（runbook 含む契約全体からの決定）が最優先。"""
    from orchestrator import tree_closure_applies
    acc = [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": ""}}]
    assert tree_closure_applies(acc) is True
    assert tree_closure_applies(acc, runbook=[{"kind": "push", "params": {}}]) is False
    v = GroundingVerifier(_Probe(status_out="?? junk.md\n"))
    report = v.verify_acceptance("/repo", acc,
                                 answered_ctx={"result_chars": 10, "closure": False})
    assert report.ok        # 閉包 off なら junk があっても answered は落ちない


def test_answered_fails_on_mixed_promised_and_unpromised():
    """約束したファイルと約束外ファイルが混在 → 約束外の行だけを理由に未達。"""
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": ""}},
         {"kind": "file", "params": {"path": "docs/90-review.md"}}],
        {"result_chars": 500},
        probe=_Probe(status_out="?? docs/90-review.md\n?? stray.tmp\n"))
    assert "約束の外の変化" in report.note
    assert "stray.tmp" in report.note
    assert "90-review.md" not in report.note.split("約束の外の変化")[1].split("—")[0]


def test_answered_skips_tree_check_without_baseline():
    """tree_baseline が "-"（workspace 無し等）は当該項目をスキップ（FAIL に偽らない）。"""
    probe = _Probe()
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": "-"}}],
        {"result_chars": 10}, probe=probe)
    assert report.ok
    assert not any("status --porcelain" in c for c in probe.commands)


def test_answered_checks_empty_baseline():
    """クリーンなツリーの baseline（空文字列）はスキップせず検査する（"-" とは別物）。"""
    probe = _Probe(status_out="?? new.md\n")
    report = _verify(
        [{"kind": "answered", "params": {"min_chars": 1, "tree_baseline": ""}}],
        {"result_chars": 10}, probe=probe)
    assert not report.ok
    assert any("status --porcelain" in c for c in probe.commands)


# ── 工程 (3) 実行: worker 指示の決定的分岐 ──

def test_discretion_note_derived_from_acceptance_set():
    """既定裁量は acceptance 集合全体から 4 分岐で導く（§8.10f 配布規則・決定的）。

    kind 単発（answered の有無だけ）の分岐は、複合依頼で「ファイルを作るな」と
    file 系完了条件が正面衝突する欠陥だった（2026-09-09 検討で検出）。閉包文言は
    ツリー閉包が適用される契約（回答のみ・回答＋作る約束のみ）にだけ付き、
    指示（事前）と検査（事後）が同じ境界を語る。"""
    from orchestrator import (MIXED_DISCRETION_NOTE,
                              MODIFY_ANSWER_DISCRETION_NOTE, _discretion_note)
    file_only = {"acceptance": [{"kind": "file", "params": {"path": "a.md"}}]}
    answer_only = {"acceptance": [{"kind": "answered", "params": {}}]}
    create_mix = {"acceptance": [{"kind": "answered", "params": {}},
                                 {"kind": "file_min_bytes",
                                  "params": {"path": "a.md", "min_bytes": 100}}]}
    modify_mix = {"acceptance": [{"kind": "answered", "params": {}},
                                 {"kind": "file_changed",
                                  "params": {"path": "src/x.py"}}]}
    runbook_mix = {"acceptance": [{"kind": "answered", "params": {}}],
                   "runbook": [{"kind": "commit_paths",
                                "params": {"paths": ["a.md"], "message": "m"}}]}
    assert _discretion_note(file_only) is DEFAULT_DISCRETION_NOTE
    assert _discretion_note(answer_only) is ANSWER_DISCRETION_NOTE
    assert _discretion_note(create_mix) is MIXED_DISCRETION_NOTE
    assert _discretion_note(modify_mix) is MODIFY_ANSWER_DISCRETION_NOTE
    assert _discretion_note(runbook_mix) is MODIFY_ANSWER_DISCRETION_NOTE
    assert "約束にないファイルは作らない" in MIXED_DISCRETION_NOTE
    assert "あわせて回答を報告" in MIXED_DISCRETION_NOTE
    assert "約束にないファイル" not in MODIFY_ANSWER_DISCRETION_NOTE
    assert "あわせて回答を報告" in MODIFY_ANSWER_DISCRETION_NOTE


# ── 工程 (4) 届け: 送信成否の伝搬と再送 1 回 ──

class _FlakySlack:
    """1 回目 False → 2 回目 True の送信スタブ（再送 1 回の検証）。"""

    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.sent = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.sent.append(text)
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        return True


def _bare():
    o = Orchestrator.__new__(Orchestrator)
    return o


def test_notify_chunked_retries_once_and_reports_delivered():
    """送信失敗チャンクは 1 回だけ再送し、成功すれば delivered=True。"""
    o = _bare()
    o.slack = _FlakySlack(fail_times=1)
    delivered = asyncio.run(o._notify_chunked("完了:", "本文"))
    assert delivered is True
    assert len(o.slack.sent) == 2      # 失敗 1 + 再送 1


def test_notify_chunked_reports_failure_after_retry():
    """再送しても失敗なら delivered=False（届いていない事実を返す）。"""
    o = _bare()
    o.slack = _FlakySlack(fail_times=2)
    delivered = asyncio.run(o._notify_chunked("完了:", "本文"))
    assert delivered is False


def test_finalize_goal_blocks_achieved_when_not_delivered():
    """answered を含む依頼は、届いていなければ検査 PASS でも achieved にしない。"""
    o = _bare()
    o.conversation = None
    o.intents_dir = None               # intent 更新は本テストの対象外（早期 return）
    task = {"task_id": "t1", "acceptance": [{"kind": "answered", "params": {}}]}
    grounding = GroundingReport(ok=True, note="", text="", summary="")
    o._finalize_goal(task, grounding, delivered=False)
    assert grounding.ok is False
    assert "届いていない" in grounding.note
    assert grounding.cause == "acceptance_failed:answered"


def test_record_delivery_stamps_terminal_record():
    """完了通知の送信成否は終端記録へ delivery_ok として刻まれる。"""
    o = _bare()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"task_id": "t1", "status": "completed"}, f)
        path = f.name
    o._record_delivery(path, True)
    with open(path) as f:
        assert json.load(f)["delivery_ok"] is True


# ── 工程 (2) 約束: 完了条件の必須化（空契約の不成立と聞き返し） ──

from orchestrator.conversation import ConversationManager


class _FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)
        return True


class _FakeContractor:
    def __init__(self, parsed):
        self.parsed = parsed

    def contract(self, history_text, summary, validate, progress=None):
        validated, _ = validate(self.parsed)
        return validated, {"origin": "opus", "backend": "worker_cli",
                           "unreachable": False, "attempts": []}


def _manager():
    tmp = tempfile.mkdtemp()
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp},
        "conversation": {"sessions_dir": tempfile.mkdtemp(),
                         "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work",
                         "worker_home": "/Users/dev"},
        "ya-ta": {"model": "dummy", "llm_timeout_sec": 60},
        "models": {},
        "contract": {"intents_dir": tempfile.mkdtemp()},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp)


def _empty_raw():
    return {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "workspace": None, "branch": None, "target_paths": [],
            "needs_repo": False, "rest_summary": None, "unmapped": []}


def test_build_contract_rejects_empty_acceptance():
    """既定付与を経ても完了条件が空の契約は不成立（fail-closed・工程 (2) の関門）。"""
    mgr = _manager()
    mgr._append_turn("c1", "user", "設計に問題がないこと確認して一覧を教えて")
    mgr.contractor = _FakeContractor(_empty_raw())
    contract, prov = mgr._build_contract("c1", "設計を確認して一覧を提示する")
    assert contract is None
    assert prov.get("empty_acceptance") is True


def test_empty_acceptance_asks_full_restatement():
    """空契約は着手確認を出さず、完了条件を含めた依頼全体の言い直しを求める。

    完了条件だけの短い補足返答は意図判定で chat に落ち契約化へ再入しない経路が
    あり得るため、言い直し（依頼全体の再送）を再入の入口として固定する。"""
    mgr = _manager()
    mgr._append_turn("c1", "user", "一覧を教えて")
    mgr.contractor = _FakeContractor(_empty_raw())
    result = mgr._prepare_execution(
        {"conversation_id": "c1", "channel_id": "C1"}, "c1", "一覧を提示する", None)
    assert result is None
    text = mgr.slack.notes[-1]
    assert "完了条件" in text
    assert "依頼全体を言い直して" in text


def test_capture_tree_baseline_stamps_dash_without_workspace():
    """workspace 無し（実測不能）は tree_baseline を "-" で刻む（FAIL に偽らない）。"""
    mgr = _manager()
    contract = {"acceptance": [{"kind": "answered", "params": {}}]}
    mgr._capture_tree_baseline(contract, None)
    assert contract["acceptance"][0]["params"]["tree_baseline"] == "-"


def test_capture_tree_baseline_stamps_sorted_lines_with_workspace():
    """workspace があるときは git status --porcelain の行集合（整列テキスト）を刻む。"""
    mgr = _manager()

    class _SSH:
        def run_ssh_probe(self, cmd, timeout):
            assert "status --porcelain" in cmd
            return 0, "?? b.md\n M a.md\n"

    mgr.process_mgr = _SSH()
    contract = {"acceptance": [{"kind": "answered", "params": {}}]}
    mgr._capture_tree_baseline(contract, "/repo")
    assert contract["acceptance"][0]["params"]["tree_baseline"] == " M a.md\n?? b.md"


def test_capture_tree_baseline_clean_tree_is_empty_string():
    """クリーンなツリーは空文字列（"-" ではない = 出口で検査される）。"""
    mgr = _manager()

    class _SSH:
        def run_ssh_probe(self, cmd, timeout):
            return 0, ""

    mgr.process_mgr = _SSH()
    contract = {"acceptance": [{"kind": "answered", "params": {}}]}
    mgr._capture_tree_baseline(contract, "/repo")
    assert contract["acceptance"][0]["params"]["tree_baseline"] == ""


# ── 工程 (3): ハーネス自身が workspace を汚さない（フック settings の配置） ──

def test_hook_settings_path_outside_workspace():
    """フック settings は依頼者の workspace の外（一時ディレクトリ）に置く。

    workspace 直下に置くと依頼者のリポジトリを毎実行汚し、answered の作業ツリー
    検査をハーネス自身の生成物が誤検知させる（2026-09-09 実機 E2E 実測:
    worker はファイルを作らなかったのに未達判定）。"""
    o = _bare()
    path = o._hook_settings_path("abc123-step1-opus")
    assert path == "/tmp/taka-ma-hooks/abc123-step1-opus.json"
    assert "/DevDev/" not in path and not path.startswith("/opt/taka-ma/work")
