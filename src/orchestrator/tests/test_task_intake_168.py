"""タスク受付の意図起票とブランチ束縛（設計書 §8.10h）。

2026-09-11 実測の抜けを固定する: 依頼文に「起票して task-<id> で作業しろ」と書き忘れた
依頼が main 直接作業のまま進み、review-verify（step0 がブランチ名と意図記録から意図を引く）
の対象外になった。受付が同じ 2 点を自動で用意し、用意できない変更系依頼は着手させない。

適用範囲の境界（回答型・読み取り系を止めない）と、fail-closed の範囲（変更系でブランチを
用意できない場合に限る）を、判定の実測入力を差し替えて固定する。

conversation.py は test_contract_binding_160.py と同方式でファイル直ロードする。
"""

import importlib.util
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import intake as intake_rules  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_168", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager

# `ledger.py start` の実出力 1 行目（ledger.py cmd_start の print と同形式）
LEDGER_OUT = ("新規起票: #168 検証系連携  ブランチ名: task-168  "
              "uuid: 60568a5926cd44aa9e04596cada267ed\n"
              "意図記録: /home/u/.claude/tools/conformance/projects/taka-ma/"
              "intents/168-60568a59.json（リポ外・worktree 削除耐性）")

LEDGER_PY = "/home/u/.claude/tools/conformance/ledger/ledger.py"


class _Run:
    """注入する実行手段。コマンドの部分一致で (rc, out) を返し、呼ばれた列を記録する。"""

    def __init__(self, rules):
        self.rules = rules          # [(部分一致, (rc, out)), ...] 先に一致したものを返す
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        for needle, result in self.rules:
            if needle in command:
                return result
        return 1, ""


def _run_ok(head="main", ledger=(0, LEDGER_OUT), branch_exists=1):
    return _Run([("rev-parse --is-inside-work-tree", (0, "true")),
                 ("rev-parse --abbrev-ref HEAD", (0, head)),
                 ("rev-parse --verify refs/heads/", (branch_exists, "")),
                 ("ledger.py start", ledger)])


def _contract(**overrides):
    base = {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "branch": None, "target_paths": [], "needs_repo": True,
            "rest_summary": "README を直す"}
    base.update(overrides)
    return base


def _bind(contract, run, workspace="/repo"):
    return intake_rules.bind_task_branch(
        run, workspace, contract, ledger_py=LEDGER_PY,
        python_bin="/usr/bin/python3", timeout=30, title="README を直す")


# ── 適用範囲（§8.10h。作業ツリーを変える約束だけを対象にする） ──

def test_needs_task_branch_only_for_worktree_kinds():
    """file 系・head_touches・diff_limit は対象。answered・ref 操作のみは対象外。"""
    assert intake_rules.needs_task_branch(
        _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a.md"}}]))
    assert intake_rules.needs_task_branch(
        _contract(acceptance=[{"kind": "answered", "params": {}},
                              {"kind": "file", "params": {"path": "a.md"}}]))
    assert not intake_rules.needs_task_branch(
        _contract(acceptance=[{"kind": "answered", "params": {"min_chars": 100}}]))
    assert not intake_rules.needs_task_branch(
        _contract(acceptance=[{"kind": "pushed", "params": {}},
                              {"kind": "branch_merged",
                               "params": {"source": "x", "target": "main"}}]))


def test_answer_type_is_not_blocked_at_intake():
    """回答型（作業ツリー不変が完了条件）は起票もブランチ作成もせず素通りする（R3/R4）。"""
    run = _run_ok()
    result = _bind(_contract(acceptance=[{"kind": "answered", "params": {}}]), run)
    assert result.status == "skip:not_mutating"
    assert not result.refused and result.branch is None
    assert run.commands == []          # 実測すら行わない（受付を重くしない）


def test_runbook_branch_step_blocks_double_decision():
    """計画が既にブランチを決めている契約には起票しない（決定点の二重化＝作業先と検査 ref の食い違い）。"""
    run = _run_ok()
    mutating = {"kind": "file_changed", "params": {"path": "a.md"}}
    for rb in ([{"kind": "switch", "params": {"branch": "feature/x"}}],
               [{"kind": "branch_create", "params": {"name": "feature/y", "base": "main"}}]):
        result = _bind(_contract(acceptance=[mutating], runbook=rb), run)
        assert result.status == "skip:runbook_branch", rb
        assert not result.refused and result.branch is None
    assert not any("ledger.py start" in c for c in run.commands)


def test_acceptance_catalog_is_partitioned():
    """完了条件カタログは「対象（作業ツリー変更）」と「対象外」に全数分割されている。

    新 kind がカタログへ増えたとき、どちらでもない未分類のまま黙って対象外に落ちるのを防ぐ
    （受付が新しい変更系の約束を素通りさせる経路を、カタログ側の追加で気づけるようにする）。
    """
    from orchestrator import contract as contract_rules
    non_worktree = {"answered", "pushed", "remote_file", "branch_merged"}
    assert set(contract_rules.ACCEPTANCE_KINDS) == set(intake_rules.WORKTREE_KINDS) | non_worktree


def test_explicit_branch_is_respected():
    """依頼者が明示した branch が最優先（起票しない・既存の switch 機械付与に委ねる）。"""
    run = _run_ok()
    result = _bind(_contract(branch="main",
                             acceptance=[{"kind": "file_changed",
                                          "params": {"path": "a.md"}}]), run)
    assert result.status == "skip:branch_specified"
    assert run.commands == []


def test_skip_when_no_workspace_or_not_git():
    """束縛対象の git が無い依頼（使い捨て作業場・非 git ディレクトリ）は止めない（R4）。"""
    mutating = _contract(acceptance=[{"kind": "file", "params": {"path": "a.md"}}])
    assert intake_rules.bind_task_branch(
        _run_ok(), None, mutating, ledger_py=LEDGER_PY, python_bin="/usr/bin/python3",
        timeout=30, title="t").status == "skip:no_workspace"
    assert intake_rules.bind_task_branch(
        None, "/repo", mutating, ledger_py=LEDGER_PY, python_bin="/usr/bin/python3",
        timeout=30, title="t").status == "skip:no_workspace"
    not_git = _Run([("rev-parse --is-inside-work-tree", (128, ""))])
    assert _bind(mutating, not_git).status == "skip:not_git"


# ── 起票とブランチ束縛（R2/R5） ──

def test_creates_ledger_entry_and_branch_create_step():
    """変更系で branch 未指定 → 起票し、基点を実測した branch_create を step として返す。"""
    run = _run_ok(head="main")
    result = _bind(_contract(directive="README を直せ",
                             constraints=[{"text": "1 行だけ", "forbid": False}],
                             acceptance=[{"kind": "file_changed",
                                          "params": {"path": "README.md"}}]), run)
    assert result.status == "created:ledger"
    assert result.branch == "task-168"
    assert result.step == {"kind": "branch_create",
                           "params": {"name": "task-168", "base": "main"}}
    start = [c for c in run.commands if "ledger.py start" in c]
    assert len(start) == 1
    # project 決定のため workspace へ cd してから呼ぶ。要件は契約フィールドの逐語
    assert start[0].startswith("cd /repo && ")
    assert "--title 'README を直す'" in start[0]
    assert "--requirement 'README を直せ'" in start[0]
    assert "--requirement '1 行だけ'" in start[0]


def test_existing_branch_becomes_switch_step():
    """台帳が返したブランチが既に在るなら作成せず switch（branch_create は前提不成立）。"""
    run = _run_ok(head="main", branch_exists=0)
    result = _bind(_contract(acceptance=[{"kind": "file", "params": {"path": "a.md"}}]),
                   run)
    assert result.status == "created:ledger"
    assert result.step == {"kind": "switch", "params": {"branch": "task-168"}}


def test_head_on_task_branch_is_reused_without_new_entry():
    """既にタスクブランチ上（スラッグ付きを含む）なら重複起票せず現 HEAD へ束縛する。"""
    run = _run_ok(head="task-168_verify-intake")
    result = _bind(_contract(acceptance=[{"kind": "file_changed",
                                          "params": {"path": "a.md"}}]), run)
    assert result.status == "bound:head_task_branch"
    assert result.branch == "task-168_verify-intake"
    assert result.step is None
    assert not any("ledger.py start" in c for c in run.commands)


def test_branch_id_regex_matches_ledger_rule():
    """タスクブランチ判定は台帳側の id 解決（ledger.py _BRANCH_ID_RE）と同一規則。"""
    for name in ("task-168", "task-168_verify-intake", "task-45-2", "wt/task-9"):
        assert intake_rules.TASK_BRANCH_RE.search(name), name
    for name in ("main", "feature/x", "task-168abc", "tasks-168"):
        assert not intake_rules.TASK_BRANCH_RE.search(name), name


# ── fail-closed（R2/R4。変更系でブランチを用意できない場合に限る） ──

def test_refuse_when_ledger_fails_or_output_unparsable():
    """起票が失敗・出力からブランチを引けない → 拒否（証跡つき）。"""
    mutating = _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a"}}])
    failed = _bind(mutating, _run_ok(ledger=(2, "ERROR: 新規起票には --title が必須")))
    assert failed.status == "refuse:ledger_failed" and failed.refused
    assert "ERROR" in failed.detail and "ledger.py start" in failed.detail

    unparsed = _bind(mutating, _run_ok(ledger=(0, "起票しました")))
    assert unparsed.status == "refuse:ledger_unparsed" and unparsed.branch is None


def test_refuse_when_head_unmeasurable():
    """現在位置が測れない（＝基点を決められない）変更系は着手させない。"""
    run = _Run([("rev-parse --is-inside-work-tree", (0, "true")),
                ("rev-parse --abbrev-ref HEAD", (255, "ssh: connect timed out"))])
    result = _bind(_contract(acceptance=[{"kind": "file", "params": {"path": "a"}}]), run)
    assert result.status == "refuse:head_unmeasurable"
    assert "ssh: connect timed out" in result.detail


def test_parse_ledger_branch():
    assert intake_rules.parse_ledger_branch(LEDGER_OUT) == "task-168"
    assert intake_rules.parse_ledger_branch("既に intent あり（上書きしない）") is None


# ── ConversationManager への配線（受付 → 契約の束縛 / 拒否） ──

class _FakeNotifier:
    def __init__(self):
        self.notes = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)


def _manager(intake_conf=True):
    tmp = tempfile.mkdtemp()
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp},
        "conversation": {"sessions_dir": tempfile.mkdtemp(prefix="sessions-"),
                         "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work", "worker_home": "/home/u"},
        "ya-ta": {"model": "dummy", "llm_timeout_sec": 60},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    if intake_conf:
        config["task_intake"] = {"ledger_py": LEDGER_PY,
                                 "python_bin": "/usr/bin/python3", "timeout_sec": 30}
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp)


class _ProbeMgr:
    def __init__(self, run):
        self.run = run

    def run_ssh_probe(self, command, timeout):
        return self.run(command, timeout)


def test_manager_binds_contract_and_prepends_step():
    """受付が契約へ branch を刻み、準備系 step を runbook 先頭へ置く（実行時に作成）。"""
    mgr = _manager()
    mgr.process_mgr = _ProbeMgr(_run_ok(head="main"))
    contract = _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a.md"}}],
                         runbook=[{"kind": "push", "params": {}}])
    assert mgr._ensure_task_intake(contract, "/repo", "README を直す") is None
    assert contract["branch"] == "task-168"
    assert contract["runbook"][0]["kind"] == "branch_create"
    assert contract["runbook"][1]["kind"] == "push"
    assert contract["_intake"] == "created:ledger"


def test_manager_returns_refusal_text_and_leaves_contract_unbound():
    """用意できない変更系は拒否文を返す（着手確認を出さない）。契約は束縛しない。"""
    mgr = _manager()
    mgr.process_mgr = _ProbeMgr(_run_ok(ledger=(2, "ERROR: 台帳が壊れている")))
    contract = _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a.md"}}])
    text = mgr._ensure_task_intake(contract, "/repo", "README を直す")
    assert text is not None
    assert "refuse:ledger_failed" in text and "ERROR: 台帳が壊れている" in text
    assert contract["branch"] is None and contract["runbook"] == []


def test_manager_refuses_when_probe_raises():
    """実行手段の例外（SSH 不達等）も「用意できない」として拒否に倒す（fail-closed）。"""
    class _Boom:
        def run_ssh_probe(self, command, timeout):
            raise OSError("ssh unreachable")

    mgr = _manager()
    mgr.process_mgr = _Boom()
    contract = _contract(acceptance=[{"kind": "file", "params": {"path": "a.md"}}])
    text = mgr._ensure_task_intake(contract, "/repo", "作る")
    assert text is not None and "refuse:exception" in text


def test_manager_noop_when_unconfigured():
    """`task_intake:` 未構成なら従来動作（段階導入。contract: と同じ規律）。"""
    mgr = _manager(intake_conf=False)
    mgr.process_mgr = _ProbeMgr(_run_ok())
    contract = _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a"}}])
    assert mgr._ensure_task_intake(contract, "/repo", "直す") is None
    assert contract["branch"] is None


def test_branch_switch_not_duplicated_after_intake():
    """受付が置いた branch_create の上に switch を重ねない（§8.10g の重複防止と整合）。"""
    mgr = _manager()
    mgr.process_mgr = _ProbeMgr(_run_ok(head="main"))
    contract = _contract(acceptance=[{"kind": "file_changed", "params": {"path": "a.md"}}])
    mgr._ensure_task_intake(contract, "/repo", "直す")
    mgr._ensure_branch_switch(contract, "/repo")
    assert [s["kind"] for s in contract["runbook"]] == ["branch_create"]


def test_baseline_uses_worktree_hash_for_pending_branch():
    """未作成の束縛ブランチでは作業ツリーの hash-object を baseline にする（file へ落とさない）。

    受付で切る予定のブランチは `rev-parse <branch>:<path>` で測れない。測れないことを理由に
    file（実在検査）へ縮退すると、修正依頼が無作業でも PASS する穴（2026-08-30 是正）が復活する。
    """
    mgr = _manager()
    run = _Run([("rev-parse --is-inside-work-tree", (0, "true")),
                ("rev-parse --abbrev-ref HEAD", (0, "main")),
                ("rev-parse --verify refs/heads/", (1, "")),
                ("ledger.py start", (0, LEDGER_OUT)),
                ("hash-object", (0, "abc123"))])
    mgr.process_mgr = _ProbeMgr(run)
    contract = _contract(acceptance=[{"kind": "file_changed",
                                      "params": {"path": "README.md"}}])
    mgr._ensure_task_intake(contract, "/repo", "直す")
    mgr._capture_file_baselines(contract, "/repo")
    assert contract["acceptance"][0]["kind"] == "file_changed"
    assert contract["acceptance"][0]["params"]["baseline"] == "abc123"
    assert any("hash-object README.md" in c for c in run.commands)
    assert not any("rev-parse task-168:README.md" in c for c in run.commands)


def test_confirm_text_states_the_operating_rule():
    """着手確認の契約テンプレートに束縛の出所を明記する（§8.10h 運用ルールの明示・R1）。"""
    text = ConversationManager._format_contract(
        {"branch": "task-168", "_intake": "created:ledger", "acceptance": [],
         "constraints": [], "rest_summary": None})
    assert "ブランチ: task-168（受付で台帳へ起票 — task-<id> 上で作業・§8.10h）" in text
