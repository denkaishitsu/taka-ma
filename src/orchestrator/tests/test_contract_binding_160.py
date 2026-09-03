"""発話→契約の束縛（設計書 §8.10f スキーマ閉包・出所束縛・branch / §8.10g switch 機械付与・
弁別力規則 / §8.10b stale 計画の失効）。

2026-08-29 実障害（obsidian-auto-stock-trader チャンネル）の再発防止を固定する:
- 発話で指定されたブランチが契約のどこにも入らず、checkout 中の別ブランチ上で
  計画・実行・検査が走った → branch の契約項目化・switch 機械付与・測定の ref 化
- 実装設計書レビュー中の発話に対し過去スレッドの要件定義書計画へ束縛した契約が提示
  された → 出所束縛（現在ターン引用の機械検査）・stale pending の失効
- 既存ファイルの不具合報告に「完了条件は既に満たされています」と実行拒否 → 弁別力規則
- 契約散文への内部独白の混入 → unmapped（スキーマ閉包）とコード組立の提示

conversation.py は test_exec_contract_149.py と同方式でファイル直ロードする。
"""

import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway import contractor as contractor_mod  # noqa: E402
from orchestrator import contract as contract_rules  # noqa: E402
from orchestrator import grounding  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_160", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager

SOURCE = ("ブランチ: feature/design-implementation\n"
          "設計書: docs/02-implementation-design.md\n"
          "mermaid のフロー図がレンダリングされないので直せ")

UTTERANCES = {"u1": "repo:/Users/dev/DevDev/projects/xxx で作業しろ",
              "u2": SOURCE}


def _raw(**overrides):
    base = {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "workspace": None, "branch": None, "target_paths": [],
            "needs_repo": False, "rest_summary": None, "unmapped": []}
    base.update(overrides)
    return base


# ── validate_contract: branch / target_paths / unmapped / 出所束縛 ──

def test_branch_field_verbatim_and_needs_repo():
    """branch は発話に現れる名前のみ受理し、指定があれば needs_repo を強制 true にする。"""
    v, problems = contract_rules.validate_contract(
        _raw(branch={"text": "feature/design-implementation", "src": "u2"}),
        SOURCE, utterances=UTTERANCES, current_id="u2")
    assert problems == []
    assert v["branch"] == "feature/design-implementation"
    assert v["needs_repo"] is True


def test_branch_not_in_utterance_rejected():
    """原文に無いブランチ名の生成は契約不成立（逐語原則）。"""
    v, problems = contract_rules.validate_contract(
        _raw(branch="feature/invented-branch"), SOURCE)
    assert v is None
    assert any("branch" in p for p in problems)


def test_branch_unsafe_chars_rejected():
    """危険文字を含むブランチ名は不成立（SSH コマンド文字列に乗るため）。"""
    v, problems = contract_rules.validate_contract(
        _raw(branch="feat;rm -rf"), SOURCE + "\nfeat;rm -rf")
    assert v is None
    assert any("branch" in p for p in problems)


def test_target_paths_verbatim_dedup_and_limit():
    """target_paths は逐語のみ・重複除去・上限超過は不成立。"""
    v, problems = contract_rules.validate_contract(
        _raw(target_paths=[{"text": "docs/02-implementation-design.md", "src": "u2"},
                           "docs/02-implementation-design.md"]),
        SOURCE, utterances=UTTERANCES, current_id="u2")
    assert problems == []
    assert v["target_paths"] == ["docs/02-implementation-design.md"]

    v, problems = contract_rules.validate_contract(
        _raw(target_paths=["docs/no-such-file.md"]), SOURCE)
    assert v is None
    assert any("target_paths" in p for p in problems)

    too_many = [f"docs/{c}.md" for c in "abcdef"]
    v, problems = contract_rules.validate_contract(
        _raw(target_paths=too_many), SOURCE + "\n" + " ".join(too_many))
    assert v is None
    assert any("上限" in p for p in problems)


def test_unmapped_makes_contract_invalid_with_marker():
    """unmapped 非空は契約不成立で、問題文は "unmapped:" 前置き（機械判別可能）。"""
    v, problems = contract_rules.validate_contract(
        _raw(unmapped=["レビューは Rev 形式で進めろ"]), SOURCE)
    assert v is None
    assert len(problems) == 1
    assert problems[0].startswith("unmapped:")
    assert "Rev 形式" in problems[0]


def test_src_pointing_to_missing_utterance_rejected():
    """存在しない発話 id からの引用は不成立（出所束縛 (1)）。"""
    v, problems = contract_rules.validate_contract(
        _raw(directive={"text": "git push", "src": "u99"}),
        "git push", utterances=UTTERANCES, current_id="u2")
    assert v is None


def test_stale_binding_without_current_turn_citation_rejected():
    """発話由来フィールドが在るのに現在ターンを引用しない契約は不成立（stale 束縛）。

    2026-08-29 実障害の型: 現在の発話（u2 = 設計書レビュー）ではなく過去の発話
    （u1 = repo 指定）だけを引用した契約。
    """
    v, problems = contract_rules.validate_contract(
        _raw(constraints=[{"text": "repo:/Users/dev/DevDev/projects/xxx で作業しろ",
                           "forbid": False, "src": "u1"}]),
        "\n".join(UTTERANCES.values()), utterances=UTTERANCES, current_id="u2")
    assert v is None
    assert any("現在ターン" in p for p in problems)


def test_goal_type_contract_without_citations_passes():
    """発話由来フィールドが全て空の契約（目標型）は現在ターン規則を適用しない。"""
    v, problems = contract_rules.validate_contract(
        _raw(), "\n".join(UTTERANCES.values()),
        utterances=UTTERANCES, current_id="u2")
    assert problems == []
    assert v is not None


def test_plain_string_fields_remain_accepted():
    """旧形式（src なしの素の文字列）も受理する（会話全体照合への縮退）。"""
    v, problems = contract_rules.validate_contract(
        _raw(directive="mermaid のフロー図がレンダリングされないので直せ"),
        SOURCE, utterances=UTTERANCES, current_id="u2")
    assert problems == []
    assert v["directive"].startswith("mermaid")


def test_default_file_acceptance_prefers_target_paths():
    """既定 file 検査の第一入力は target_paths（散文パス検出は空のときの補助）。"""
    contract = {"directive": None, "acceptance": [],
                "target_paths": ["docs/02-implementation-design.md"]}
    out = contract_rules.apply_default_acceptance(contract, "docs/99-other.md を修正して")
    paths = [a["params"]["path"] for a in out["acceptance"] if a["kind"] == "file_changed"]
    assert paths == ["docs/02-implementation-design.md"]


# ── Contractor: unmapped はリトライしない ──

def test_contractor_unmapped_stops_without_retry(monkeypatch):
    """unmapped 検出はスキーマ閉包の正常動作 — リトライせず直ちに人へ（§8.10f）。"""
    calls = []
    output = json.dumps(_raw(unmapped=["独自の進め方の指定"]), ensure_ascii=False)

    def _runner(name, prompt):
        calls.append(name)
        return output

    def _no_local(*a, **k):
        raise AssertionError("ローカルを呼んではならない")
    monkeypatch.setattr(contractor_mod, "run_ollama", _no_local)

    c = contractor_mod.Contractor(
        {"ya-ta": {"model": "local-dummy", "llm_timeout_sec": 60},
         "sa-ru": {"ollama_host": "http://localhost:11434"}},
        escalate_runner=_runner)
    validated, prov = c.contract(
        "履歴", "要約", lambda parsed: contract_rules.validate_contract(parsed, SOURCE))
    assert validated is None
    assert calls == ["opus"]  # 1 回で停止（リトライなし）
    assert prov["unmapped"] == ["独自の進め方の指定"]


# ── GroundingVerifier: 測定の ref 化 ──

class _RefProbe:
    """(接頭辞, (rc, out)) 対応の偽 SSH probe。実行コマンド列を記録する。"""

    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        for prefix, result in self.responses:
            if prefix in command:
                return result
        return 0, ""


def test_file_check_uses_cat_file_on_other_branch():
    """branch 指定あり・HEAD 不一致の file 検査は cat-file で指定 ref を実測する。"""
    probe = _RefProbe([("rev-parse --is-inside-work-tree", (0, "true")),
                       ("rev-parse --abbrev-ref HEAD", (0, "main")),
                       ("cat-file -e", (0, ""))])
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/repo", [{"kind": "file", "params": {"path": "docs/02.md"}}],
        default_branch="feature/x")
    assert report.ok
    assert any("cat-file -e feature/x:docs/02.md" in c for c in probe.commands)
    assert not any("ls -la" in c for c in probe.commands)


def test_file_check_uses_worktree_when_head_matches():
    """HEAD が指定 branch と一致していれば従来どおり作業ツリー（ls）を測る。"""
    probe = _RefProbe([("rev-parse --is-inside-work-tree", (0, "true")),
                       ("rev-parse --abbrev-ref HEAD", (0, "feature/x")),
                       ("ls -la", (0, "-rw-r--r--"))])
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/repo", [{"kind": "file", "params": {"path": "docs/02.md"}}],
        default_branch="feature/x")
    assert report.ok
    assert any("ls -la" in c for c in probe.commands)


def test_pushed_defaults_to_contract_branch():
    """pushed の省略時既定値は契約の branch（HEAD ではなく当該 ref を比較する）。"""
    probe = _RefProbe([("rev-parse --is-inside-work-tree", (0, "true")),
                       ("rev-parse feature/x", (0, "abc123")),
                       ("ls-remote origin feature/x", (0, "abc123\trefs/heads/feature/x"))])
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/repo", [{"kind": "pushed", "params": {}}], default_branch="feature/x")
    assert report.ok
    assert any("rev-parse feature/x" in c for c in probe.commands)
    assert not any("rev-parse --abbrev-ref HEAD" in c for c in probe.commands)


# ── ConversationManager: マーカー優先・switch 機械付与・stale 失効 ──

class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None, contract_text=None):
        self.confirms.append({"exec_request_id": exec_request_id,
                              "summary": summary, "contract_text": contract_text})


def _manager(tmp_dir):
    sessions_dir = tempfile.mkdtemp(prefix="sessions-")
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp_dir},
        "conversation": {"sessions_dir": sessions_dir, "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work",
                         "worker_home": "/Users/dev"},
        "ya-ta": {"model": "dummy", "llm_timeout_sec": 60},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


class _FakeContractor:
    def __init__(self, parsed):
        self.parsed = parsed
        self.history_texts = []

    def contract(self, history_text, summary, validate, progress=None):
        self.history_texts.append(history_text)
        validated, _ = validate(self.parsed)
        return validated, {"origin": "opus", "backend": "worker_cli",
                           "degraded": False, "attempts": []}


def test_build_contract_marker_overrides_brain_branch():
    """`ブランチ:` マーカーの決定的抽出は契約化脳の提案より優先される（§8.10f）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr._append_turn("c1", "user", "ブランチ: feature/design-implementation で直せ")
    mgr.contractor = _FakeContractor(_raw())
    contract, _ = mgr._build_contract("c1", "修正する")
    assert contract["branch"] == "feature/design-implementation"


def test_build_contract_annotates_utterance_ids():
    """契約化プロンプトの履歴にはユーザー発話 id（[uN]）が注記される（出所束縛の入力）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr._append_turn("c1", "user", "最初の依頼")
    mgr._append_turn("c1", "assistant", "了解")
    mgr._append_turn("c1", "user", "続きの依頼")
    fake = _FakeContractor(_raw())
    mgr.contractor = fake
    mgr._build_contract("c1", "要約")
    text = fake.history_texts[0]
    assert "[u1] ユーザー: 最初の依頼" in text
    assert "[u2] ユーザー: 続きの依頼" in text


class _HeadProbeMgr:
    def __init__(self, head):
        self.head = head
        self.commands = []

    def run_ssh_probe(self, command, timeout):
        self.commands.append(command)
        if "rev-parse --abbrev-ref HEAD" in command:
            return 0, self.head
        return 0, ""


def test_ensure_branch_switch_prepends_on_mismatch():
    """契約 branch と HEAD の不一致で runbook 先頭に switch が機械付与される（§8.10g）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _HeadProbeMgr("feature/docs-requirements-v1")
    contract = {"branch": "feature/design-implementation",
                "runbook": [{"kind": "push", "params": {}}]}
    mgr._ensure_branch_switch(contract, "/repo")
    assert contract["runbook"][0] == {
        "kind": "switch", "params": {"branch": "feature/design-implementation"}}
    assert contract["runbook"][1]["kind"] == "push"


def test_ensure_branch_switch_noop_when_head_matches_or_exists():
    """HEAD 一致・既に同 branch への switch がある場合は付与しない（重複防止）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _HeadProbeMgr("feature/x")
    contract = {"branch": "feature/x", "runbook": []}
    mgr._ensure_branch_switch(contract, "/repo")
    assert contract["runbook"] == []

    mgr.process_mgr = _HeadProbeMgr("main")
    contract = {"branch": "feature/x",
                "runbook": [{"kind": "switch", "params": {"branch": "feature/x"}}]}
    mgr._ensure_branch_switch(contract, "/repo")
    assert len(contract["runbook"]) == 1


def test_present_summary_supersedes_stale_pendings():
    """新しい着手確認の提示は、同一会話面の既存 pending を superseded へ失効させる（§8.10b）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    stale_path = os.path.join(tmp, "stale.json")
    with open(stale_path, "w") as f:
        json.dump({"exec_request_id": "old1", "status": "pending",
                   "conversation_id": "T1:C1:111", "created_at": "2026-08-27T09:00:00",
                   "summary": "要件定義書のコミットとマージ", "plan": None,
                   "team_id": "T1", "channel_id": "C1", "user_id": "U1"}, f)
    msg = {"conversation_id": "T1:C1:222", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "222"}
    mgr._present_summary(msg, "mermaid 修正")
    with open(stale_path) as f:
        stale = json.load(f)
    assert stale["status"] == "superseded"
    # 新しい確認レコード自身は pending のまま
    assert len(mgr._pending_confirms(msg)) == 1


def test_directive_plan_includes_prepended_switch():
    """directive 型契約でも機械付与された switch（runbook）が計画の先頭に載り、
    逐語命令はその後に直列で続く（Layer3 自己レビュー検出の是正）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    mgr.process_mgr = _HeadProbeMgr("main")  # step_already_done: HEAD=main → switch 未済
    msg = {"conversation_id": "T1:C1:1", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "1"}
    contract = {"directive": "git push -u origin feature/x",
                "constraints": [], "acceptance": [],
                "runbook": [{"kind": "switch", "params": {"branch": "feature/x"}}],
                "branch": "feature/x", "needs_repo": True}
    mgr._present_summary(msg, "push する", workspace="/repo", contract=contract)
    plans = mgr._pending_confirms(msg)
    assert len(plans) == 1
    plan = plans[0][2]["plan"]
    assert plan[0]["execution"] == "runbook"
    assert "switch" in plan[0]["command"]
    assert plan[1]["command"] == "git push -u origin feature/x"
    assert plan[1]["depends_on"] == [1]  # runbook の後に直列


def test_supersede_covers_other_threads_same_channel():
    """同一チャンネル別スレッドの stale pending も失効する（同一会話面・§8.10b。
    現在スレッドに pending があっても別スレッドの stale を生き残らせない）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    for i, cid in ((1, "T1:C1:111"), (2, "T1:C1:222")):
        with open(os.path.join(tmp, f"p{i}.json"), "w") as f:
            json.dump({"exec_request_id": f"e{i}", "status": "pending",
                       "conversation_id": cid, "created_at": f"2026-08-2{i}T00:00:00",
                       "summary": f"S{i}", "plan": None,
                       "team_id": "T1", "channel_id": "C1", "user_id": "U1"}, f)
    # 新しい発話は e2 と同じスレッド（cid 一致の pending が存在する状態）
    msg = {"conversation_id": "T1:C1:222", "user_id": "U1",
           "team_id": "T1", "channel_id": "C1", "thread_ts": "222"}
    mgr._present_summary(msg, "新しい依頼")
    statuses = {}
    for name in os.listdir(tmp):
        if name.endswith(".json"):
            with open(os.path.join(tmp, name)) as f:
                r = json.load(f)
            statuses[r["exec_request_id"]] = r["status"]
    assert statuses["e1"] == "superseded"  # 別スレッドの stale も失効
    assert statuses["e2"] == "superseded"


# ── E2E 是正（2026-08-30）: file_changed（完了検査の空洞の是正） ──

def test_default_acceptance_uses_file_changed():
    """修正系依頼の既定検査は file_changed で立つ（実在のみの file では無作業でも完了）。"""
    contract = {"directive": None, "acceptance": [],
                "target_paths": ["docs/02-implementation-design.md"]}
    out = contract_rules.apply_default_acceptance(contract, "図を修正して")
    assert out["acceptance"] == [{"kind": "file_changed",
                                  "params": {"path": "docs/02-implementation-design.md"}}]
    assert "file_changed" in contract_rules.TRANSITION_KINDS  # reconcile で弁別力を持つ


class _BaselineProbeMgr:
    def __init__(self, head, responses):
        self.head = head
        self.responses = responses
        self.commands = []

    def run_ssh_probe(self, command, timeout):
        self.commands.append(command)
        if "rev-parse --abbrev-ref HEAD" in command:
            return 0, self.head
        for prefix, result in self.responses:
            if prefix in command:
                return result
        return 1, ""


def test_capture_baseline_worktree_and_missing():
    """既存ファイルは hash-object の blob id を baseline に刻み、不在は file（作成）へ確定。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _BaselineProbeMgr("main", [("hash-object docs/a.md", (0, "abc123")),
                                                ("hash-object docs/none.md", (1, ""))])
    contract = {"branch": None, "acceptance": [
        {"kind": "file_changed", "params": {"path": "docs/a.md"}},
        {"kind": "file_changed", "params": {"path": "docs/none.md"}}]}
    mgr._capture_file_baselines(contract, "/repo")
    assert contract["acceptance"][0]["params"]["baseline"] == "abc123"
    assert contract["acceptance"][1]["kind"] == "file"  # 不在＝作成の実測へ


def test_capture_baseline_uses_ref_when_branch_not_checked_out():
    """契約 branch が未 checkout なら当該 ref 上の blob id を baseline にする（ref 化）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _BaselineProbeMgr(
        "main", [("rev-parse feature/x:docs/a.md", (0, "blob999"))])
    contract = {"branch": "feature/x", "acceptance": [
        {"kind": "file_changed", "params": {"path": "docs/a.md"}}]}
    mgr._capture_file_baselines(contract, "/repo")
    assert contract["acceptance"][0]["params"]["baseline"] == "blob999"
    assert any("rev-parse feature/x:docs/a.md" in c for c in mgr.process_mgr.commands)


def test_capture_baseline_degrades_to_file_without_probe():
    """実測手段なし（process_mgr 未注入）は file へ縮退（従来動作・fail-closed 側）。"""
    mgr = _manager(tempfile.mkdtemp())
    contract = {"acceptance": [{"kind": "file_changed", "params": {"path": "a.md"}}]}
    mgr._capture_file_baselines(contract, "/repo")
    assert contract["acceptance"][0]["kind"] == "file"


def test_file_changed_verify_pass_and_fail():
    """出口検査: baseline と異なれば PASS・同一/取得不能は未達（無作業の完了を塞ぐ）。"""
    probe = _RefProbe([("hash-object", (0, "newhash"))])
    report = grounding.GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "file_changed",
                   "params": {"path": "docs/a.md", "baseline": "oldhash"}}])
    assert report.ok

    probe = _RefProbe([("hash-object", (0, "samehash"))])
    report = grounding.GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "file_changed",
                   "params": {"path": "docs/a.md", "baseline": "samehash"}}])
    assert not report.ok
    assert "実行前と同一" in report.note

    probe = _RefProbe([("hash-object", (1, ""))])
    report = grounding.GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "file_changed",
                   "params": {"path": "docs/a.md", "baseline": "oldhash"}}])
    assert not report.ok


# ── E2E 是正（2026-08-30）: 失敗証跡の伝搬 ──

def test_chain_failure_evidence_collects_exception_text():
    """runbook 前提不成立の証跡（例外本文）が失敗証跡として収集される。"""
    import asyncio
    import orchestrator as orch_pkg
    inst = orch_pkg.Orchestrator.__new__(orch_pkg.Orchestrator)
    loop = asyncio.new_event_loop()
    try:
        f1 = loop.create_future()
        f1.set_exception(orch_pkg.runbook_rules.RunbookError(
            "runbook_precondition:switch",
            "作業ツリーが clean でない（未コミット変更 4 件。退避・破棄はしません）\n"
            "$ git status --porcelain (rc=0)\nM docs/00-index.md"))
        f2 = loop.create_future()
        f2.set_result("ok")
        text = inst._chain_failure_evidence({1: f1, 2: f2})
    finally:
        loop.close()
    assert "Step 1:" in text
    assert "未コミット変更 4 件" in text
    assert "$ git status --porcelain (rc=0)" in text
    assert "Step 2" not in text


def test_format_contract_shows_branch_and_targets():
    """着手確認の提示に branch・対象文書が明示される（空欄も「なし」・§8.10f）。"""
    fmt = ConversationManager._format_contract
    text = fmt({"directive": None, "constraints": [], "acceptance": [],
                "branch": "feature/x", "target_paths": ["docs/a.md"]})
    assert "ブランチ: feature/x" in text
    assert "対象文書: docs/a.md" in text
    text = fmt({"directive": None, "constraints": [], "acceptance": []})
    assert "ブランチ: なし（HEAD のまま作業）" in text
    assert "対象文書: なし" in text


def test_build_contract_force_ready_exempts_current_turn_rule():
    """明示エスケープ（force_ready＝go 記法・/taka-ma-go）由来の契約化は現在ターン
    引用規則（出所束縛 (2)）を適用しない（§8.10f・2026-09-03 E2E 実測の是正:
    現在ターンが逐語 `go` のみだと規則が構造的に必ず不成立になる）。src 逐語照合
    （出所束縛 (1)）は維持される。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr._append_turn("c1", "user", "ブランチ: feature/x で docs/a.md を直せ")
    mgr._append_turn("c1", "assistant", "了解")
    mgr._append_turn("c1", "user", "go")  # 現在ターンは逐語 go のみ
    # 過去ターン（u1）だけを引用する契約 — 通常経路なら現在ターン引用欠落で不成立
    parsed = _raw(target_paths=[{"text": "docs/a.md", "src": "u1"}])
    mgr.contractor = _FakeContractor(parsed)
    contract, _ = mgr._build_contract("c1", "docs/a.md を直す")
    assert contract is None  # 通常経路: 現在ターン引用の欠落で不成立（規則維持）
    mgr.contractor = _FakeContractor(parsed)
    contract, _ = mgr._build_contract("c1", "docs/a.md を直す", force_ready=True)
    assert contract is not None  # force_ready: 規則 (2) 免除で成立
    assert contract["target_paths"] == ["docs/a.md"]
    # (1) の逐語照合は force_ready でも維持: 原文に無い引用は不成立のまま
    mgr.contractor = _FakeContractor(_raw(
        target_paths=[{"text": "docs/fabricated.md", "src": "u1"}]))
    contract, _ = mgr._build_contract("c1", "x", force_ready=True)
    assert contract is None
