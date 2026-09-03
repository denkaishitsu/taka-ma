"""§8.10g「決定的実行と実測回答（LLM 裁量の構造撤去）」の検証。

2026-08-27 インシデント（worker の独断 stash・無実行ステップの成功宣言・虚偽状態回答・
同一計画の再提示ループ）の恒久対策を、失敗類型そのものに対応するコードレベルの検査で担保する:

- runbook: カタログ検証・コマンド組立・前提/事後の実測・修復（stash 等）の構造的不発生
- repo 状態の機械判定行: probe 実出力のパースのみで導出（LLM 不使用）
- 再入 reconcile: 完了条件の事前実測で「済んだ依頼」に計画を出さない
- 失敗原因コード: RunbookError の cause がそのまま failure_cause になる

conversation.py は test_exec_contract_149.py と同方式でファイル直ロードする。
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import contract as contract_rules  # noqa: E402
from orchestrator import plan as plan_rules  # noqa: E402
from orchestrator import runbook  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_158", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager


# ── runbook カタログ検証 ──

def test_validate_runbook_accepts_catalog_steps():
    steps, problems = runbook.validate_runbook([
        {"kind": "commit_paths",
         "params": {"paths": ["docs/a.md"], "message": "docs: update"}},
        {"kind": "push", "params": {}},
        {"kind": "merge_ff", "params": {"source": "feature/x", "target": "main"}},
    ])
    assert problems == []
    assert [s["kind"] for s in steps] == ["commit_paths", "push", "merge_ff"]


def test_validate_runbook_rejects_unknown_kind_and_bad_params():
    for raw in (
        [{"kind": "rebase", "params": {}}],                      # カタログ外の操作
        [{"kind": "switch", "params": {"branch": "a;rm -rf"}}],  # 危険文字
        [{"kind": "commit_paths",
          "params": {"paths": ["/etc/passwd"], "message": "x"}}],   # 絶対パス
        [{"kind": "commit_paths",
          "params": {"paths": ["../up.md"], "message": "x"}}],      # トラバーサル
        [{"kind": "commit_paths",
          "params": {"paths": ["a.md"], "message": "x\ny"}}],       # 改行入りメッセージ
        [{"kind": "push", "params": {"branch": "ok", "extra": "v"}}],  # 未知パラメータ
    ):
        steps, problems = runbook.validate_runbook(raw)
        assert steps is None and problems, raw


def test_validate_runbook_none_and_empty_are_empty_list():
    assert runbook.validate_runbook(None) == ([], [])
    assert runbook.validate_runbook([]) == ([], [])


def test_validate_runbook_caps_steps_and_paths():
    """巨大提案は拒否する（逐語提示が Slack 表示切り詰めに掛かり「見えていない列の承認」
    になるのを防ぐ・§8.10g 逐語承認原則）。"""
    many_steps = [{"kind": "push", "params": {}}] * (runbook._MAX_STEPS + 1)
    steps, problems = runbook.validate_runbook(many_steps)
    assert steps is None and any("上限" in p for p in problems)
    many_paths = [{"kind": "commit_paths",
                   "params": {"paths": [f"f{i}.md" for i in range(runbook._MAX_PATHS + 1)],
                              "message": "x"}}]
    steps, problems = runbook.validate_runbook(many_paths)
    assert steps is None and problems


# ── コマンド組立（提示と実行の SSOT） ──

def test_build_commands_assembly():
    ws = "/Users/u/DevDev/repo"
    assert runbook.build_commands(ws, "merge_ff",
                                  {"source": "feature/x", "target": "main"}) == [
        f"git -C {ws} switch main",
        f"git -C {ws} merge --ff-only feature/x",
    ]
    cmds = runbook.build_commands(
        ws, "commit_paths", {"paths": ["docs/a.md"], "message": "docs: update a"})
    assert cmds[0] == f"git -C {ws} add docs/a.md"
    assert cmds[1] == f"git -C {ws} commit -m 'docs: update a'"
    assert runbook.build_commands(ws, "push", {}) == [f"git -C {ws} push origin HEAD"]


# ── run_step: 前提不成立は修復せず失敗（stash が構造的に発生しない） ──

class _ScriptedGit:
    """substring 一致で応答を返す偽 SSH 実行（先勝ち）。呼び出しを全記録する。"""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, command, timeout=30):
        self.calls.append(command)
        for pattern, rc, out in self.responses:
            if pattern in command:
                return rc, out
        return 0, ""


def test_merge_ff_dirty_tree_fails_without_repair():
    """dirty tree での merge は stash せず前提不成立で止まる（2026-08-27 の stash 逸脱の対偶）。"""
    git = _ScriptedGit([
        ("is-inside-work-tree", 0, "true"),
        ("status --porcelain", 0, " M docs/00-index.md\n?? docs/02-basic-design.md"),
    ])
    with pytest.raises(runbook.RunbookError) as e:
        runbook.run_step(git, "/tmp/repo", "merge_ff",
                         {"source": "feature/x", "target": "main"})
    assert e.value.cause == "runbook_precondition:merge_ff"
    assert "未コミット変更" in str(e.value)
    # 修復・状態変更コマンドが 1 つも発行されていないこと（読み取り検査のみ）
    for cmd in git.calls:
        assert "stash" not in cmd and "merge" not in cmd and "switch" not in cmd


def test_merge_ff_non_ff_fails():
    git = _ScriptedGit([
        ("is-inside-work-tree", 0, "true"),
        ("status --porcelain", 0, ""),
        ("rev-parse --verify", 0, "ok"),
        ("merge-base --is-ancestor", 1, ""),
    ])
    with pytest.raises(runbook.RunbookError) as e:
        runbook.run_step(git, "/tmp/repo", "merge_ff",
                         {"source": "feature/x", "target": "main"})
    assert e.value.cause == "runbook_precondition:merge_ff"


class _CommitWorld:
    """commit_paths の前提→実行→事後を通す状態付き偽 git。"""

    def __init__(self):
        self.committed = False
        self.calls = []

    def __call__(self, command, timeout=30):
        self.calls.append(command)
        if "is-inside-work-tree" in command:
            return 0, "true"
        if "status --porcelain" in command:
            return 0, ("" if self.committed else " M docs/a.md")
        if " add " in command:
            return 0, ""
        if " commit " in command:
            self.committed = True
            return 0, "[main abc1234] docs: update"
        return 0, ""


def test_commit_paths_success_records_executed_commands():
    git = _CommitWorld()
    executed, text = runbook.run_step(
        git, "/tmp/repo", "commit_paths",
        {"paths": ["docs/a.md"], "message": "docs: update"})
    # 遵守照合（§8.10f）に載る実行記録は状態変更コマンドのみ
    assert executed == ["git -C /tmp/repo add docs/a.md",
                        "git -C /tmp/repo commit -m 'docs: update'"]
    assert "判定: runbook commit_paths 成功" in text


def test_commit_paths_without_changes_fails_precondition():
    """実変更なしのコミット指示は「成功」と宣言せず前提不成立で止まる（無実行成功の排除）。"""
    git = _ScriptedGit([("is-inside-work-tree", 0, "true"),
                        ("status --porcelain", 0, "")])
    with pytest.raises(runbook.RunbookError) as e:
        runbook.run_step(git, "/tmp/repo", "commit_paths",
                         {"paths": ["docs/a.md"], "message": "x"})
    assert e.value.cause == "runbook_precondition:commit_paths"


def test_push_postcondition_mismatch_fails():
    """push は remote 実出力の一致まで確認して初めて成功（§8.9 と同じ規律の実行版）。"""
    git = _ScriptedGit([
        ("is-inside-work-tree", 0, "true"),
        ("rev-parse --verify", 0, "ok"),
        ("push origin", 0, ""),
        ("ls-remote origin", 0, "bbbbbbbbbbbb\trefs/heads/main"),
        ("rev-parse main", 0, "aaaaaaaaaaaa"),
    ])
    with pytest.raises(runbook.RunbookError) as e:
        runbook.run_step(git, "/tmp/repo", "push", {"branch": "main"})
    assert e.value.cause == "runbook_failed:push"


# ── 契約への統合（§8.10g） ──

def test_validate_contract_accepts_runbook_and_forces_needs_repo():
    raw = {"directive": None, "constraints": [], "acceptance": [],
           "runbook": [{"kind": "push", "params": {}}],
           "workspace": None, "needs_repo": False, "rest_summary": None}
    validated, problems = contract_rules.validate_contract(raw, "push しておいて")
    assert problems == []
    assert validated["runbook"] == [{"kind": "push", "params": {}}]
    assert validated["needs_repo"] is True  # runbook がある契約は実リポジトリ必須


def test_validate_contract_migrates_runbook_kind_from_acceptance():
    """脳が runbook の kind を完了条件欄に置く誤り（2026-08-28 E2E で高頻度に実測）は、
    契約ごと弾かず runbook へ機械移送する（カタログ名の完全一致のみ・決定的）。"""
    raw = {"directive": None, "constraints": [],
           "acceptance": [{"kind": "merge_ff",
                           "params": {"source": "feature/x", "target": "main"}},
                          {"kind": "file", "params": {"path": "docs/note.md"}}],
           "runbook": [], "workspace": None, "needs_repo": True,
           "rest_summary": None}
    validated, problems = contract_rules.validate_contract(raw, "もう一回やって")
    assert problems == []
    assert [r["kind"] for r in validated["runbook"]] == ["merge_ff"]
    assert [a["kind"] for a in validated["acceptance"]] == ["file"]
    # 移送後も params 不正は従来どおり契約不成立（型エラーを覆い隠さない）
    raw_bad = {"directive": None, "constraints": [],
               "acceptance": [{"kind": "merge_ff", "params": {"source": "a;rm"}}],
               "runbook": [], "workspace": None, "needs_repo": True}
    validated, problems = contract_rules.validate_contract(raw_bad, "x")
    assert validated is None and problems


def test_validate_contract_rejects_bad_runbook():
    raw = {"directive": None, "constraints": [], "acceptance": [],
           "runbook": [{"kind": "rebase", "params": {}}],
           "workspace": None, "needs_repo": True}
    validated, problems = contract_rules.validate_contract(raw, "x")
    assert validated is None and any("runbook" in p or "kind" in p for p in problems)


def test_runbook_warning_only_when_git_words_without_runbook():
    assert contract_rules.runbook_warning(
        {"runbook": [], "directive": None}, "main へマージして push する") is not None
    assert contract_rules.runbook_warning(
        {"runbook": [{"kind": "push", "params": {}}], "directive": None},
        "main へマージして push する") is None
    assert contract_rules.runbook_warning(
        {"runbook": [], "directive": None}, "設計書をレビューする") is None


# ── repo 状態の機械判定行（§8.10g。LLM 不使用のパースのみ） ──

def test_derive_repo_verdicts():
    results = [
        ("git -C /r status --porcelain", 0, " M docs/00-index.md\n?? new.md"),
        ("git -C /r for-each-ref refs/heads --format='...'", 0,
         "feature/x abc123 \nmain def456 [ahead 2]"),
        ("git -C /r branch --format='%(refname:short)' --no-merged main", 0,
         "feature/docs-requirements-v1"),
        ("git -C /r stash list", 0, "stash@{0}: WIP on feature/x: ..."),
    ]
    verdicts = ConversationManager._derive_repo_verdicts(results)
    text = "\n".join(verdicts)
    assert "未コミット変更: 2 件（うち未追跡 1 件）" in text
    assert "main [ahead 2]" in text
    assert "main へ未マージのブランチ: feature/docs-requirements-v1" in text
    assert "stash 退避: 1 件" in text


def test_derive_repo_verdicts_skips_failed_probes():
    """取得不能（rc 非 0）の項目からは判定行を出さない（誤った断定をしない）。"""
    results = [("git -C /r branch --format='%(refname:short)' --no-merged main", 128,
                "fatal: malformed object name main")]
    assert ConversationManager._derive_repo_verdicts(results) == []


# ── 再入 reconcile（§8.10g。済んだ依頼に計画を出さない） ──

class _FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)


class _FakeProcMgr:
    """file 検査（ls -la）の成否をパス substring で決める偽 SSH。"""

    def __init__(self, present):
        self.present = present

    def run_ssh_probe(self, command, timeout=30):
        if "ls -la" in command:
            return (0, "-rw-r--r-- 1 u s 1 a") if any(
                p in command for p in self.present) else (1, "No such file")
        if "diff-tree" in command:
            # head_touches（状態遷移型）: present のパスを HEAD の変更として返す
            return 0, "\n".join(self.present)
        return 0, ""


def _reconcile_cm(present):
    cm = ConversationManager.__new__(ConversationManager)
    cm.process_mgr = _FakeProcMgr(present)
    cm.slack = _FakeNotifier()
    cm._appended = []
    cm._append_turn = lambda cid, role, text: cm._appended.append(text)
    cm._set_awaiting = lambda cid, awaiting: None
    return cm


def test_reconcile_all_pass_suppresses_plan():
    """状態遷移型検査（head_touches）を含む全 PASS は計画を出さず実測報告で止まる。"""
    cm = _reconcile_cm(present=["docs/a.md"])
    contract = {"acceptance": [
        {"kind": "file", "params": {"path": "docs/a.md"}},
        {"kind": "head_touches", "params": {"path": "docs/a.md"}},
    ]}
    out = cm._reconcile_acceptance("cid", contract, "/tmp/repo", {"conversation_id": "cid"})
    assert out is None  # 全 PASS → 計画・着手確認を出さない
    assert any("完了条件は既に満たされています（実測）" in n for n in cm.slack.notes)


def test_reconcile_existence_only_does_not_suppress():
    """存在型（file）のみの事前 PASS は弁別力なし＝実行拒否しない（§8.10g 弁別力規則。
    2026-08-29 実障害: 既存ファイルの不具合報告に対し file 事前 PASS を根拠に
    「完了条件は既に満たされています。実行は行いません」と実行を拒否した — の是正）。"""
    cm = _reconcile_cm(present=["docs/a.md"])
    contract = {"acceptance": [{"kind": "file", "params": {"path": "docs/a.md"}}]}
    out = cm._reconcile_acceptance("cid", contract, "/tmp/repo", {"conversation_id": "cid"})
    assert out == ["file path=docs/a.md"]  # 「済（実測）」注記に留め、着手確認へ進む
    assert cm.slack.notes == []


def test_reconcile_partial_pass_returns_satisfied_lines():
    cm = _reconcile_cm(present=["docs/a.md"])
    contract = {"acceptance": [
        {"kind": "file", "params": {"path": "docs/a.md"}},
        {"kind": "file", "params": {"path": "docs/b.md"}},
    ]}
    out = cm._reconcile_acceptance("cid", contract, "/tmp/repo", {"conversation_id": "cid"})
    assert out == ["file path=docs/a.md"]
    assert cm.slack.notes == []  # 部分 PASS は着手確認側で提示（ここでは通知しない）


def test_reconcile_without_acceptance_or_workspace_is_noop():
    cm = _reconcile_cm(present=[])
    assert cm._reconcile_acceptance("cid", {"acceptance": []}, "/tmp/repo", {}) == []
    assert cm._reconcile_acceptance(
        "cid", {"acceptance": [{"kind": "file", "params": {"path": "a"}}]}, None, {}) == []


def _handle_manager(tmp_dir):
    """handle_message を通すための実 ConversationManager（LLM 依存はモックで潰す）。"""
    import tempfile
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
    return conversation.ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


def _reconcile_msg(force):
    return {"conversation_id": "c1", "text": "repo:/Users/dev/r もう一度やって",
            "force_ready": force,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}


def _drive_reconcile(monkeypatch, force):
    """達成済み契約（reconcile が None を返す状態）で handle_message を最後まで通し、
    (reconcile 呼び出し回数, 着手確認提示回数) を返す。"""
    import tempfile
    mgr = _handle_manager(tempfile.mkdtemp())
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": "要約", "reply": ""})
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None, force_ready=False: ({
        "directive": None, "constraints": [],
        "acceptance": [{"kind": "file", "params": {"path": "a.md"}}],
        "runbook": [], "workspace": None, "needs_repo": False}, {}))
    monkeypatch.setattr(mgr, "_recheck_open_intents", lambda cid, msg: None)
    reconcile_calls = []
    monkeypatch.setattr(mgr, "_reconcile_acceptance",
                        lambda *a, **k: reconcile_calls.append(1) or None)
    presents = []
    monkeypatch.setattr(mgr, "_present_summary",
                        lambda *a, **k: presents.append(1))
    mgr.handle_message(_reconcile_msg(force))
    return len(reconcile_calls), len(presents)


def test_reconcile_gates_normal_but_force_ready_escapes(monkeypatch):
    """通常発話は事前実測（全 PASS なら提示なし）、/taka-ma-go（force_ready）は
    「達成済みでも再実行せよ」の明示エスケープとして reconcile を跳ばし提示へ進む（§8.10g）。"""
    assert _drive_reconcile(monkeypatch, force=False) == (1, 0)  # 実測して提示を抑止
    assert _drive_reconcile(monkeypatch, force=True) == (0, 1)   # 実測を跳ばして提示


# ── 完了条件 branch_merged（§8.10g。マージ完了の機械語彙） ──

def test_acceptance_branch_merged_pass_and_fail():
    from orchestrator.grounding import GroundingVerifier

    def _probe_factory(merged):
        def probe(command, timeout=30):
            if "is-inside-work-tree" in command:
                return 0, "true"
            if "merge-base --is-ancestor" in command:
                return (0, "") if merged else (1, "")
            return 0, ""
        return probe

    check = [{"kind": "branch_merged",
              "params": {"source": "feature/x", "target": "main"}}]
    assert GroundingVerifier(_probe_factory(True)).verify_acceptance(
        "/tmp/repo", check).ok
    report = GroundingVerifier(_probe_factory(False)).verify_acceptance(
        "/tmp/repo", check)
    assert not report.ok and report.cause == "acceptance_failed:branch_merged"


def test_validate_contract_accepts_branch_merged():
    raw = {"directive": None, "constraints": [],
           "acceptance": [{"kind": "branch_merged",
                           "params": {"source": "feature/x", "target": "main"}}],
           "runbook": [], "workspace": None, "needs_repo": False,
           "rest_summary": None}
    validated, problems = contract_rules.validate_contract(raw, "マージして")
    assert problems == []
    assert validated["acceptance"][0]["kind"] == "branch_merged"
    assert validated["needs_repo"] is True  # リポジトリ系検査は needs_repo を強制


# ── reconcile と runbook 未実施の関係（§8.10g。2026-08-28 E2E 実測の是正） ──

class _ReconcileWorld:
    """acceptance（ls -la）と runbook 済み判定（rev-parse 等）の両方に応える偽 SSH。"""

    def __init__(self, merged):
        self.merged = merged

    def run_ssh_probe(self, command, timeout=30):
        if "ls -la" in command:
            return 0, "-rw-r--r-- 1 u s 1 a"
        if "is-inside-work-tree" in command:
            return 0, "true"
        if "rev-parse feature/x" in command:
            return 0, "aaaa"
        if "rev-parse main" in command:
            return 0, ("aaaa" if self.merged else "bbbb")
        return 0, ""


def _reconcile_runbook_cm(merged):
    cm = ConversationManager.__new__(ConversationManager)
    cm.process_mgr = _ReconcileWorld(merged)
    cm.slack = _FakeNotifier()
    cm._append_turn = lambda cid, role, text: None
    cm._set_awaiting = lambda cid, awaiting: None
    return cm


def test_reconcile_not_suppressed_while_runbook_merge_remains():
    """完了条件が全 PASS でも、契約の runbook 操作（マージ）が未実施なら抑止しない
    （2026-08-28 実測: commit/push の検査だけ見てマージ未実施の依頼を『達成済み』と
    誤抑止した欠陥の再現と是正）。"""
    cm = _reconcile_runbook_cm(merged=False)
    contract = {"acceptance": [{"kind": "file", "params": {"path": "docs/a.md"}}],
                "runbook": [{"kind": "merge_ff",
                             "params": {"source": "feature/x", "target": "main"}}]}
    out = cm._reconcile_acceptance("cid", contract, "/tmp/repo",
                                   {"conversation_id": "cid"})
    assert out == ["file path=docs/a.md"]  # 済み分の表示に留め、計画提示へ進む
    assert cm.slack.notes == []


def test_reconcile_suppresses_when_runbook_also_done():
    cm = _reconcile_runbook_cm(merged=True)
    contract = {"acceptance": [{"kind": "file", "params": {"path": "docs/a.md"}}],
                "runbook": [{"kind": "merge_ff",
                             "params": {"source": "feature/x", "target": "main"}}]}
    out = cm._reconcile_acceptance("cid", contract, "/tmp/repo",
                                   {"conversation_id": "cid"})
    assert out is None  # 全て済み＝抑止（実測報告）
    assert any("完了条件は既に満たされています" in n for n in cm.slack.notes)


# ── runbook step の計画統合（§8.10g） ──

class _FakePlanService:
    def __init__(self, subtasks):
        self._subtasks = subtasks
        self.built_with = None

    def build(self, summary, progress=None):
        self.built_with = summary
        return [dict(s) for s in self._subtasks]


def test_merge_runbook_plan_orders_and_shifts_dependencies():
    cm = ConversationManager.__new__(ConversationManager)
    cm.process_mgr = None  # 済み判定なし（全 step を計画へ載せる縮退）
    cm.plan_service = _FakePlanService([
        {"step": 1, "command": "設計書を作成", "execution": "agent", "depth": "deep",
         "confidence": 0.9, "depends_on": []},
        {"step": 2, "command": "作成結果を確認", "execution": "agent", "depth": "shallow",
         "confidence": 0.9, "depends_on": [1]},
    ])
    plan, skipped = cm._merge_runbook_plan(
        [{"kind": "push", "params": {}},
         {"kind": "switch", "params": {"branch": "main"}}],
        "/tmp/repo", "push して main に切替えてから設計書を作成",
        # 分解入力は契約の rest_summary（§8.10g 必須キー化・確定要約フォールバック廃止）
        contract={"rest_summary": "設計書を作成"}, progress=None)
    assert skipped == []
    assert [s["step"] for s in plan] == [1, 2, 3, 4]
    # 残り作業あり（§8.10g 成果系の後置・2026-09-03 改訂）: 準備系 switch → agent 列 →
    # 成果系 push の順で直列化される（push を先行させると空 push になる）
    assert plan[0]["execution"] == "runbook" and plan[0]["_runbook"]["kind"] == "switch"
    assert plan[1]["execution"] == "agent" and plan[1]["depends_on"] == [1]
    assert plan[2]["execution"] == "agent" and plan[2]["depends_on"] == [2]
    assert plan[3]["execution"] == "runbook" and plan[3]["_runbook"]["kind"] == "push"
    assert plan[3]["command"] == "git -C /tmp/repo push origin HEAD"  # 組立後コマンドの逐語
    assert plan[3]["depends_on"] == [3]
    # 分解へは「runbook が実行する操作を含めない」注意が明示される（二重実行の防止）
    assert "サブタスクに含めない" in cm.plan_service.built_with


class _StateProbe:
    """済み判定用の偽 SSH（substring 一致・先勝ち）。"""

    def __init__(self, responses):
        self.responses = responses

    def run_ssh_probe(self, command, timeout=30):
        for pattern, rc, out in self.responses:
            if pattern in command:
                return rc, out
        return 0, ""


def test_merge_runbook_plan_skips_already_done_steps():
    """済んだ工程（commit/push 済）は計画に載せず残作業だけを提示する（§8.10g。
    2026-08-28 E2E 実測: 失敗後の「もう一回やって」に済み commit を再提案し前提不成立で
    再失敗する構造の是正）。"""
    cm = ConversationManager.__new__(ConversationManager)
    # commit: 対象パスに変更なし（済）／push: local==remote（済）／merge: 先端不一致（残）
    cm.process_mgr = _StateProbe([
        ("is-inside-work-tree", 0, "true"),
        ("status --porcelain", 0, ""),
        ("ls-remote origin", 0, "aaaa\trefs/heads/feature/x"),
        ("rev-parse feature/x", 0, "aaaa"),
        ("rev-parse main", 0, "bbbb"),
        ("rev-parse --abbrev-ref HEAD", 0, "feature/x"),
    ])
    cm.plan_service = _FakePlanService([])
    plan, skipped = cm._merge_runbook_plan(
        [{"kind": "commit_paths", "params": {"paths": ["docs/note.md"], "message": "x"}},
         {"kind": "push", "params": {"branch": "feature/x"}},
         {"kind": "merge_ff", "params": {"source": "feature/x", "target": "main"}}],
        "/tmp/repo", "もう一回やって", progress=None)
    # 残作業は merge_ff のみ・済み 2 件は「済（実測）」表示行として返る
    assert [s["_runbook"]["kind"] for s in plan] == ["merge_ff"]
    assert plan[0]["step"] == 1 and plan[0]["depends_on"] == []
    assert len(skipped) == 2
    assert any("コミット済み" in s for s in skipped)
    assert any("push 済み" in s for s in skipped)


def test_post_work_runbook_kept_and_ordered_after_agent_when_rest_exists():
    """「編集してコミット」型（残り作業あり）では、クリーンな作業ツリーでも成果系
    commit/push を「済み」と誤判定せず、agent step の後ろへ直列化する（§8.10g 成果系の
    後置・2026-09-03 E2E 実測の是正: 計画時のクリーン tree を「コミット済み」と誤判定して
    commit step が計画から欠落し、head_touches 未達で終わった）。"""
    cm = ConversationManager.__new__(ConversationManager)
    # 済み判定が走れば commit（変更なし）・push（先端一致）とも「済み」に見える状態を偽装
    cm.process_mgr = _StateProbe([
        ("is-inside-work-tree", 0, "true"),
        ("status --porcelain", 0, ""),
        ("ls-remote origin", 0, "aaaa\trefs/heads/feature/x"),
        ("rev-parse feature/x", 0, "aaaa"),
        ("rev-parse --abbrev-ref HEAD", 0, "feature/x"),
    ])
    cm.plan_service = _FakePlanService([
        {"step": 1, "command": "ファイルを追記", "execution": "agent", "depth": "shallow",
         "confidence": 0.9, "depends_on": []},
    ])
    plan, skipped = cm._merge_runbook_plan(
        [{"kind": "commit_paths", "params": {"paths": ["docs/a.md"], "message": "x"}},
         {"kind": "push", "params": {"branch": "feature/x"}}],
        "/tmp/repo", "追記してコミットして push",
        contract={"rest_summary": "docs/a.md へ追記する"}, progress=None)
    # 成果系は済みスキップされない（skipped 空）・agent → commit → push の順
    assert skipped == []
    assert [s.get("execution") for s in plan] == ["agent", "runbook", "runbook"]
    assert [s["_runbook"]["kind"] for s in plan[1:]] == ["commit_paths", "push"]
    assert plan[1]["depends_on"] == [1] and plan[2]["depends_on"] == [2]


def test_plan_view_runbook_step_bypasses_model_resolution():
    def _resolve(*args, **kwargs):
        raise AssertionError("runbook step でモデル写像が呼ばれてはならない")

    view = plan_rules.build_view(
        [{"step": 1, "command": "git -C /r push origin HEAD",
          "execution": "runbook", "depth": None, "confidence": 1.0, "depends_on": []}],
        _resolve)
    assert view[0]["weight"] == plan_rules.WEIGHT_RUNBOOK
    assert view[0]["model"] is None
    text = plan_rules.format_plan(view)
    assert "決定的実行" in text


# ── 失敗原因コード（§8.10g） ──

def test_failure_cause_from_runbook_error():
    from orchestrator import Orchestrator
    exc = runbook.RunbookError("runbook_precondition:merge_ff", "dirty")
    assert Orchestrator._failure_cause_from_error(exc) == "runbook_precondition:merge_ff"


def test_required_input_for_runbook_causes():
    assert "前提" in contract_rules.required_input_for("runbook_precondition:merge_ff")
    assert "失敗" in contract_rules.required_input_for("runbook_failed:push")
