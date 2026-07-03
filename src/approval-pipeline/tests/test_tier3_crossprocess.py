"""Tier 3 cross-process 承認（§8.10）の回帰テスト。

`/code-review` で検出した不具合の再発防止:
  - #1: タイムアウト境界で u-zu の承認(approved)が timeout に握り潰される競合
  - #2: Slack 送信失敗時に安全側 deny へ倒し孤児 pending を残さない
  - #5: 承認レコードに worker context を載せる
  - #6: 決定後に承認ファイルを done/ へ退避する

pytest-asyncio に依存せず実行できるよう、各テストは `asyncio.run()` で同期的に駆動する。
u-zu のボタン押下は「承認ファイルの status を書き換える」ことなので、テストでは直接書き込んで模す。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

import asyncio
import json
import os
import tempfile

import tier3_handler as t3


class FakePTY:
    """WorkerPtyWrapper の approve()/deny() だけを観測するスタブ。"""
    instance_id = "task-abc-step1"

    def __init__(self):
        self.action = None

    def approve(self):
        self.action = "approve"

    def deny(self):
        self.action = "deny"


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = None
        self.notes = []

    def send_approval_request(self, **kw):
        if self.fail:
            raise RuntimeError("slack down")
        self.sent = kw

    def notify(self, text, channel=None, team_id=None):
        self.notes.append(text)


class Prompt:
    command = "rm -rf /prod"
    context = "Run: rm -rf /prod\nthis will delete production"


def _uzu_write(path, status):
    """u-zu のボタン押下を模す: pending のときだけ status を書き換える（resolve_approval 相当）。"""
    with open(path) as f:
        record = json.load(f)
    if record.get("status") != "pending":
        return False
    record["status"] = status
    tmp = f"{path}.uzu.tmp"
    with open(tmp, "w") as f:
        json.dump(record, f, ensure_ascii=False)
    os.replace(tmp, path)
    return True


def _handler(tmp):
    return t3.Tier3Handler(slack_notifier=FakeNotifier(), approval_dir=tmp)


# ── 回帰: 競合の最終裁定（承認を握り潰さない） ──

def test_claim_timeout_honors_late_approval():
    tmp = tempfile.mkdtemp()
    h = _handler(tmp)
    path = os.path.join(tmp, "r.json")
    h._write_record(path, {"request_id": "r", "status": "pending"})
    assert _uzu_write(path, "approved") is True          # 境界でユーザー承認
    assert h._claim_timeout(path) == "approved"          # sa-ru の timeout 処理は上書きしない
    assert json.load(open(path))["status"] == "approved"


def test_claim_timeout_sets_timeout_when_still_pending():
    tmp = tempfile.mkdtemp()
    h = _handler(tmp)
    path = os.path.join(tmp, "r.json")
    h._write_record(path, {"request_id": "r", "status": "pending"})
    assert h._claim_timeout(path) == "timeout"           # 誰も押さなければ従来どおり timeout
    assert json.load(open(path))["status"] == "timeout"


# ── end-to-end: approve / reject / timeout ──

def _run_decided(status, monkey_timeout=2.0):
    """handle() を回し、ポーリング中に u-zu が status を書く e2e。"""
    tmp = tempfile.mkdtemp()
    pty = FakePTY()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp)

    async def scenario():
        async def flip():
            await asyncio.sleep(0.1)
            rid = notifier.sent["request_id"]
            _uzu_write(os.path.join(tmp, f"{rid}.json"), status)
        res, _ = await asyncio.gather(
            h.handle(Prompt(), pty, ctx={"instance_id": "i", "risk_reason": "本番削除",
                                         "team_id": "T1", "channel": "C1", "task_id": "t"}),
            flip(),
        )
        return res

    orig_poll, orig_to = t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS
    t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = 0.05, monkey_timeout
    try:
        res = asyncio.run(scenario())
    finally:
        t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = orig_poll, orig_to
    return res, pty, notifier, tmp


def test_e2e_approve():
    res, pty, notifier, tmp = _run_decided("approved")
    assert res["action"] == "approved" and pty.action == "approve"
    # 承認リクエストに context が渡る
    assert "Run: rm -rf /prod" in notifier.sent["context"]
    # 決定後ファイルは done/ へ退避（メインディレクトリには残らない）
    assert [f for f in os.listdir(tmp) if f.endswith(".json")] == []
    assert os.listdir(os.path.join(tmp, "done"))


def test_e2e_reject():
    res, pty, _, _ = _run_decided("rejected")
    assert res["action"] == "denied" and pty.action == "deny"


def test_e2e_timeout():
    tmp = tempfile.mkdtemp()
    pty = FakePTY()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp)
    orig_poll, orig_to = t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS
    t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = 0.05, 0.2
    try:
        res = asyncio.run(h.handle(Prompt(), pty, ctx={"instance_id": "i"}))
    finally:
        t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = orig_poll, orig_to
    assert res["reason"] == "timeout" and pty.action == "deny"
    assert any("タイムアウト" in n for n in notifier.notes)


# ── Slack 送信失敗 → 安全側 deny・孤児 pending を残さない ──

def test_slack_failure_denies_and_cleans_up():
    tmp = tempfile.mkdtemp()
    pty = FakePTY()
    h = t3.Tier3Handler(slack_notifier=FakeNotifier(fail=True), approval_dir=tmp)
    res = asyncio.run(h.handle(Prompt(), pty, ctx={"instance_id": "i"}))
    assert res["action"] == "denied" and res["reason"] == "slack_error"
    assert pty.action == "deny"
    assert [f for f in os.listdir(tmp) if f.endswith(".json")] == []   # 孤児 pending なし
