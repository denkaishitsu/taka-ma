"""承認の引き継ぎ（§8.10）— 再投入後の同一操作に再承認を求めない照合の検証。

保留 → 決着 → 再投入で worker が承認済み操作へ再到達したとき、decide() が
approvals/done/ の approved レコード（同一 task_id・tool_name・tool_input 完全一致）を
根拠に、承認依頼を発行せず allow を返すこと。および引き継ぎが効いてはならない側
（入力不一致・別タスク・却下済み・always_deny）で効かないことを検証する。

背景: 2026-08-17 の G2 実機検証（T09-B8）で、人間の応答が猶予（hold_grace_sec）より
遅い環境では「承認 → 保留 → 再投入 → 同一操作で再依頼」が循環し、何度承認しても
実行に反映されない欠陥を確認した。本テストはその再発防止の回帰である。
"""

import asyncio
import json
import os
import tempfile

from approval_types import PendingApproval
from test_e2e import FakeNotifier, _pipeline

TASK = "task-abc-123"
TOOL_INPUT = {"file_path": "/opt/taka-ma/work/task-abc-123/deploy.txt", "content": "デプロイ"}


def _write_done_record(approval_dir: str, *, request_id="orig-req-1", status="approved",
                       task_id=TASK, tool_name="Write", tool_input=None):
    """approvals/done/ に決着済みレコードを 1 件置く（引き継ぎ照合の入力）。"""
    done = os.path.join(approval_dir, "done")
    os.makedirs(done, exist_ok=True)
    with open(os.path.join(done, f"{request_id}.json"), "w") as f:
        json.dump({
            "request_id": request_id,
            "task_id": task_id,
            "tool_name": tool_name,
            "tool_input": TOOL_INPUT if tool_input is None else tool_input,
            "status": status,
        }, f)


def _decide(pipeline, pending, task_id=TASK):
    return asyncio.run(pipeline.decide(pending, instance_id="test-carry", task_id=task_id))


def test_carryover_allows_identical_approved_operation():
    """同一タスク・同一操作の approved レコードがあれば、承認依頼を発行せず allow する。

    監査には handler=approval_carryover と元 request_id が残り、人間決定へ遡れる。
    """
    with tempfile.TemporaryDirectory() as tmp:
        _write_done_record(tmp)
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)  # 分類は Tier3 だが照合が先に通す
        pending = PendingApproval(tool_name="Write", tool_input=dict(TOOL_INPUT))

        result = _decide(pipeline, pending)

        assert result.allow
        assert result.handler == "approval_carryover"
        assert "orig-req-1" in result.reason            # 元の人間決定へ遡れること
        assert notifier.sent is None                    # 新規の承認依頼を発行しない
        assert pipeline.logger.entries[-1]["tier"] == 3  # 人間決定由来として監査に残る


def test_no_carryover_when_tool_input_differs():
    """tool_input が 1 要素でも違えば別操作 — 引き継がず通常の Tier3（保留）へ進む。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_done_record(tmp)
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)
        pending = PendingApproval(
            tool_name="Write",
            tool_input={**TOOL_INPUT, "content": "デプロイ v2"})  # 内容が違う

        result = _decide(pipeline, pending)

        assert not result.allow
        assert result.hold                              # 猶予超過で保留（テスト値 2 秒）
        assert notifier.sent is not None                # 承認依頼は通常どおり発行される


def test_no_carryover_across_tasks():
    """別タスクの承認は越境しない（同一 tool_input でも task_id が違えば対象外）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_done_record(tmp, task_id="other-task-999")
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)
        pending = PendingApproval(tool_name="Write", tool_input=dict(TOOL_INPUT))

        result = _decide(pipeline, pending)

        assert not result.allow
        assert result.hold
        assert notifier.sent is not None


def test_rejected_record_carries_over_as_deny():
    """rejected レコードは「再依頼せず deny」で引き継ぐ（§8.10 却下の粒度・2026-09-03。
    旧仕様は却下済み操作へ再び承認依頼を出し、同じ操作を人へ二度聞いていた）。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_done_record(tmp, status="rejected")
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)
        pending = PendingApproval(tool_name="Write", tool_input=dict(TOOL_INPUT))

        result = _decide(pipeline, pending)

        assert not result.allow
        assert not result.hold                          # 保留せず即 deny（worker は次へ進める）
        assert result.handler == "rejection_carryover"
        assert "orig-req-1" in result.reason            # 元の人間決定へ遡れること
        assert notifier.sent is None                    # 却下済み操作を人へ二度聞かない


def test_always_deny_beats_carryover():
    """決定論の always_deny は引き継ぎでも覆せない（deny が常に勝つ・§8.10 判定順）。"""
    with tempfile.TemporaryDirectory() as tmp:
        deny_input = {"command": "rm -rf /"}
        _write_done_record(tmp, tool_name="Bash", tool_input=deny_input)
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)
        pending = PendingApproval(tool_name="Bash", tool_input=dict(deny_input))

        result = _decide(pipeline, pending)

        assert not result.allow
        assert result.handler == "safety_deny"


def test_no_carryover_without_task_id():
    """task_id が無い判定（interactive 単発等）ではスコープを主張できず、引き継がない。"""
    with tempfile.TemporaryDirectory() as tmp:
        _write_done_record(tmp)
        notifier = FakeNotifier()
        pipeline = _pipeline(3, notifier, approval_dir=tmp)
        pending = PendingApproval(tool_name="Write", tool_input=dict(TOOL_INPUT))

        result = _decide(pipeline, pending, task_id="")

        assert not result.allow
        assert result.hold
