"""event_dedup 単体テスト — Slack イベント再送の重複排除（Task #89 H13）。

同一 event_id の再配信を 1 度目だけ通し、2 度目以降は重複として弾く。
空 event_id は重複判定できないため常に通す（＝処理する）。

構築手順書: docs/procedures/03-slack-bot.md
"""

from services import event_dedup


def _fresh(monkeypatch):
    """モジュール大域の seen セットを空にしてからテストする（テスト間の汚染防止）。"""
    monkeypatch.setattr(event_dedup, "_seen", {})


def test_first_seen_false_resend_true(monkeypatch):
    _fresh(monkeypatch)
    assert event_dedup.seen_before("Ev1") is False   # 初見は通す
    assert event_dedup.seen_before("Ev1") is True    # 再送は弾く


def test_distinct_ids_independent(monkeypatch):
    _fresh(monkeypatch)
    assert event_dedup.seen_before("Ev1") is False
    assert event_dedup.seen_before("Ev2") is False   # 別 id は独立に通す


def test_empty_id_always_processed(monkeypatch):
    _fresh(monkeypatch)
    # 空 event_id は重複判定不能 → 毎回 False（処理する）
    assert event_dedup.seen_before("") is False
    assert event_dedup.seen_before("") is False


def test_ttl_sweep_evicts_expired(monkeypatch):
    _fresh(monkeypatch)
    # TTL を 0 にすると、次回呼び出しの掃除で既存エントリが落ち、同 id が再び初見扱いになる
    event_dedup.seen_before("Ev1")
    monkeypatch.setattr(event_dedup, "_TTL_SECONDS", -1)
    assert event_dedup.seen_before("Ev2") is False
    assert event_dedup.seen_before("Ev1") is False   # 掃除済みなので初見に戻る
