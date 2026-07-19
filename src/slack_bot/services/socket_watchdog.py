"""Socket Mode の受信死検出 — pong 受信時刻の「前進」の途絶判定。

Socket Mode の WebSocket は、ネットワーク瞬断後に受信だけが死んだまま復帰しない
ことがある（2026-07-17 障害）。E2E の障害注入で、その死に方が 2 形態あることを実測した:

- half-open: 確立済み接続で送信は成功し続け、受信（pong）だけが止まる。slack_sdk の
  死活チェックはソケットへの書込みで判定するため検出できない（本障害。DM を 4 時間
  43 分取りこぼした）
- 再接続ストーム: クライアント内部状態が壊れ、約 30 秒ごとに新セッションを作っては
  即 BrokenPipe で閉じるループに陥り、ネットワーク回復後も自力復帰しない（E2E 実測。
  1 時間継続を確認）

受信が生きている唯一の証拠は「最後に受信した pong の時刻」（slack_sdk builtin
Connection.last_ping_pong_time。配備先 3.42.0 に存在を実機確認済み）が**前進する**こと。
セッションの入替は前進と見なさない（ストームでは入替が延々続くため、入替を猶予の
起点にすると永遠に検出できない）。本モジュールは判定だけを持ち、回復（プロセス終了
→ launchd KeepAlive 再起動）は呼び出し側 main.py が行う。

構築手順書: docs/procedures/03-slack-bot.md
"""

import time


class SocketWatchdog:
    """pong 受信時刻の前進が閾値を超えて止まったら異常と判定する状態機械。

    Slack とは数十秒間隔で ping/pong を交わすため、正常なら閾値（分単位）の間に
    必ず前進する。起動直後は初回チェック時刻を起点に閾値分の猶予を持つ。
    """

    def __init__(self, stale_threshold_sec: float):
        self._threshold = stale_threshold_sec
        self._last_progress: float | None = None

    def is_stale(self, session, now: float | None = None) -> bool:
        """pong の前進が閾値を超えて途絶していれば True。

        session には slack_sdk SocketModeClient.current_session（Connection）を渡す。
        None（未確立）や pong 未受信のセッションは前進なしとして扱う。
        """
        if now is None:
            now = time.time()
        if self._last_progress is None:
            # 初回チェック: ここから閾値分の猶予（起動直後の pong 未受信は正常）。
            self._last_progress = now
        last = getattr(session, "last_ping_pong_time", None) if session is not None else None
        if last is not None and last > self._last_progress:
            self._last_progress = last
        return (now - self._last_progress) > self._threshold

    def stale_seconds(self, now: float | None = None) -> float:
        """最後の前進からの経過秒。ログ用（is_stale の後に呼ぶ前提）。"""
        if now is None:
            now = time.time()
        if self._last_progress is None:
            return 0.0
        return now - self._last_progress
