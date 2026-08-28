"""契約化の ya-ta 移管と昇格ラダー（設計書 §8.4「契約化の呼び出し」/ §8.4.x (e) / §8.10f / §8.10g）。

検証する振る舞い（2026-08-28 E2E の契約化失敗 4 事例の構造的再発防止）:
- 失敗事例の再現: (1) 完了条件欄への runbook 操作名の記載（機械移送で契約成立を維持）、
  (2) commit_paths の message 欠落、(3) 必須 params の全欠落 — (2)(3) は機械検証で不成立
- 昇格ラダー: ローカルモデル 2 回不合格 → ladder の上位モデルで再契約化・同一検証。
  段の実行エラーは次段へ・全段失敗のみ None（fail-closed は人へ差し戻す従来動作のまま）
- 昇格チャネル: worker CLI の `<command> -p <model_flag>` SSH 単発（keychain 依存は拒否）
- rest_summary: null=agent 分解の省略 / 文字列=それだけを分解 / キー欠落=確定要約を分解（縮退）
- 提示: 着手確認に「残り作業」と昇格来歴の行が載る（見えていない縮約・脳は承認されない）

conversation.py は test_exec_contract_149.py と同方式でファイル直ロードする。
"""

import importlib.util
import json
import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from ai_gateway import contractor as contractor_mod  # noqa: E402
from orchestrator import contract as contract_rules  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_159", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager

SOURCE = ("repo:/Users/dev/DevDev/projects/xxx で作業しろ\n"
          "git push -u origin feature/docs をやれ")


def _raw(**overrides):
    base = {"directive": None, "constraints": [], "acceptance": [], "runbook": [],
            "workspace": None, "needs_repo": False, "rest_summary": None}
    base.update(overrides)
    return base


# ── 2026-08-28 E2E 失敗事例の再現（機械検証が検出することの固定） ──

def test_e2e_case_misplaced_runbook_kind_is_transferred():
    """事例 (1): 完了条件欄の merge_ff（runbook 操作名）は機械移送され契約が成立する。"""
    validated, problems = contract_rules.validate_contract(_raw(
        acceptance=[{"kind": "merge_ff",
                     "params": {"source": "feature/docs-update", "target": "main"}}]),
        SOURCE)
    assert problems == []
    assert validated["acceptance"] == []
    assert validated["runbook"] == [{"kind": "merge_ff",
                                     "params": {"source": "feature/docs-update",
                                                "target": "main"}}]


def test_e2e_case_commit_paths_missing_message_fails():
    """事例 (2): commit_paths の message 欠落は契約不成立（fail-closed）。"""
    validated, problems = contract_rules.validate_contract(_raw(
        runbook=[{"kind": "commit_paths", "params": {"paths": ["docs/overview.md"]}}]),
        SOURCE)
    assert validated is None
    assert any("commit_paths" in p and "message" in p for p in problems)


def test_e2e_case_all_params_missing_fails():
    """事例 (3): message・paths・source・target の全欠落は契約不成立（fail-closed）。"""
    validated, problems = contract_rules.validate_contract(_raw(
        runbook=[{"kind": "commit_paths", "params": {}},
                 {"kind": "merge_ff", "params": {}}]),
        SOURCE)
    assert validated is None
    assert any("commit_paths" in p for p in problems)
    assert any("merge_ff" in p for p in problems)


# ── Contractor（ya-ta）: ローカル試行と昇格ラダー ──

GOOD_OUTPUT = json.dumps(_raw(directive="git push -u origin feature/docs",
                              needs_repo=True), ensure_ascii=False)
# 事例 (2) をそのままローカルモデルの出力として使う（検証不合格の実物）
BAD_OUTPUT = json.dumps(_raw(
    runbook=[{"kind": "commit_paths", "params": {"paths": ["docs/overview.md"]}}]),
    ensure_ascii=False)


def _contractor_config(ladder=("haiku", "sonnet")):
    return {
        "ya-ta": {"model": "local-dummy", "llm_timeout_sec": 60},
        "sa-ru": {"ollama_host": "http://localhost:11434"},
        "routing": {"escalation": {"ladder": list(ladder)}},
    }


def _validate(parsed):
    return contract_rules.validate_contract(parsed, SOURCE)


def test_local_failure_escalates_to_ladder(monkeypatch):
    """ローカル 2 回不合格 → ladder 先頭（haiku）で再契約化・同一検証で成立。"""
    local_calls = []
    monkeypatch.setattr(contractor_mod, "run_ollama",
                        lambda *a, **k: local_calls.append(a) or BAD_OUTPUT)
    runner_calls = []
    c = contractor_mod.Contractor(
        _contractor_config(),
        escalate_runner=lambda name, prompt: runner_calls.append((name, prompt))
        or GOOD_OUTPUT)
    validated, prov = c.contract("履歴", "要約する", _validate)
    assert validated["directive"] == "git push -u origin feature/docs"
    assert len(local_calls) == 2
    assert [name for name, _ in runner_calls] == ["haiku"]
    assert "要約する" in runner_calls[0][1]  # 同一プロンプト（確定要約入り）で再契約化
    assert prov["origin"] == "haiku"
    assert prov["local_failures"] == 2
    assert [a["model"] for a in prov["attempts"]] == ["local-dummy", "local-dummy", "haiku"]


def test_local_success_skips_ladder(monkeypatch):
    """ローカル 1 回目で成立すれば昇格しない。"""
    monkeypatch.setattr(contractor_mod, "run_ollama", lambda *a, **k: GOOD_OUTPUT)

    def _never(name, prompt):
        raise AssertionError("昇格してはならない")

    validated, prov = contractor_mod.Contractor(
        _contractor_config(), escalate_runner=_never).contract("履歴", "要約", _validate)
    assert validated is not None
    assert prov["origin"] == "local"
    assert prov["local_failures"] == 0


def test_rung_execution_error_advances_to_next(monkeypatch):
    """段の実行エラー（SSH/CLI）は検証不合格と同列にその段の失敗とし、次段へ進む。"""
    monkeypatch.setattr(contractor_mod, "run_ollama", lambda *a, **k: BAD_OUTPUT)

    def _runner(name, prompt):
        if name == "haiku":
            raise RuntimeError("SSH command failed")
        return GOOD_OUTPUT

    validated, prov = contractor_mod.Contractor(
        _contractor_config(), escalate_runner=_runner).contract("履歴", "要約", _validate)
    assert validated is not None
    assert prov["origin"] == "sonnet"
    assert prov["attempts"][2]["problems"][0].startswith("実行失敗")


def test_all_rungs_fail_returns_none(monkeypatch):
    """全段失敗 → (None, origin=None)。呼び出し側の fail-closed（人へ）に乗る。"""
    monkeypatch.setattr(contractor_mod, "run_ollama", lambda *a, **k: BAD_OUTPUT)
    validated, prov = contractor_mod.Contractor(
        _contractor_config(), escalate_runner=lambda n, p: BAD_OUTPUT
    ).contract("履歴", "要約", _validate)
    assert validated is None
    assert prov["origin"] is None
    assert len(prov["attempts"]) == 4  # ローカル 2 + haiku + sonnet


def test_no_runner_means_no_escalation(monkeypatch):
    """escalate_runner 未注入（段階導入）はローカル失敗で確定する。"""
    monkeypatch.setattr(contractor_mod, "run_ollama", lambda *a, **k: BAD_OUTPUT)
    validated, prov = contractor_mod.Contractor(
        _contractor_config()).contract("履歴", "要約", _validate)
    assert validated is None
    assert len(prov["attempts"]) == 2


# ── ConversationManager: 昇格ランナーのコマンド組立と来歴の伝搬 ──

class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None, contract_text=None):
        self.confirms.append({"summary": summary, "contract_text": contract_text})


class _FakeSSH:
    def __init__(self):
        self.calls = []

    def run_ssh_command(self, command, timeout=120, stdin_text=None):
        self.calls.append({"command": command, "timeout": timeout,
                           "stdin_text": stdin_text})
        return GOOD_OUTPUT


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
        "models": {"haiku": {"command": "claude", "model_flag": "--model haiku"},
                   "gemini": {"command": "agy", "model_flag": "-p",
                              "keychain_auth": True}},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


def test_escalation_runner_builds_print_mode_command():
    """昇格段は `<command> -p <model_flag>` の SSH 単発・プロンプトは stdin（§8.4.x (e)）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _FakeSSH()
    out = mgr._contract_escalation_runner("haiku", "PROMPT")
    assert out == GOOD_OUTPUT
    call = mgr.process_mgr.calls[0]
    assert call["command"] == "claude -p --model haiku"
    assert call["stdin_text"] == "PROMPT"
    assert call["timeout"] == 60  # ya-ta.llm_timeout_sec を共用（新キーなし）


def test_escalation_runner_normalizes_ssh_timeout():
    """SSH ハング（TimeoutExpired）は RuntimeError へ正規化＝段の失敗として次段へ進める
    （素通しすると Contractor の捕捉外で会話処理全体が落ちる）。"""
    import subprocess

    mgr = _manager(tempfile.mkdtemp())

    class _HangSSH:
        def run_ssh_command(self, command, timeout=120, stdin_text=None):
            raise subprocess.TimeoutExpired(cmd=command, timeout=timeout)

    mgr.process_mgr = _HangSSH()
    with pytest.raises(RuntimeError, match="タイムアウト"):
        mgr._contract_escalation_runner("haiku", "PROMPT")


def test_escalation_runner_rejects_keychain_and_unknown_models():
    """keychain 依存 CLI・未登録モデルは例外（Contractor が次段へ進める）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _FakeSSH()
    with pytest.raises(RuntimeError):
        mgr._contract_escalation_runner("gemini", "PROMPT")
    with pytest.raises(RuntimeError):
        mgr._contract_escalation_runner("nonexistent", "PROMPT")
    with pytest.raises(RuntimeError):
        mgr.process_mgr = None
        mgr._contract_escalation_runner("haiku", "PROMPT")


class _FakeContractor:
    """Contractor の代役。検証関数が sa-ru から注入されることも同時に確かめる。"""

    def __init__(self, parsed, provenance):
        self.parsed = parsed
        self.provenance = provenance

    def contract(self, history_text, summary, validate, progress=None):
        validated, _ = validate(self.parsed)
        return validated, self.provenance


def test_build_contract_records_escalation_origin():
    """昇格で確定した契約は _contract_origin（来歴）を持つ。ローカル成立は持たない。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.contractor = _FakeContractor(
        _raw(), {"origin": "haiku", "local_failures": 2, "attempts": []})
    contract = mgr._build_contract("c1", "要約")
    assert contract["_contract_origin"] == {"model": "haiku", "local_failures": 2}

    mgr.contractor = _FakeContractor(
        _raw(), {"origin": "local", "local_failures": 0, "attempts": []})
    contract = mgr._build_contract("c2", "要約")
    assert "_contract_origin" not in contract


def test_build_contract_fail_closed_on_none():
    """Contractor が全段失敗（None）なら契約も None（呼び出し側が人へ差し戻す）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.contractor = _FakeContractor(
        {"runbook": "not-a-list"}, {"origin": None, "local_failures": 2, "attempts": []})
    assert mgr._build_contract("c1", "要約") is None


# ── rest_summary: 検証・分解入力・提示 ──

def test_validate_rest_summary_null_and_string_and_invalid():
    """null は明示として保持・文字列は strip・型不正はキーごと落とす（縮退・契約は成立）。"""
    v, _ = contract_rules.validate_contract(_raw(rest_summary=None), SOURCE)
    assert v["rest_summary"] is None
    v, _ = contract_rules.validate_contract(_raw(rest_summary=" README を追記 "), SOURCE)
    assert v["rest_summary"] == "README を追記"
    v, problems = contract_rules.validate_contract(_raw(rest_summary=123), SOURCE)
    assert problems == []
    assert "rest_summary" not in v
    raw = _raw()
    del raw["rest_summary"]
    v, _ = contract_rules.validate_contract(raw, SOURCE)
    assert "rest_summary" not in v


def _plan_capture(mgr):
    calls = []

    def fake_build_plan(text, progress=None):
        calls.append(text)
        return [{"step": 1, "command": "編集", "execution": "agent", "depth": None,
                 "confidence": 1.0, "depends_on": []}]

    mgr._build_plan = fake_build_plan
    return calls


def test_merge_runbook_plan_skips_decompose_when_rest_null():
    """rest_summary=null → agent 分解を省略し、計画は runbook step のみ（§8.10g）。"""
    mgr = _manager(tempfile.mkdtemp())
    calls = _plan_capture(mgr)
    plan, skipped = mgr._merge_runbook_plan(
        [{"kind": "push", "params": {}}], "/Users/dev/r", "要約",
        contract={"rest_summary": None})
    assert calls == []
    assert [s["execution"] for s in plan] == ["runbook"]


def test_merge_runbook_plan_decomposes_rest_summary_only():
    """rest_summary=文字列 → 確定要約ではなくその要約だけを分解入力にする。"""
    mgr = _manager(tempfile.mkdtemp())
    calls = _plan_capture(mgr)
    plan, _ = mgr._merge_runbook_plan(
        [{"kind": "push", "params": {}}], "/Users/dev/r", "要約全体",
        contract={"rest_summary": "README を追記する"})
    assert len(calls) == 1
    assert calls[0].startswith("README を追記する")
    assert "要約全体" not in calls[0]
    assert "サブタスクに含めないこと" in calls[0]  # 注意書きは多層として維持
    assert [s["execution"] for s in plan] == ["runbook", "agent"]
    assert plan[1]["depends_on"] == [1]  # runbook の後へ直列化


def test_merge_runbook_plan_falls_back_without_rest_key():
    """rest_summary キー欠落（旧出力・検証縮退）→ 従来どおり確定要約を分解する。"""
    mgr = _manager(tempfile.mkdtemp())
    calls = _plan_capture(mgr)
    mgr._merge_runbook_plan(
        [{"kind": "push", "params": {}}], "/Users/dev/r", "要約全体", contract={})
    assert len(calls) == 1
    assert calls[0].startswith("要約全体")


# ── 直近タスクの帰結の実測回答（終端記録＋回答時再検査・§8.10g） ──
# 2026-08-28 E2E FAIL の再現: マージ完了 30 秒後の「マージは終わったか」に
# 「実行中 0 件」しか答えられなかった（終端記録への読み経路の欠落）

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


class _ProbeMgr:
    def __init__(self, responses):
        self.run_ssh_probe = _FakeProbe(responses)


def _write_terminal_record(mgr, cid, tid, **overrides):
    record = {"task_id": tid, "conversation_id": cid, "status": "completed",
              "updated_at": "2026-08-28T16:10:44", "workspace": "/repo",
              "acceptance": [{"kind": "file", "params": {"path": "docs/note.md"}}],
              "result": "【runbook merge_ff】成功"}
    record.update(overrides)
    done = os.path.join(mgr.task_dir, "done", "2026-08-28")
    os.makedirs(done, exist_ok=True)
    with open(os.path.join(done, f"20260828161038_{tid}.json"), "w") as f:
        json.dump(record, f)
    mgr._last_task_id[cid] = tid


def test_terminal_record_answered_with_recheck_pass():
    """実行中 0 件でも終端記録から帰結を答え、完了条件をいま再実測して併記する。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _ProbeMgr([("rev-parse --is-inside-work-tree", (0, "true")),
                                 ("ls -la", (0, "-rw-r--r-- docs/note.md"))])
    _write_terminal_record(mgr, "c1", "t1")
    text = mgr._task_status_text({"conversation_id": "c1"})
    assert "直近タスク（終端記録）: 完了（2026-08-28 16:10）" in text
    assert "いま再検査: file path=docs/note.md → PASS" in text


def test_terminal_record_recheck_fail_prefers_current_measurement():
    """記録が完了でも再検査 FAIL なら「現在の実測は未達」を明示する（実測優先）。"""
    mgr = _manager(tempfile.mkdtemp())
    mgr.process_mgr = _ProbeMgr([("rev-parse --is-inside-work-tree", (0, "true")),
                                 ("ls -la", (1, "No such file"))])
    _write_terminal_record(mgr, "c1", "t1")
    text = mgr._task_status_text({"conversation_id": "c1"})
    assert "いま再検査: file path=docs/note.md → FAIL" in text
    assert "現在の実測は未達" in text


def test_terminal_record_pure_generation_shows_result_only():
    """完了検査の無いタスク（純生成）は終端記録の実在と結果冒頭が実測の上限。"""
    mgr = _manager(tempfile.mkdtemp())
    _write_terminal_record(mgr, "c1", "t1", acceptance=[],
                           result="要約: 本文の要点は 3 つ。\n詳細…")
    text = mgr._task_status_text({"conversation_id": "c1"})
    assert "結果（記録・冒頭）: 要約: 本文の要点は 3 つ。" in text
    assert "再検査" not in text


def test_no_terminal_record_keeps_plain_answer():
    """終端記録が無ければ従来どおり実行系タスク 0 件の実測のみ。"""
    mgr = _manager(tempfile.mkdtemp())
    text = mgr._task_status_text({"conversation_id": "c1"})
    assert text.startswith("タスクは走っていません")
    assert "直近タスク" not in text


def test_terminal_record_without_probe_says_record_only():
    """再検査手段が無いときは「記録時点の測定」であることを明示する（断言しない）。"""
    mgr = _manager(tempfile.mkdtemp())
    _write_terminal_record(mgr, "c1", "t1")
    text = mgr._task_status_text({"conversation_id": "c1"})
    assert "記録時点の測定" in text


def test_format_contract_shows_rest_summary_and_origin():
    """着手確認の契約提示に「残り作業」と昇格来歴が載る（§8.10f / §8.4.x (e) 可視化）。"""
    fmt = ConversationManager._format_contract
    base = {"directive": None, "constraints": [], "acceptance": [],
            "runbook": [{"kind": "push", "params": {}}]}
    assert "残り作業（分解対象）: なし（runbook で完結）" in fmt(
        {**base, "rest_summary": None})
    assert "残り作業（分解対象）: README を追記する" in fmt(
        {**base, "rest_summary": "README を追記する"})
    assert "残り作業（分解対象）: 未特定（確定要約の全体を分解）" in fmt(base)
    text = fmt({**base, "rest_summary": None,
                "_contract_origin": {"model": "haiku", "local_failures": 2}})
    assert "契約化: ローカル検証不合格 2 回 → haiku（昇格）" in text
