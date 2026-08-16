"""Tier 3 cross-process 承認（§8.10）の回帰テスト。

`/code-review` で検出した不具合の再発防止:
  - #1: 猶予の境界で u-zu の承認(approved)が握り潰される競合
  - #2: Slack 送信失敗時に安全側 deny へ倒し孤児 pending を残さない
  - #5: 承認レコードに worker context を載せる
  - #6: 決定後に承認ファイルを done/ へ退避する

#132 で「猶予超過＝自動 deny」を廃し「保留（hold）」へ変更した。保留は deny ではないため、
承認レコードを done/ へ退避しない（退避すると人間が後から押しても届かない）ことと、
同一タスクの 2 度目以降が猶予を待たず即 hold になる冪等性を本テストで担保する。

Tier3Handler.handle は CLI 非依存になり Decision（allow / reason）を返すだけで、y/n の物理伝達は
アダプタの責務。よって本テストは pty 反映ではなく返り値 Decision と承認ファイルの状態を検証する。
pytest-asyncio に依存せず、各テストは `asyncio.run()` で同期駆動する。u-zu のボタン押下は
「承認ファイルの status を書き換える」ことなので、テストでは直接書き込んで模す。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

import asyncio
import json
import os
import tempfile

import tier3_handler as t3
from approval_types import PendingApproval


class FakeNotifier:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = None
        self.notes = []

    def send_approval_request(self, **kw):
        if self.fail:
            raise RuntimeError("slack down")
        self.sent = kw

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)


def _pending():
    """Tier3 にかける高リスク要求（Bash: 本番削除）。operation_str で command 文字列になる。"""
    return PendingApproval(
        tool_name="Bash",
        tool_input={"command": "rm -rf /prod"},
        context="Run: rm -rf /prod\nthis will delete production",
    )


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


def _handler(tmp, *, hold_grace_sec=60, poll_interval_sec=1):
    """猶予/poll は #103 で yaml SSOT 化され構築時注入の必須引数になった（実効値と同値を渡す）。"""
    return t3.Tier3Handler(slack_notifier=FakeNotifier(), approval_dir=tmp,
                           hold_grace_sec=hold_grace_sec, poll_interval_sec=poll_interval_sec)


# ── 回帰: 競合の最終裁定（承認を握り潰さない） ──

def test_claim_hold_honors_late_approval():
    tmp = tempfile.mkdtemp()
    h = _handler(tmp)
    path = os.path.join(tmp, "r.json")
    h._write_record(path, {"request_id": "r", "status": "pending"})
    assert _uzu_write(path, "approved") is True          # 境界でユーザー承認
    assert h._claim_hold(path) == "approved"             # sa-ru の保留処理は上書きしない
    record = json.load(open(path))
    assert record["status"] == "approved"
    assert "held_at" not in record                       # 決着済みに保留印を付けない


def test_claim_hold_keeps_pending_and_stamps_held_at():
    """誰も押さなければ保留。status は pending のまま（＝人間は後からでも押せる）。"""
    tmp = tempfile.mkdtemp()
    h = _handler(tmp)
    path = os.path.join(tmp, "r.json")
    h._write_record(path, {"request_id": "r", "status": "pending"})
    assert h._claim_hold(path) == t3.HOLD                # timeout ではなく保留
    record = json.load(open(path))
    assert record["status"] == "pending"                 # 自動 deny しない（§8.10）
    assert record["held_at"]                             # 保留の印だけが付く
    # 保留後も u-zu は決定を書ける（pending のときのみ受理する契約を満たす）
    assert _uzu_write(path, "approved") is True


# ── end-to-end: approve / reject / hold ──

def _run_decided(status, hold_grace_sec=2.0):
    """handle() を回し、ポーリング中に u-zu が status を書く e2e。

    猶予/poll は構築時注入（#103）のため、旧モジュール定数の差し替えではなく
    コンストラクタで短縮値を渡す。
    """
    tmp = tempfile.mkdtemp()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp,
                        hold_grace_sec=hold_grace_sec, poll_interval_sec=0.05)

    async def scenario():
        async def flip():
            await asyncio.sleep(0.1)
            rid = notifier.sent["request_id"]
            _uzu_write(os.path.join(tmp, f"{rid}.json"), status)
        res, _ = await asyncio.gather(
            h.handle(_pending(), ctx={"instance_id": "i", "risk_reason": "本番削除",
                                      "team_id": "T1", "channel": "C1", "task_id": "t"}),
            flip(),
        )
        return res

    res = asyncio.run(scenario())
    return res, notifier, tmp


def test_e2e_approve():
    res, notifier, tmp = _run_decided("approved")
    assert res.allow                                     # 人間 approve → allow
    # 承認リクエストに context が渡る
    assert "Run: rm -rf /prod" in notifier.sent["context"]
    # 決定後ファイルは done/ へ退避（メインディレクトリには残らない）
    assert [f for f in os.listdir(tmp) if f.endswith(".json")] == []
    assert os.listdir(os.path.join(tmp, "done"))


def test_e2e_reject():
    res, _, _ = _run_decided("rejected")
    assert not res.allow                                 # 人間 reject → deny


def test_e2e_hold_keeps_record_alive():
    """猶予超過は自動 deny ではなく保留。承認レコードは pending 存置＋held_at（§3.3 (4)）。"""
    tmp = tempfile.mkdtemp()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp,
                        hold_grace_sec=0.2, poll_interval_sec=0.05)
    res = asyncio.run(h.handle(_pending(), ctx={"instance_id": "i", "task_id": "t"}))
    assert not res.allow                                 # ツールはブロックされる
    assert res.hold is True                              # ただし deny ではない
    assert res.reason.startswith("hold:")
    assert any("保留" in n for n in notifier.notes)      # 期限が無いことを人間へ伝える
    # 承認レコードはその場に残る（done/ へ退避すると後から承認できない＝旧実装の実害）
    remaining = [f for f in os.listdir(tmp) if f.endswith(".json")]
    assert len(remaining) == 1
    record = json.load(open(os.path.join(tmp, remaining[0])))
    assert record["status"] == "pending" and record["held_at"]
    assert not os.path.exists(os.path.join(tmp, "done"))


def test_e2e_hold_is_idempotent_per_task():
    """同一タスクに未決着の保留がある間は、新規レコードも Slack 再投稿もせず即 hold（§8.10）。

    worker が別ツールで迂回を試みるたびに猶予を待つと「迂回 N 回 × 猶予」が累積し、
    同じ保留通知が連投される。2 度目が猶予より十分速く返ることで冪等性を検証する。
    """
    import time as _time
    tmp = tempfile.mkdtemp()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp,
                        hold_grace_sec=0.2, poll_interval_sec=0.05)
    ctx = {"instance_id": "i", "task_id": "t"}
    asyncio.run(h.handle(_pending(), ctx=dict(ctx)))     # 1 回目: 猶予超過で保留
    sent_first = notifier.sent
    notes_first = len(notifier.notes)

    started = _time.monotonic()
    res = asyncio.run(h.handle(_pending(), ctx=dict(ctx)))  # 2 回目: 迂回の試み
    elapsed = _time.monotonic() - started

    assert res.hold is True and not res.allow
    assert elapsed < 0.2                                 # 猶予を待っていない
    assert notifier.sent is sent_first                   # 承認リクエストの再送なし
    assert len(notifier.notes) == notes_first            # 保留通知の連投なし
    assert len([f for f in os.listdir(tmp) if f.endswith(".json")]) == 1  # レコードは 1 件のまま


def test_e2e_deadline_clamps_poll_budget():
    """decide_deadline（デーモン外側タイムアウトの締切）が hold_grace_sec=60 秒より近いとき、
    ポーリングは締切の内側で保留を確定させる（前段消費時間で残余が縮んでも
    「内側が先に確定」を保つ・T08-V11 の包含）。hold_grace_sec は実効値のまま
    （60 秒）にし、締切だけで短く切れることを検証する。"""
    import time as _time
    tmp = tempfile.mkdtemp()
    notifier = FakeNotifier()
    h = t3.Tier3Handler(slack_notifier=notifier, approval_dir=tmp,
                        hold_grace_sec=60, poll_interval_sec=0.05)
    started = _time.monotonic()
    res = asyncio.run(h.handle(_pending(), ctx={
        "instance_id": "i",
        "decide_deadline": _time.monotonic() + 1.0,  # 残余 1 秒 ＜ hold_grace_sec 60 秒
    }))
    elapsed = _time.monotonic() - started
    assert res.hold is True and not res.allow
    assert elapsed < 10                                  # 60 秒待ちに入っていない
    # 保留は退避しない（承認レコードは生きたまま人間の決着を待つ）
    assert len([f for f in os.listdir(tmp) if f.endswith(".json")]) == 1


# ── Slack 送信失敗 → 安全側 deny・孤児 pending を残さない ──

def test_slack_failure_denies_and_cleans_up():
    tmp = tempfile.mkdtemp()
    h = t3.Tier3Handler(slack_notifier=FakeNotifier(fail=True), approval_dir=tmp,
                        hold_grace_sec=60, poll_interval_sec=1)
    res = asyncio.run(h.handle(_pending(), ctx={"instance_id": "i"}))
    assert not res.allow and res.reason == "slack_error"
    assert not res.hold                                  # 承認経路が無い＝保留ではなく deny
    assert [f for f in os.listdir(tmp) if f.endswith(".json")] == []   # 孤児 pending なし
