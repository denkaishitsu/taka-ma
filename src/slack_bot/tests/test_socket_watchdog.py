"""SocketWatchdog（Socket Mode 受信死判定）の分離テスト。

時刻は now 引数で注入し、実時間に依存しない。session は last_ping_pong_time だけを
持つスタブで代替する（slack_sdk builtin Connection と同じ読み取り契約）。
"""

from services.socket_watchdog import SocketWatchdog

THRESHOLD = 180.0


class _Session:
    """Connection の読み取り契約（last_ping_pong_time 属性）だけを再現するスタブ。"""

    def __init__(self, last_ping_pong_time=None):
        self.last_ping_pong_time = last_ping_pong_time


def test_pong_advances_is_not_stale():
    """pong が閾値内に前進し続ける正常系では異常判定しない。"""
    watchdog = SocketWatchdog(THRESHOLD)
    session = _Session(last_ping_pong_time=1000.0)
    assert watchdog.is_stale(session, now=1000.0) is False
    session.last_ping_pong_time = 1100.0
    assert watchdog.is_stale(session, now=1160.0) is False
    session.last_ping_pong_time = 1200.0
    assert watchdog.is_stale(session, now=1260.0) is False


def test_pong_stall_beyond_threshold_is_stale():
    """同一セッションで pong が止まり閾値を超えたら異常（half-open。本障害の形態）。"""
    watchdog = SocketWatchdog(THRESHOLD)
    session = _Session(last_ping_pong_time=1000.0)
    watchdog.is_stale(session, now=1000.0)
    # 前進 1000.0 を基準に、閾値ちょうどまでは正常、超えたら異常。
    assert watchdog.is_stale(session, now=1000.0 + THRESHOLD) is False
    assert watchdog.is_stale(session, now=1000.0 + THRESHOLD + 1) is True


def test_session_replacement_is_not_progress():
    """セッション入替は前進ではない（再接続ストーム。E2E 実測の形態）。

    約 30 秒ごとに pong 未受信の新セッションへ入れ替わり続けても、最後の pong から
    閾値を超えれば異常と判定する。
    """
    watchdog = SocketWatchdog(THRESHOLD)
    watchdog.is_stale(_Session(last_ping_pong_time=1000.0), now=1000.0)
    for now in (1030.0, 1090.0, 1150.0, 1000.0 + THRESHOLD):  # 閾値内の入替は正常
        assert watchdog.is_stale(_Session(last_ping_pong_time=None), now=now) is False
    assert watchdog.is_stale(_Session(last_ping_pong_time=None), now=1000.0 + THRESHOLD + 1) is True


def test_replacement_then_pong_resumes_is_not_stale():
    """入替後の新セッションが pong を受ければ前進として正常継続。"""
    watchdog = SocketWatchdog(THRESHOLD)
    old = _Session(last_ping_pong_time=1000.0)
    watchdog.is_stale(old, now=1000.0)
    new = _Session(last_ping_pong_time=1150.0)  # 再接続後に pong 受信
    assert watchdog.is_stale(new, now=1160.0) is False
    assert watchdog.is_stale(new, now=1150.0 + THRESHOLD) is False
    assert watchdog.is_stale(new, now=1150.0 + THRESHOLD + 1) is True


def test_startup_grace_then_stale_without_any_pong():
    """起動から一度も pong を受けない場合、初回チェック起点の猶予を超えたら異常。"""
    watchdog = SocketWatchdog(THRESHOLD)
    assert watchdog.is_stale(None, now=1000.0) is False  # 初回チェック＝猶予起点
    assert watchdog.is_stale(None, now=1000.0 + THRESHOLD) is False
    assert watchdog.is_stale(None, now=1000.0 + THRESHOLD + 1) is True


def test_stale_seconds_reports_elapsed_since_progress():
    """stale_seconds はログ用に最後の前進からの経過秒を返す。"""
    watchdog = SocketWatchdog(THRESHOLD)
    session = _Session(last_ping_pong_time=1000.0)
    watchdog.is_stale(session, now=1000.0)
    watchdog.is_stale(session, now=1300.0)
    assert watchdog.stale_seconds(now=1300.0) == 300.0
