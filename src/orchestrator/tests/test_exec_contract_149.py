"""会話⇄実行の受け渡し契約（設計書 §8.10f）の検証。

検証する振る舞い（2026-08-22 インシデントの構造的再発防止）:
- 契約の検証: directive / constraints は発話の逐語引用のみ受理（原文に無い命令を生成させない）
- 完了条件の検査: 固定カタログのコマンドを実行し、全 PASS のときだけ ok（判定は rc・実出力のみ）
- 逐語命令: 分解せず原文 1 件のプランになり、遵守照合が命令 ⇄ 実行コマンドを機械照合する
- fail-closed: 実リポジトリを要するのに workspace 未解決なら着手確認を出さない
- 反復停止: 同一原因コード 2 回連続で再計画を提示しない（/taka-ma-go は明示続行）
- 依頼の寿命: intent レコードは完了条件 PASS まで open（宣言では閉じない）
- 禁止型拘束: decide デーモンがタスク別 deny 規則で Bash コマンドを実行前に拒否する

conversation.py は test_repo_wiring_143.py と同方式でファイル直ロードする。
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import contract as contract_rules  # noqa: E402
from orchestrator import intent_store  # noqa: E402
from orchestrator.grounding import GroundingVerifier  # noqa: E402
from orchestrator.headless_runner import WorkerHeadlessRunner  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_149", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager

SOURCE = ("repo:/Users/dev/DevDev/projects/xxx で作業しろ\n"
          "git push -u origin feature/docs をやれ\n"
          "鍵の再登録はするな")


# ── contract_rules.validate_contract ──

def test_validate_accepts_verbatim_directive():
    validated, problems = contract_rules.validate_contract(
        {"directive": "git push -u origin feature/docs", "constraints": [],
         "acceptance": [], "workspace": None, "needs_repo": False}, SOURCE)
    assert problems == []
    assert validated["directive"] == "git push -u origin feature/docs"
    # directive に git コマンド → needs_repo は機械補助で強制 true
    assert validated["needs_repo"] is True


def test_validate_accepts_verbatim_directive_regardless_of_language():
    """機械が判定するのは逐語性のみ。逐語引用なら言語・形は問わない（命令かどうかの
    最終判断は着手確認で人間が行う・§8.10f）。"""
    source = "docs を feature ブランチへ push しろ"
    validated, problems = contract_rules.validate_contract(
        {"directive": "docs を feature ブランチへ push しろ",
         "constraints": [], "acceptance": []}, source)
    assert problems == []
    assert validated["directive"] == "docs を feature ブランチへ push しろ"


def test_validate_rejects_fabricated_directive():
    """原文に無い命令（LLM の創作）は契約全体を不成立にする（fail-closed）。"""
    validated, problems = contract_rules.validate_contract(
        {"directive": "rm -rf /tmp/x", "constraints": [], "acceptance": []}, SOURCE)
    assert validated is None
    assert any("逐語引用" in p for p in problems)


def test_validate_rejects_fabricated_constraint():
    validated, problems = contract_rules.validate_contract(
        {"directive": None, "acceptance": [],
         "constraints": [{"text": "本番環境に触るな", "forbid": True}]}, SOURCE)
    assert validated is None


def test_validate_constraint_patterns_min_length():
    """短すぎる deny パターン（無差別ブロックの危険）は捨てられる。"""
    validated, problems = contract_rules.validate_contract(
        {"directive": None, "acceptance": [],
         "constraints": [{"text": "鍵の再登録はするな", "forbid": True,
                          "patterns": ["ssh-keygen", "git"]}]}, SOURCE)
    assert problems == []
    assert validated["constraints"][0]["patterns"] == ["ssh-keygen"]


def test_validate_rejects_unknown_acceptance_kind():
    validated, problems = contract_rules.validate_contract(
        {"directive": None, "constraints": [],
         "acceptance": [{"kind": "run_command", "params": {"cmd": "true"}}]}, SOURCE)
    assert validated is None


def test_validate_rejects_unsafe_acceptance_params():
    validated, problems = contract_rules.validate_contract(
        {"directive": None, "constraints": [],
         "acceptance": [{"kind": "file", "params": {"path": "a; rm -rf /"}}]}, SOURCE)
    assert validated is None


def test_validate_repo_kind_forces_needs_repo():
    validated, problems = contract_rules.validate_contract(
        {"directive": None, "constraints": [], "needs_repo": False,
         "acceptance": [{"kind": "remote_file",
                         "params": {"branch": "feature/docs", "path": "docs/01.md"}}]},
        SOURCE)
    assert problems == []
    assert validated["needs_repo"] is True


def test_default_acceptance_injected_for_push_requests():
    """push を含む依頼で acceptance 空なら既定検査（pushed）を自動付与（§8.10f 実装漏れの是正）。"""
    c = {"directive": None, "constraints": [], "acceptance": [],
         "workspace": None, "needs_repo": True}
    out = contract_rules.apply_default_acceptance(dict(c), "README を追記して push する")
    assert out["acceptance"] == [{"kind": "pushed", "params": {}}]
    # push を含まない依頼には付与しない（編集のみのタスクに pushed を課すと誤未達）
    out = contract_rules.apply_default_acceptance(dict(c), "README の誤字を直す")
    assert out["acceptance"] == []
    # 脳が立てた検査は上書きしない
    existing = {"directive": None, "constraints": [],
                "acceptance": [{"kind": "file", "params": {"path": "x"}}],
                "workspace": None, "needs_repo": True}
    out = contract_rules.apply_default_acceptance(existing, "push する")
    assert out["acceptance"] == [{"kind": "file", "params": {"path": "x"}}]


# ── 遵守照合・反復停止 ──

def test_compare_directive_match_and_mismatch():
    ok, text = contract_rules.compare_directive(
        "git push -u origin feature/docs",
        ["cd /repo && git push -u origin feature/docs"])
    assert ok and "一致" in text
    ok, text = contract_rules.compare_directive(
        "git push -u origin feature/docs", ["git status"])
    assert not ok and "不一致" in text


def test_is_repeated_cause():
    assert contract_rules.is_repeated_cause(
        ["workspace_not_repo", "workspace_not_repo"]) is True
    # 原因コードが違っても連続 2 回失敗なら停止（E2E 実測 2026-08-24: worker の勝手な
    # 回避＝git init で原因コードがズレ、同一コード基準では反復をすり抜けた）
    assert contract_rules.is_repeated_cause(
        ["workspace_not_repo", "acceptance_failed:pushed"]) is True
    assert contract_rules.is_repeated_cause(["workspace_not_repo"]) is False
    assert contract_rules.is_repeated_cause([]) is False


def test_required_input_covers_catalog_causes():
    assert "repo:" in contract_rules.required_input_for("workspace_not_repo")
    assert contract_rules.required_input_for("acceptance_failed:remote_file")


# ── GroundingVerifier.verify_acceptance（偽 probe・実行なし） ──

class _FakeProbe:
    """コマンド接頭辞 → (rc, output) の対応で SSH probe を偽装する。"""

    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        for prefix, result in self.responses:
            if prefix in command:
                return result
        return (0, "")


def test_acceptance_remote_file_pass():
    probe = _FakeProbe([
        ("rev-parse --is-inside-work-tree", (0, "true")),
        ("fetch origin", (0, "")),
        ("cat-file -e", (0, "")),
    ])
    report = GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "remote_file",
                   "params": {"branch": "feature/docs", "path": "docs/01.md"}}])
    assert report.ok is True
    assert report.cause is None


def test_acceptance_remote_file_fail_sets_cause():
    probe = _FakeProbe([
        ("rev-parse --is-inside-work-tree", (0, "true")),
        ("fetch origin", (0, "")),
        ("cat-file -e", (128, "")),
    ])
    report = GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "remote_file",
                   "params": {"branch": "feature/docs", "path": "docs/01.md"}}])
    assert report.ok is False
    assert report.cause == "acceptance_failed:remote_file"


def test_acceptance_not_a_repo_sets_workspace_cause():
    """8/22 インシデントの形: 空 workspace では repo 系検査が前提不成立で未達になる。"""
    probe = _FakeProbe([("rev-parse --is-inside-work-tree", (128, ""))])
    report = GroundingVerifier(probe).verify_acceptance(
        "/opt/taka-ma/work/x", [{"kind": "pushed", "params": {}}])
    assert report.ok is False
    assert report.cause == "workspace_not_repo"


def test_acceptance_pushed_mismatch():
    probe = _FakeProbe([
        ("rev-parse --is-inside-work-tree", (0, "true")),
        ("rev-parse --abbrev-ref HEAD", (0, "main")),
        ("rev-parse HEAD", (0, "aaaa1111")),
        ("ls-remote origin", (0, "bbbb2222\trefs/heads/main")),
    ])
    report = GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "pushed", "params": {}}])
    assert report.ok is False
    assert report.cause == "acceptance_failed:pushed"


def test_acceptance_invalid_param_fails_closed():
    """不正パラメータは検査 FAIL に倒し、コマンドには乗せない。"""
    probe = _FakeProbe([])
    report = GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "file", "params": {"path": "a;rm"}}])
    assert report.ok is False
    assert all("a;rm" not in c for c in probe.commands)


def test_acceptance_head_touches():
    probe = _FakeProbe([
        ("rev-parse --is-inside-work-tree", (0, "true")),
        ("diff-tree", (0, "docs/01.md\nREADME.md")),
    ])
    report = GroundingVerifier(probe).verify_acceptance(
        "/repo", [{"kind": "head_touches", "params": {"path": "docs/01.md"}}])
    assert report.ok is True


# ── intent_store（依頼の寿命） ──

def test_intent_lifecycle_open_until_pass():
    d = tempfile.mkdtemp(prefix="intents-")
    acceptance = [{"kind": "file", "params": {"path": "x"}}]
    intent_store.create(d, task_id="t-1", conversation_id="c1", summary="S",
                        acceptance=acceptance, workspace="/repo")
    record = intent_store.load(d, "t-1")
    assert record["goal_status"] == intent_store.GOAL_OPEN
    assert intent_store.list_open(d, "c1")[0]["task_id"] == "t-1"
    # 検査 PASS のときだけ achieved（宣言では閉じない — 呼び出し側の規律を API が支える）
    intent_store.set_goal_status(d, "t-1", intent_store.GOAL_ACHIEVED)
    assert intent_store.load(d, "t-1")["goal_status"] == intent_store.GOAL_ACHIEVED
    assert intent_store.list_open(d, "c1") == []


def test_intent_without_acceptance_is_closed_at_creation():
    d = tempfile.mkdtemp(prefix="intents-")
    intent_store.create(d, task_id="t-2", conversation_id="c1", summary="S",
                        acceptance=[], workspace="/repo")
    assert intent_store.load(d, "t-2")["goal_status"] == intent_store.GOAL_ACHIEVED


# ── ConversationManager（契約化パスのゲート群） ──

class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None, contract_text=None):
        self.confirms.append({"summary": summary, "workspace_text": workspace_text,
                              "contract_text": contract_text})


def _manager(tmp_dir, intents_dir=None):
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
        "contract": {"intents_dir": intents_dir or tempfile.mkdtemp(prefix="intents-")},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


def _msg(text, cid="c1", force=False):
    return {"conversation_id": cid, "text": text, "force_ready": force,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}


def _ready(mgr, monkeypatch, summary="要約"):
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": summary, "reply": ""})


def _records(tmp_dir):
    out = []
    for name in os.listdir(tmp_dir):
        if name.endswith(".json"):
            with open(os.path.join(tmp_dir, name)) as f:
                out.append(json.load(f))
    return out


def test_directive_contract_freezes_verbatim_plan(monkeypatch):
    """逐語命令は分解されず、原文 1 件のプランと契約フィールドが確認レコードへ載る。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": "git push -u origin feature/docs",
        "constraints": [{"text": "鍵の再登録はするな", "forbid": True,
                         "patterns": ["ssh-keygen"]}],
        "acceptance": [{"kind": "pushed", "params": {}}],
        "workspace": None, "needs_repo": True})

    mgr.handle_message(_msg("repo:/Users/dev/DevDev/projects/xxx git push -u origin feature/docs をやれ"))

    records = [r for r in _records(tmp) if r.get("exec_request_id")]
    assert len(records) == 1
    record = records[0]
    assert record["directive"] == "git push -u origin feature/docs"
    assert record["plan"] == [{"step": 1, "command": "git push -u origin feature/docs",
                               "execution": "agent", "depth": None,
                               "confidence": 1.0, "depends_on": []}]
    assert record["acceptance"] == [{"kind": "pushed", "params": {}}]
    assert "命令（逐語実行）" in mgr.slack.confirms[0]["contract_text"]

    # 確定タスクへ契約フィールドが運ばれ、intent レコードが open で作られる
    task_id = mgr.create_exec_task(record)
    tasks = [r for r in _records(tmp) if r.get("task_id")]
    assert tasks[0]["directive"] == "git push -u origin feature/docs"
    assert tasks[0]["acceptance"] == [{"kind": "pushed", "params": {}}]
    intent = intent_store.load(mgr.intents_dir, task_id)
    assert intent["goal_status"] == intent_store.GOAL_OPEN


def test_needs_repo_without_workspace_blocks_before_confirm(monkeypatch):
    """実リポジトリを要するのに場所未解決 → 着手確認を出さず質問（fail-closed・§8.10f）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": None, "constraints": [], "acceptance": [],
        "workspace": None, "needs_repo": True})

    mgr.handle_message(_msg("既存リポジトリのコードを直して"))

    assert mgr.slack.confirms == []
    assert any("未解決" in n for n in mgr.slack.notes)


def test_contract_failure_blocks_confirm(monkeypatch):
    """契約が確定できない依頼は実行へ進めない（fail-closed）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: None)

    mgr.handle_message(_msg("なにかやって"))

    assert mgr.slack.confirms == []
    assert any("実行契約を確定できませんでした" in n for n in mgr.slack.notes)


def test_contract_workspace_proposal_is_validated_and_used(monkeypatch):
    """契約化パスの workspace 提案（会話全文脈からの特定）が検証を経て採用される。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": None, "constraints": [], "acceptance": [],
        "workspace": "~/DevDev/projects/xxx", "needs_repo": True})

    mgr.handle_message(_msg("さっきのリポジトリの続きをやって"))

    assert len(mgr.slack.confirms) == 1
    record = [r for r in _records(tmp) if r.get("exec_request_id")][0]
    assert record["workspace"] == "/Users/dev/DevDev/projects/xxx"


def test_repeated_cause_stops_replanning(monkeypatch):
    """同一原因コード 2 回連続 → 再計画を提示せず必要な入力を平文報告（§8.10f）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": None, "constraints": [], "acceptance": [],
        "workspace": None, "needs_repo": False})
    task = {"conversation_id": "c1"}
    mgr.record_task_outcome(task, "workspace_not_repo")
    mgr.record_task_outcome(task, "workspace_not_repo")

    mgr.handle_message(_msg("もう一度やって"))

    assert mgr.slack.confirms == []
    assert any("workspace_not_repo" in n and "repo:" in n for n in mgr.slack.notes)

    # /taka-ma-go（force_ready）は明示続行としてゲートを通す
    mgr.handle_message(_msg("続行", force=True))
    assert len(mgr.slack.confirms) == 1


def test_repeated_cause_resets_on_new_workspace(monkeypatch):
    """場所の指定が変わったら反復カウントをリセットし、新指定での再試行を塞がない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _ready(mgr, monkeypatch)
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": None, "constraints": [], "acceptance": [],
        "workspace": None, "needs_repo": False})
    task = {"conversation_id": "c1"}
    mgr.record_task_outcome(task, "workspace_not_repo")
    mgr.record_task_outcome(task, "workspace_not_repo")

    mgr.handle_message(_msg("repo:/Users/dev/DevDev/projects/xxx でやり直して"))

    assert len(mgr.slack.confirms) == 1


def test_outcome_success_resets_causes():
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    task = {"conversation_id": "c1"}
    mgr.record_task_outcome(task, "worker_error")
    mgr.record_task_outcome(task, None)
    assert mgr._failure_causes.get("c1") is None


def test_extract_json_ignores_code_fence_inside_json_string():
    """JSON 文字列値の中のコードフェンスをフェンス優先が誤採取しない（E2E 実測 2026-08-24）。"""
    from ai_gateway.llm import extract_json
    resp = json.dumps({"reply": "着手します", "ready": True,
                       "summary": "手順:\n```bash\ngit push -u origin feature/x\n```\n以上",
                       "probe": None})
    assert json.loads(extract_json(resp))["ready"] is True


# ── 不正 JSON エスケープの機械修復（2026-08-24 E2E 実測の壊れ形） ──

def test_invoke_llm_repairs_invalid_backtick_escape(monkeypatch):
    """脳が markdown 癖で `\\`` を出力しても、修復パースで ready 判定まで到達する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    broken = ('{"reply": "着手します", "ready": true, '
              '"summary": "実行方法は \\`npm run test:e2e\\`\\. を追記", "probe": null}')
    monkeypatch.setattr(conversation, "run_ollama",
                        lambda *a, **k: broken)
    result = mgr._invoke_llm([{"role": "user", "text": "x"}], force=False)
    assert result["ready"] is True
    assert "npm run test:e2e" in result["summary"]


# ── headless の実行コマンド採取（遵守照合の材料） ──

class _FakeStdout:
    def __init__(self, lines):
        self.lines = [line.encode() for line in lines]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.lines:
            raise StopAsyncIteration
        return self.lines.pop(0)


class _FakeStderr:
    async def read(self):
        return b""


class _FakeProc:
    def __init__(self, lines):
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()


def test_consume_stream_collects_bash_commands():
    runner = WorkerHeadlessRunner("t-1-step1-opus")
    assistant = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "git push -u origin feature/docs"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        {"type": "text", "text": "done"},
    ]}})
    result_line = json.dumps({"type": "result", "result": "ok"})
    result = asyncio.run(runner._consume_stream(_FakeProc([assistant, result_line])))
    assert result.commands == ["git push -u origin feature/docs"]


# ── decide デーモンのタスク別 deny（禁止型拘束の機械強制） ──

def test_decide_daemon_task_deny(monkeypatch):
    sys.path.insert(0, os.path.abspath(os.path.join(_SRC, "approval-pipeline")))
    import decide_daemon
    from approval_types import PendingApproval

    deny_dir = tempfile.mkdtemp(prefix="task-deny-")
    monkeypatch.setattr(decide_daemon, "_TASK_DENY_DIR", deny_dir)
    with open(os.path.join(deny_dir, "t-1.json"), "w") as f:
        json.dump({"task_id": "t-1", "patterns": ["ssh-keygen"],
                   "sources": ["鍵の再登録はするな"]}, f)

    pending = PendingApproval(tool_name="Bash",
                              tool_input={"command": "ssh-keygen -t ed25519"},
                              tool_use_id="u1")
    reason = decide_daemon.DecideDaemon._task_deny_reason("t-1", pending)
    assert reason and "ssh-keygen" in reason

    # 規則に無いコマンド・規則の無いタスク・Bash 以外は deny しない（通常 Tier 判定へ）
    ok = PendingApproval(tool_name="Bash", tool_input={"command": "git status"},
                         tool_use_id="u2")
    assert decide_daemon.DecideDaemon._task_deny_reason("t-1", ok) is None
    assert decide_daemon.DecideDaemon._task_deny_reason("t-2", pending) is None
    read = PendingApproval(tool_name="Read", tool_input={"file_path": "/x"},
                           tool_use_id="u3")
    assert decide_daemon.DecideDaemon._task_deny_reason("t-1", read) is None
