"""出口ゲート — 完了報告前の独立検証段（設計書 §8.10f「出口ゲート」・#169）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
  - 遮断: 検証エージェントのプロンプトに worker の自己申告テキストが構造的に入らない
    （回答型依頼の回答本文だけが成果物として入る）
  - 総合判定の機械導出: LLM の合格宣言を採らず、全件 resolved かつ新規不整合ゼロのみ PASS
  - JSON 逸脱の 1 回リトライと fail-closed（exit_gate_unverified・差し戻しなし）
  - 差し戻しループ: FAIL で完了報告を出さず init 再投入・上限超過 / directive 型は ⚠ 未達
  - worker 指示への判定表の前置き（差し戻し後の再実行）
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import asyncio
import json
import types

from orchestrator import Orchestrator
from orchestrator.exit_gate import ExitGateReport, ExitGateVerifier, collect_check_paths

_TEMPLATE = ("根拠:\n{request_doc}\n\n証跡:\n{evidence}\n\n機械検査:\n{machine_checks}")

_PASS_JSON = json.dumps({
    "items": [{"req": "指摘Aの解消", "verdict": "resolved", "evidence": "docs/a.md:12"}],
    "new_issues": []}, ensure_ascii=False)

_FAIL_JSON = json.dumps({
    "items": [{"req": "指摘Aの解消", "verdict": "resolved", "evidence": "docs/a.md:12"},
              {"req": "指摘Bの解消", "verdict": "unresolved", "evidence": "証跡に実体なし"}],
    "new_issues": [{"issue": "図と表の件数不一致", "evidence": "docs/a.md:40"}]},
    ensure_ascii=False)


def _llm_factory(outputs: list[str], calls: list[str] | None = None):
    """出力列を順に返す run_llm の偽物。calls に受け取ったプロンプトを記録する。"""
    seq = list(outputs)

    def run_llm(prompt: str) -> str:
        if calls is not None:
            calls.append(prompt)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return run_llm


def _probe_recorder(commands: list[str]):
    def run_probe(command: str, timeout: int = 30):
        commands.append(command)
        return (0, "1\t内容")
    return run_probe


_TASK_BASE = {"task_id": "t1", "command": "指摘Aと指摘Bを解消せよ",
              "target_paths": ["docs/a.md"]}


# ── ExitGateVerifier 単体（判定の機械導出） ──

def test_all_resolved_and_no_issues_is_pass():
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([_PASS_JSON]), _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "機械検査証跡")
    assert report.ok and report.cause is None
    assert "resolved" in report.text and "独立検証" in report.text


def test_unresolved_or_new_issue_is_fail_with_exit_gate_failed():
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([_FAIL_JSON]), _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "")
    assert not report.ok and report.cause == "exit_gate_failed"
    assert "未解消/部分解消の要件 1 件" in report.note
    assert "新規不整合 1 件" in report.note
    # 差し戻し前置き（findings_text）は未解消と新規不整合のみを含む
    findings = report.findings_text()
    assert "指摘Bの解消" in findings and "図と表の件数不一致" in findings
    assert "指摘Aの解消" not in findings


def test_llm_pass_claim_is_not_trusted():
    # LLM が items に unresolved を残したまま「合格」を主張しても、総合判定はコード側の
    # 機械導出（全件 resolved かつ新規不整合ゼロ）で FAIL になる
    out = json.dumps({"pass": True, "items": [
        {"req": "指摘A", "verdict": "unresolved", "evidence": ""}], "new_issues": []})
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([out]), _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "")
    assert not report.ok and report.cause == "exit_gate_failed"


def test_json_deviation_retries_once_then_fails_closed():
    calls: list[str] = []
    v = ExitGateVerifier(_probe_recorder([]),
                         _llm_factory(["これは JSON ではありません", "まだ違います"], calls),
                         _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "")
    assert len(calls) == 2                       # 1 回だけリトライ
    assert not report.ok and report.cause == "exit_gate_unverified"


def test_empty_items_is_not_a_vacuous_pass():
    # 空の判定表（items: []）は全件突合の不履行＝解釈不能と同じ扱い（fail-closed）。
    # 受理すると検証 LLM の手抜き出力が無検証のままゲートを PASS させる
    out = json.dumps({"items": [], "new_issues": []})
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([out]), _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "")
    assert not report.ok and report.cause == "exit_gate_unverified"


def test_llm_execution_failure_fails_closed():
    def broken(prompt):
        raise RuntimeError("SSH 不達")
    v = ExitGateVerifier(_probe_recorder([]), broken, _TEMPLATE)
    report = v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "")
    assert not report.ok and report.cause == "exit_gate_unverified"


# ── 遮断（判定根拠の限定）と証跡採取 ──

def test_prompt_contains_contract_and_evidence_only():
    calls: list[str] = []
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([_PASS_JSON], calls), _TEMPLATE)
    v.verify("/opt/taka-ma/work/t1", dict(_TASK_BASE), "機械検査の証跡ブロック",
             answer_text=None)
    prompt = calls[0]
    assert "指摘Aと指摘Bを解消せよ" in prompt          # 根拠文書（確定要約）
    assert "機械検査の証跡ブロック" in prompt          # 機械検査の証跡
    assert "回答本文" not in prompt                    # answer_text=None なら成果物に載らない


def test_answer_text_included_only_for_answered_deliverable():
    calls: list[str] = []
    v = ExitGateVerifier(_probe_recorder([]), _llm_factory([_PASS_JSON], calls), _TEMPLATE)
    v.verify(None, dict(_TASK_BASE), "", answer_text="回答の実体テキスト")
    assert "回答の実体テキスト" in calls[0]


def test_evidence_probes_are_fixed_catalog_and_reject_unsafe_paths():
    commands: list[str] = []
    task = dict(_TASK_BASE)
    task["target_paths"] = ["docs/a.md", "bad;rm -rf /", "../escape.md"]
    v = ExitGateVerifier(_probe_recorder(commands), _llm_factory([_PASS_JSON]), _TEMPLATE)
    v.verify("/opt/taka-ma/work/t1", task, "")
    joined = "\n".join(commands)
    assert "git -C /opt/taka-ma/work/t1 show HEAD" in joined     # 固定カタログの採取
    assert "docs/a.md" in joined
    assert "rm -rf" not in joined and "escape.md" not in joined  # 不正パスは採取しない


def test_collect_check_paths_merges_target_paths_and_acceptance():
    task = {"target_paths": ["docs/a.md"],
            "acceptance": [{"kind": "file", "params": {"path": "docs/b.md"}},
                           {"kind": "file", "params": {"path": "docs/a.md"}}]}
    assert collect_check_paths(task) == ["docs/a.md", "docs/b.md"]


# ── _execute_chain 配線（差し戻しループ・報告添付・遮断） ──

class _SlackSpy:
    def __init__(self):
        self.sent = []

    def notify(self, text, channel=None, *, team_id=None, thread_ts=None):
        self.sent.append(text)


def _chain_orchestrator(llm_outputs: list[str], worker_output: str,
                        llm_calls: list[str] | None = None, max_reinject: int = 1):
    """出口ゲートつき _execute_chain の成功分岐を駆動できる最小 Orchestrator を作る。"""
    o = Orchestrator.__new__(Orchestrator)
    o.slack = _SlackSpy()
    o.config = {
        "task_context": {"workspace_base": "/opt/taka-ma/work"},
        "models": {"verifier": {"command": "claude", "model_flag": "--model opus"}},
        "exit_gate": {"model": "verifier", "timeout_sec": 5,
                      "max_reinject": max_reinject},
    }
    seq = list(llm_outputs)

    def run_ssh_command(remote, timeout=None, stdin_text=None):
        if llm_calls is not None:
            llm_calls.append(stdin_text)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    o.process_mgr = types.SimpleNamespace(
        run_ssh_probe=lambda cmd, timeout=30: (0, ""),
        run_ssh_command=run_ssh_command)
    o._updates = []

    async def _fake_update(task_file, status, result=None, extra=None):
        o._updates.append((status, result, extra))
        return "/opt/taka-ma/data/tasks/done/2026-09-12/t1.json"
    o._update_status = _fake_update

    async def _fake_sub(task, subtask, results, futures, channel, completed_steps=None):
        results[subtask["step"]] = worker_output
        futures[subtask["step"]].set_result(worker_output)
    o._execute_subtask_in_chain = _fake_sub

    o.conversation = types.SimpleNamespace(
        append_task_result=lambda task, text, path, ws=None: None)
    return o


_TASK = {"task_id": "t1", "channel_id": "C1", "team_id": "T1", "thread_ts": "1.2",
         "command": "指摘Aと指摘Bを解消せよ", "target_paths": ["docs/a.md"]}
_SUBTASKS = [{"step": 1, "command": "指摘を解消する", "execution": "agent",
              "depends_on": []}]


def test_chain_gate_pass_attaches_report_to_completion():
    o = _chain_orchestrator([_PASS_JSON], "作業を実施しました")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    assert o._updates[0][0] == "completed"
    assert any("タスク完了" in m for m in o.slack.sent)
    # 独立検証レポート（判定表）が通知へ添付される（§8.10f 報告への添付）
    assert any("独立検証" in m and "resolved" in m for m in o.slack.sent)


def test_chain_gate_prompt_excludes_worker_self_report():
    calls: list[str] = []
    o = _chain_orchestrator([_PASS_JSON], "SECRETMARKER: 完了しましたと自己申告", calls)
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    assert calls, "検証エージェントが呼ばれていない"
    assert all("SECRETMARKER" not in p for p in calls)   # 遮断（構造的に渡らない）


def test_chain_gate_fail_reinjects_without_completion_report():
    o = _chain_orchestrator([_FAIL_JSON], "作業を実施しました")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    status, _, extra = o._updates[0]
    assert status == "init"                              # 完了にせず差し戻し
    assert extra["_exit_gate_count"] == 1
    assert extra["completed_steps"] == {}                # 全 step やり直し
    assert "指摘Bの解消" in extra["exit_gate_findings"]
    assert not any("タスク完了" in m for m in o.slack.sent)
    assert any("差し戻して再実行" in m for m in o.slack.sent)


def test_chain_gate_fail_over_limit_reports_unmet():
    o = _chain_orchestrator([_FAIL_JSON], "作業を実施しました")
    task = dict(_TASK)
    task["_exit_gate_count"] = 1                         # 既に 1 回差し戻し済み（上限 1）
    asyncio.run(o._execute_chain("f.json", task, _SUBTASKS))
    status, result, extra = o._updates[0]
    assert status == "completed"
    assert extra == {"failure_cause": "exit_gate_failed"}
    assert any("タスク未完了" in m and "独立検証で未達" in m for m in o.slack.sent)


def test_chain_gate_unverified_does_not_reinject():
    # 判定不能（JSON 逸脱が 2 回続く）は worker 再実行で直らないため差し戻さず未達
    o = _chain_orchestrator(["not json", "still not json"], "作業を実施しました")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    status, _, extra = o._updates[0]
    assert status == "completed"
    assert extra == {"failure_cause": "exit_gate_unverified"}
    assert not any("タスク完了（" in m for m in o.slack.sent)


def test_chain_directive_task_fails_without_reinject():
    o = _chain_orchestrator([_FAIL_JSON], "実行しました")
    task = dict(_TASK)
    task["directive"] = "git push origin main"
    o._executed_commands = {"t1": ["git push origin main"]}   # 遵守照合を PASS させる
    asyncio.run(o._execute_chain("f.json", task, _SUBTASKS))
    status, _, extra = o._updates[0]
    assert status == "completed"                         # 差し戻しなし（逐語命令は言い換え不能）
    assert extra == {"failure_cause": "exit_gate_failed"}


def test_chain_without_exit_gate_config_skips_gate():
    o = _chain_orchestrator([_FAIL_JSON], "作業を実施しました")
    del o.config["exit_gate"]                            # 段階導入: 未構成なら従来動作
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    assert o._updates[0][0] == "completed"
    assert any("タスク完了" in m for m in o.slack.sent)


# ── 差し戻し後の worker 指示（判定表の前置き） ──

def test_findings_are_prepended_to_worker_command():
    o = Orchestrator.__new__(Orchestrator)
    o.slack = _SlackSpy()
    o._plan_execution = lambda *a, **k: ("agent", ["opus"], False)
    captured = {}

    async def _fake_enqueue(item):
        captured["command"] = item["_command"]
        item["_result_future"].set_result("done")
    o._enqueue = _fake_enqueue

    task = {"task_id": "t1", "channel_id": "C1",
            "exit_gate_findings": "- [unresolved] 指摘Bの解消（根拠: 証跡に実体なし）"}
    subtask = {"step": 1, "command": "指摘を解消する", "execution": "agent",
               "depends_on": []}
    results: dict = {}

    async def _run():
        futures = {1: asyncio.get_event_loop().create_future()}
        await o._execute_subtask_in_chain(task, subtask, results, futures, "C1")
    asyncio.run(_run())
    assert captured["command"].startswith("前回実行は独立検証で未達と判定された")
    assert "指摘Bの解消" in captured["command"]
    assert "指摘を解消する" in captured["command"]
