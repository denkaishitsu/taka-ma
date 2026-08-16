"""完了報告の実出力グラウンディング（設計書 §8.9 / §8.3 probe）の振る舞いテスト。

2026-08-10 Slack DM インシデントの再発防止:
  F1: worker が「push しました」と自己申告しただけで「タスク完了」と報告される
      → remote 実出力（git ls-remote）で裏取りし、確認できなければ「未完了」と報告する。
  F2: 確認系質問（リポジトリ・ブランチ・ファイル名）に「実行します」という宣言だけを
      反復して実行結果を返さない → probe で実コマンドを実行し実出力を 1 メッセージで返す。

grep では潰せない振る舞い（判定分岐・通知文言の機械導出・実行不能時の縮退）を分離実行で担保する。
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import asyncio
import json
import os
import threading
import types

from orchestrator import Orchestrator
from orchestrator.conversation import ConversationManager
from orchestrator.grounding import GroundingVerifier, detect_claims
from orchestrator.headless_runner import WorkerHeadlessRunner

LOCAL_HASH = "a" * 40


# ── 主張検出（worker 出力は検証の選別にのみ使う） ──

def test_detect_claims_push_japanese_and_english():
    assert detect_claims("リモートへプッシュしました")["push"]
    assert detect_claims("Changes were pushed to origin")["push"]


def test_detect_claims_commit():
    assert detect_claims("コミットを作成しました")["commit"]
    assert detect_claims("committed as abc123")["commit"]


def test_detect_claims_none_for_unrelated_text():
    claims = detect_claims("ドキュメントを整理し、要件を 3 点にまとめました")
    assert not claims["push"] and not claims["commit"]


def test_detect_claims_ignores_clearly_negated_mentions():
    # 正直な未実施報告（F1 とは逆）に ⚠ 未完了警告を重ねない
    assert not detect_claims("push はしていません")["push"]
    assert not detect_claims("Changes were not pushed to the remote")["push"]
    # 肯定と否定が混在するなら主張あり（検証する側に倒す）
    assert detect_claims("push しました。force push はしていません")["push"]


# ── GroundingVerifier（判定はコマンド実出力のみから導出） ──

def _probe_factory(responses: dict):
    """コマンド部分文字列 → (rc, 出力) の対応で run_probe を偽装する。"""
    def run_probe(command: str, timeout: int = 30):
        for key, resp in responses.items():
            if key in command:
                return resp
        return (0, "")
    return run_probe


def _git_ok_responses(remote_hash: str | None):
    """コミット済み workspace の応答一式。remote_hash=None は ls-remote 空応答（remote 未反映）。"""
    return {
        "ls -la": (0, "total 8\n-rw-r--r--  1 u  staff  42 REQ.md"),
        "rev-parse --is-inside-work-tree": (0, "true"),
        "log -1": (0, f"{LOCAL_HASH} feat: 要件定義"),
        "rev-parse --abbrev-ref HEAD": (0, "main"),
        "ls-remote": (0, f"{remote_hash}\trefs/heads/main" if remote_hash else ""),
    }


def test_push_claim_confirmed_when_remote_matches_local_head():
    v = GroundingVerifier(_probe_factory(_git_ok_responses(LOCAL_HASH)))
    report = v.verify("/opt/taka-ma/work/t1", "コミットして push しました")
    assert report.ok
    assert "push を remote 実出力で確認" in report.summary
    assert LOCAL_HASH[:12] in report.summary or LOCAL_HASH[:12] in report.text


def test_push_claim_unconfirmed_when_remote_has_no_ref():
    # F1 の再現: worker は push 完了を主張するが remote に当該ブランチが存在しない
    v = GroundingVerifier(_probe_factory(_git_ok_responses(None)))
    report = v.verify("/opt/taka-ma/work/t1", "push は正常に完了しています")
    assert not report.ok
    assert "push は未完了" in report.note
    assert "未完了" in report.summary


def test_push_claim_unconfirmed_when_remote_hash_differs():
    v = GroundingVerifier(_probe_factory(_git_ok_responses("b" * 40)))
    report = v.verify("/opt/taka-ma/work/t1", "pushed to origin/main")
    assert not report.ok
    assert "不一致" in report.note


def test_push_claim_unconfirmed_when_workspace_not_a_repo():
    # F1 の実態: 使い捨て workspace が git リポジトリですらない
    responses = {
        "ls -la": (0, "total 0"),
        "rev-parse --is-inside-work-tree": (128, "fatal: not a git repository"),
    }
    v = GroundingVerifier(_probe_factory(responses))
    report = v.verify("/opt/taka-ma/work/t1", "リモートへプッシュ済みです")
    assert not report.ok
    assert "push は未完了" in report.note


def test_push_claim_unconfirmed_when_ssh_unreachable():
    # 検証不能は fail-open にしない（確認できない push は「未完了」と報告する）
    v = GroundingVerifier(lambda cmd, timeout=30: (-1, "SSH 実行不能: timeout"))
    report = v.verify("/opt/taka-ma/work/t1", "push しました")
    assert not report.ok


def test_commit_claim_reports_real_hash():
    v = GroundingVerifier(_probe_factory(_git_ok_responses(None)))
    report = v.verify("/opt/taka-ma/work/t1", "コミットしました（push はしていません）")
    assert report.ok  # push 主張なし・コミットは実ハッシュで確認できた
    assert LOCAL_HASH[:12] in report.summary
    assert LOCAL_HASH in report.text  # git log 実出力が証跡に残る


def test_no_claims_with_unreachable_ssh_does_not_flag_incomplete():
    # 主張が無いタスクは一時的な SSH 不達で「⚠ 未完了」を出さない（警告の常態化防止）。
    # 証跡には rc=-1 の実測が残る
    v = GroundingVerifier(lambda cmd, timeout=30: (-1, "SSH 実行不能: timeout"))
    report = v.verify("/opt/taka-ma/work/t1", "要件を整理しました")
    assert report.ok
    assert "rc=-1" in report.text


def test_no_claims_still_records_ls_output():
    # ファイル生成報告の裏付け: 主張が無くても実パスと ls 実出力を証跡に含める
    v = GroundingVerifier(_probe_factory({"ls -la": (0, "total 8\n-rw- a.md"),
                                          "rev-parse --is-inside-work-tree": (128, "fatal")}))
    report = v.verify("/opt/taka-ma/work/t1", "要件を整理しました")
    assert report.ok
    assert "ls -la" in report.text and "a.md" in report.text


# ── _execute_chain 配線（完了/未完了の文言が検証結果から機械導出される） ──

class _SlackSpy:
    def __init__(self):
        self.sent = []

    def notify(self, text, channel=None, *, team_id=None, thread_ts=None):
        self.sent.append(text)


def _chain_orchestrator(probe_responses: dict, worker_output: str):
    """_execute_chain の成功分岐だけを駆動できる最小 Orchestrator を作る。"""
    o = Orchestrator.__new__(Orchestrator)
    o.slack = _SlackSpy()
    o.config = {"task_context": {"workspace_base": "/opt/taka-ma/work"}}
    o.process_mgr = types.SimpleNamespace(run_ssh_probe=_probe_factory(probe_responses))
    o._updates = []

    async def _fake_update(task_file, status, result=None, extra=None):
        o._updates.append((status, result))
        return "/opt/taka-ma/data/tasks/done/2026-08-14/t1.json"
    o._update_status = _fake_update

    async def _fake_sub(task, subtask, results, futures, channel, completed_steps=None):
        results[subtask["step"]] = worker_output
        futures[subtask["step"]].set_result(worker_output)
    o._execute_subtask_in_chain = _fake_sub

    o._reflow = []
    o.conversation = types.SimpleNamespace(
        append_task_result=lambda task, text, path, ws=None: o._reflow.append((text, ws)))
    return o


_TASK = {"task_id": "t1", "channel_id": "C1", "team_id": "T1", "thread_ts": "1.2",
         "user_id": "U1"}
_SUBTASKS = [{"step": 1, "command": "実装して push", "execution": "agent", "depends_on": []}]


def test_chain_reports_incomplete_when_push_unverified():
    o = _chain_orchestrator(_git_ok_responses(None), "コミットとプッシュを実行しました。正常に完了しています")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    header_msg = o.slack.sent[0]
    assert "タスク未完了" in header_msg and "push は未完了" in header_msg
    # 実測証跡（コマンドと rc）が通知に含まれる
    assert any("ls-remote" in m and "rc=" in m for m in o.slack.sent)
    # 会話還流の先頭がグラウンディング判定で上書きされ、workspace が紐付く
    reflow_text, reflow_ws = o._reflow[0]
    assert reflow_text.startswith("（実測確認の結果、未完了")
    assert reflow_ws == "/opt/taka-ma/work/t1"


def test_chain_reports_complete_when_push_verified():
    o = _chain_orchestrator(_git_ok_responses(LOCAL_HASH), "コミットして push しました")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    assert o.slack.sent[0].startswith("タスク完了")
    reflow_text, _ = o._reflow[0]
    assert reflow_text.startswith("（実測確認済み")


def test_chain_records_grounding_evidence_in_result_file():
    # 証跡は Slack 表示と独立に正本（結果ファイル）へも残る（§8.9）
    o = _chain_orchestrator(_git_ok_responses(None), "push 完了です")
    asyncio.run(o._execute_chain("f.json", dict(_TASK), _SUBTASKS))
    status, result = o._updates[0]
    assert status == "completed"
    assert "【実測確認】" in result and "ls-remote" in result


# ── headless worker の exit code（成功文言の機械導出・§8.9） ──

class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProc:
    def __init__(self, lines, returncode):
        self.stdout = _FakeStdout(lines)
        self.stderr = types.SimpleNamespace()
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


def test_headless_run_captures_returncode(monkeypatch):
    result_line = json.dumps({"type": "result", "result": "done"}).encode() + b"\n"
    proc = _FakeProc([result_line], returncode=3)

    async def _fake_exec(*args, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = WorkerHeadlessRunner("t1-step1-fable")
    result = asyncio.run(runner.run("task", timeout=5))
    assert result.returncode == 3
    assert result.text == "done"


def test_run_worker_headless_raises_on_nonzero_exit(monkeypatch):
    import orchestrator as orch_mod

    class _FakeRunner:
        def __init__(self, *a, **k):
            pass

        async def run(self, command, timeout):
            return types.SimpleNamespace(text="push しました", session_id=None, returncode=3)

    monkeypatch.setattr(orch_mod, "WorkerHeadlessRunner", _FakeRunner)
    o = Orchestrator.__new__(Orchestrator)
    o.config = {"headless": {
        "mini_host": "mac-mini", "decide_client": "/x/decide_client.py",
        "decide_socket": "/x/decide.sock", "hook_timeout_sec": 310,
        "python_bin": "/opt/taka-ma-env/bin/python3", "run_timeout_sec": 60}}
    o.process_mgr = types.SimpleNamespace(run_ssh_command=lambda *a, **k: "")
    o._mbp_host = "mbp"
    # §8.14 / #139（merge 統合: _run_worker_headless が起動前に preflight.check を呼ぶ）
    o.preflight = types.SimpleNamespace(check=lambda *a, **k: None)
    try:
        asyncio.run(o._run_worker_headless("i1", "claude", "do", workspace="/opt/taka-ma/work/t1"))
        raise AssertionError("非 0 終了が成功として扱われた")
    except RuntimeError as e:
        assert "非 0 終了" in str(e)


# ── 会話 probe（確認系質問に宣言でなく実行結果を返す・§8.3） ──

def _cm(tmp_path, process_mgr=None):
    cm = ConversationManager.__new__(ConversationManager)
    cm.sessions = {}
    cm._last_seen = {}
    cm._last_workspace = {}
    cm.session_workspace = {}   # §8.13 / #143（merge 統合: _persist_session が参照）
    cm.worker_home = None       # §8.13 / #143（~ 展開の供給元。テストでは未設定＝差し戻し）
    cm.canceller = None         # §8.10d / #138（merge 統合: handle_message の制御判定が参照）
    cm._sessions_lock = threading.Lock()
    cm.sessions_dir = str(tmp_path / "sessions")
    os.makedirs(cm.sessions_dir, exist_ok=True)
    cm.confirm_dir = str(tmp_path / "confirm")  # 空 = pending 訂正なし
    cm.task_dir = str(tmp_path / "tasks")
    cm.slack = _SlackSpy()
    cm.process_mgr = process_mgr
    cm.workspace_base = "/opt/taka-ma/work"
    cm.session_ttl_sec = 3600
    cm.classifier = None
    cm.plan_service = None
    return cm


_MSG = {"conversation_id": "T1:C1:9.9", "channel_id": "C1", "team_id": "T1",
        "thread_ts": "9.9", "text": "リポジトリとブランチとファイル名を教えて"}


def test_answer_probe_returns_command_outputs_in_one_message(tmp_path):
    responses = {
        "remote -v": (0, "origin\tgit@github.com:u/r.git (push)"),
        "abbrev-ref": (0, "main"),
        "ls -la": (0, "total 8\n-rw- REQ.md"),
    }
    cm = _cm(tmp_path, types.SimpleNamespace(run_ssh_probe=_probe_factory(responses)))
    cm._last_workspace[_MSG["conversation_id"]] = "/opt/taka-ma/work/t1"
    cm._answer_probe(dict(_MSG))
    assert len(cm.slack.sent) == 1  # 1 メッセージで返す（宣言の反復をしない）
    text = cm.slack.sent[0]
    assert "git@github.com:u/r.git" in text and "main" in text and "REQ.md" in text
    assert "rc=0" in text and "実行します" not in text
    # 実測結果は会話履歴にも残る（後続ターンで脳が事実として参照する）
    assert cm.sessions[_MSG["conversation_id"]][-1]["text"] == text


def test_answer_probe_reports_error_when_ssh_fails(tmp_path):
    cm = _cm(tmp_path, types.SimpleNamespace(
        run_ssh_probe=lambda cmd, timeout=30: (-1, "SSH 実行不能: timeout")))
    cm._last_workspace[_MSG["conversation_id"]] = "/opt/taka-ma/work/t1"
    cm._answer_probe(dict(_MSG))
    assert len(cm.slack.sent) == 1
    assert "rc=-1" in cm.slack.sent[0] and "SSH 実行不能" in cm.slack.sent[0]


def test_answer_probe_reports_missing_workspace_as_fact(tmp_path):
    cm = _cm(tmp_path, types.SimpleNamespace(run_ssh_probe=_probe_factory({})))
    cm._answer_probe(dict(_MSG))
    assert len(cm.slack.sent) == 1
    assert "実測確認を実行できません" in cm.slack.sent[0]


def test_handle_message_routes_probe_instead_of_llm_reply(tmp_path):
    # F2 の再現: 脳が「今すぐ実行し結果のみ報告します」と宣言しても、それは送らず実出力を返す
    responses = {"remote -v": (0, "origin\tgit@github.com:u/r.git (push)"),
                 "abbrev-ref": (0, "main"), "ls -la": (0, "REQ.md")}
    cm = _cm(tmp_path, types.SimpleNamespace(run_ssh_probe=_probe_factory(responses)))
    cm._last_workspace[_MSG["conversation_id"]] = "/opt/taka-ma/work/t1"
    cm._invoke_llm = lambda history, force, progress=None: {
        "reply": "今すぐ git remote -v を実行し結果のみ報告します", "ready": False,
        "summary": None, "probe": "repo_status"}
    cm.handle_message(dict(_MSG))
    assert len(cm.slack.sent) == 1
    assert "git@github.com" in cm.slack.sent[0]
    assert "報告します" not in cm.slack.sent[0]


def test_invoke_llm_accepts_only_allowlisted_probe(tmp_path, monkeypatch):
    import orchestrator.conversation as conv_mod
    cm = _cm(tmp_path)
    cm.model = "m"
    cm.ollama_host = "h"
    cm.timeout = 5
    cm.think = None
    cm._prompt_template = "{history}|{message}"
    monkeypatch.setattr(conv_mod, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "x", "ready": False, "summary": None, "probe": "repo_status"}))
    assert cm._invoke_llm([{"role": "user", "text": "q"}], force=False)["probe"] == "repo_status"
    monkeypatch.setattr(conv_mod, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "x", "ready": False, "summary": None, "probe": "rm -rf /"}))
    assert cm._invoke_llm([{"role": "user", "text": "q"}], force=False)["probe"] is None


def test_last_workspace_persists_across_restart(tmp_path):
    cm1 = _cm(tmp_path)
    cm1._set_last_workspace("T1:C1:9.9", "/opt/taka-ma/work/t9")
    cm2 = _cm(tmp_path)  # sa-ru 再起動を模す（同じ sessions_dir・別インスタンス）
    with cm2._sessions_lock:
        cm2._load_or_create_session("T1:C1:9.9")
    assert cm2._last_workspace["T1:C1:9.9"] == "/opt/taka-ma/work/t9"


def test_create_exec_task_records_default_workspace(tmp_path):
    cm = _cm(tmp_path)
    record = {"conversation_id": "T1:C1:9.9", "summary": "要件定義を作る",
              "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "9.9"}
    task_id = cm.create_exec_task(record)
    assert cm._last_workspace["T1:C1:9.9"] == f"/opt/taka-ma/work/{task_id}"


def test_create_exec_task_records_explicit_repo_workspace(tmp_path):
    cm = _cm(tmp_path)
    record = {"conversation_id": "T1:C1:9.9", "summary": "修正する",
              "workspace": "/Users/u/DevDev/myrepo",
              "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "9.9"}
    cm.create_exec_task(record)
    assert cm._last_workspace["T1:C1:9.9"] == "/Users/u/DevDev/myrepo"
