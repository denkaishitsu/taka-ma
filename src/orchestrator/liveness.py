"""sa-ru の死活監視 — 心拍 2 探針・プロセス内 daemon スレッドによる自己終了復帰（設計書 §8.16.1・ADR 0002）。

2026-09-04 18:46、sa-ru は全 8 ループが例外なく沈黙し、プロセスは生存していたため launchd
KeepAlive（終了時のみ再起動）も _supervise（例外時のみ再起動）も反応せず、翌朝の手動
kickstart まで復帰しなかった。本モジュールは「止まれば誰も気づかない」を廃する。

構成（§8.16 u-zu の Socket Mode 死活監視と同じ型）:
- Heartbeat.beat_loop（イベントループ上のコルーチン）: probe_interval_sec ごとに
  (a) 時刻を心拍ファイルへ上書き（イベントループ閉塞の検知）
  (b) to_thread へ空関数を投げ probe_timeout_sec で戻らなければ「スレッドプール枯渇」
      （9/4 の推定機序）を記録する
  ログは状態遷移時のみ（枯渇の検知・回復）。平常時は 0 行。
- run_watchdog（daemon スレッド）: check_interval_sec ごとに「健全な心拍（pool_ok）」の
  前進を見る。stale_threshold_sec を超えて途絶したら CRITICAL ログ後に os._exit(1) し、
  launchd KeepAlive に再起動させる。イベントループにもスレッドプールにも依存しない。
- 再起動の抑制: 自己終了の時刻を restart_count_path へ永続化し、直近 1 時間の回数が
  restart_limit_per_hour を超えたら自己終了せず CRITICAL と Slack 通知に切り替える
  （長時間の到達不能中に原因側の修正が不十分で再ハングしたときの往復を止める）。
  Slack 通知は初回の自己終了と打ち切り時のみ。MBP の最終疎通時刻を併記する。

運用値は sa-ru.yaml の liveness ブロックが唯一の源（コード側に既定値なし。キー欠落は
起動失敗）。既存の `heartbeat` ブロックは §10.8 の LLM 処理待ち進捗通知であり別物。
"""

import asyncio
import json
import logging
import os
import threading
import time

logger = logging.getLogger("sa-ru.liveness")

_REQUIRED_KEYS = ("probe_interval_sec", "probe_timeout_sec", "stale_threshold_sec",
                  "check_interval_sec", "heartbeat_path", "restart_count_path",
                  "restart_limit_per_hour")


def _atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


class Heartbeat:
    """心拍の発生源（コルーチン）と判定材料（最新の健全な心拍時刻）を持つ。"""

    def __init__(self, conf: dict, clock=time.monotonic, wall_clock=time.time):
        for k in _REQUIRED_KEYS:
            if k not in conf:
                raise KeyError(f"sa-ru.yaml liveness.{k} が未設定（唯一の源・既定値なし）")
        self.probe_interval = conf["probe_interval_sec"]
        self.probe_timeout = conf["probe_timeout_sec"]
        self.stale_threshold = conf["stale_threshold_sec"]
        self.check_interval = conf["check_interval_sec"]
        self.heartbeat_path = conf["heartbeat_path"]
        self.restart_count_path = conf["restart_count_path"]
        self.restart_limit = conf["restart_limit_per_hour"]
        self._clock = clock
        self._wall = wall_clock
        # 判定材料: 健全な心拍（イベントループが回り・スレッドプールが応答した）の最新時刻。
        # 起動時刻を起点にし、起動直後の閾値分は猶予（§8.16 と同じ誤検出耐性）
        self.last_ok: float = clock()
        self.pool_ok: bool | None = None
        self.beats: int = 0

    # ── 心拍（イベントループ上） ──

    async def beat_loop(self) -> None:
        """probe_interval_sec ごとに心拍を打つ常駐コルーチン（Orchestrator.run の gather に乗る）。"""
        logger.info("死活監視の心拍を開始（周期 %d 秒 / 途絶閾値 %d 秒 / プール探針上限 %d 秒）",
                    self.probe_interval, self.stale_threshold, self.probe_timeout)
        while True:
            await self.beat_once()
            await asyncio.sleep(self.probe_interval)

    async def beat_once(self) -> None:
        """心拍 1 回: プール探針 → 状態更新 → 心拍ファイル上書き。ログは状態遷移時のみ。"""
        pool_ok = await self._probe_thread_pool()
        now = self._clock()
        if pool_ok:
            self.last_ok = now
        if pool_ok != self.pool_ok:
            if pool_ok:
                if self.pool_ok is False:
                    logger.warning("死活監視: スレッドプールが回復（探針が %d 秒以内に応答）", self.probe_timeout)
            else:
                logger.error("死活監視: スレッドプール枯渇の疑い（空関数の探針が %d 秒で戻らない）。"
                             "健全な心拍を止める＝%d 秒続けば自己終了して再起動する",
                             self.probe_timeout, self.stale_threshold)
        self.pool_ok = pool_ok
        self.beats += 1
        self._write_heartbeat_file(pool_ok)

    async def _probe_thread_pool(self) -> bool:
        try:
            await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=self.probe_timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def _write_heartbeat_file(self, pool_ok: bool) -> None:
        """人と外部監視が読むための心拍ファイル（常に最新 1 行・増えない）。失敗は心拍を止めない。"""
        try:
            _atomic_write_text(self.heartbeat_path, json.dumps({
                "ts": round(self._wall(), 3), "pool_ok": pool_ok, "pid": os.getpid(),
                "beats": self.beats}) + "\n")
        except OSError as e:
            # 書けない事象は稀で、書けない旨の連投はログ規律に反する。初回のみ記録
            if not getattr(self, "_write_failed", False):
                logger.warning("死活監視: 心拍ファイルを書けません（%s）: %s", self.heartbeat_path, e)
                self._write_failed = True

    # ── 判定（daemon スレッド側から呼ぶ。イベントループ・プール非依存） ──

    def stale_seconds(self, now: float | None = None) -> float:
        return (self._clock() if now is None else now) - self.last_ok

    def is_stale(self, now: float | None = None) -> bool:
        return self.stale_seconds(now) > self.stale_threshold


class RestartLimiter:
    """自己終了の回数を永続化し、直近 1 時間の上限で往復を止める。"""

    WINDOW_SEC = 3600

    def __init__(self, path: str, limit_per_hour: int, wall_clock=time.time):
        self.path = path
        self.limit = limit_per_hour
        self._wall = wall_clock

    def _load(self) -> list[float]:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return [float(t) for t in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def recent(self) -> list[float]:
        now = self._wall()
        return [t for t in self._load() if now - t <= self.WINDOW_SEC]

    def record(self) -> int:
        """自己終了を 1 回記録し、直近 1 時間の回数（今回込み）を返す。"""
        stamps = self.recent() + [self._wall()]
        try:
            _atomic_write_text(self.path, json.dumps(stamps))
        except OSError as e:
            logger.warning("死活監視: 再起動回数を書けません（%s）: %s", self.path, e)
        return len(stamps)

    def exceeded(self) -> bool:
        return len(self.recent()) >= self.limit


def run_watchdog(hb: Heartbeat, limiter: RestartLimiter, *, notify=None,
                 reachability=None, exit_fn=os._exit, sleep=time.sleep,
                 once: bool = False) -> str | None:
    """心拍の途絶を検出したらプロセスごと終了する常駐ループ（daemon スレッド）。

    §8.16 と同じく回復はプロセス内で行わず、異常検出＝異常終了とし launchd KeepAlive に
    再起動させる。直近 1 時間の自己終了が上限に達していたら終了せず、打ち切りを 1 回だけ
    通知して監視を続ける（回復すれば通常に戻る）。

    Args:
        notify: Slack 通知（text -> None）。None なら通知しない。初回の自己終了と打ち切り時のみ呼ぶ。
        reachability: MBP 到達性の説明文を返す関数（通知に併記）。None なら省略。
        exit_fn / sleep / once: テスト注入（once=True は 1 巡だけ判定して結果を返す）。

    Returns:
        once=True のとき "ok" / "exit" / "cutoff"（それ以外は戻らない）。
    """
    cutoff_notified = False
    try:
        while True:
            if not once:
                sleep(hb.check_interval)
            if not hb.is_stale():
                cutoff_notified = False
                if once:
                    return "ok"
                continue
            stale = int(hb.stale_seconds())
            reach = f"\nMBP 到達性: {reachability()}" if reachability else ""
            if limiter.exceeded():
                if not cutoff_notified:
                    logger.critical(
                        "死活監視: 心拍途絶 %d 秒（閾値 %d 秒）だが、直近 1 時間の自己終了が %d 回に達した"
                        "ため自己終了を打ち切る（監視は継続）", stale, hb.stale_threshold, hb.restart_limit)
                    if notify:
                        _safe_notify(notify, (
                            f"⛔ sa-ru 死活監視: 心拍が {stale} 秒途絶。直近 1 時間の自己再起動が "
                            f"{hb.restart_limit} 回に達したため再起動を打ち切りました。手動確認が必要です"
                            f"（runbook『ハング時の手順』）。{reach}"))
                    cutoff_notified = True
                if once:
                    return "cutoff"
                continue
            count = limiter.record()
            logger.critical(
                "死活監視: 心拍途絶 %d 秒（閾値 %d 秒・直近 1 時間 %d 回目）。沈黙と判定し"
                "プロセスを終了する（launchd KeepAlive が再起動）", stale, hb.stale_threshold, count)
            if notify and count == 1:
                _safe_notify(notify, (
                    f"⚠️ sa-ru 死活監視: 心拍が {stale} 秒途絶したため自己終了し、launchd が再起動します"
                    f"（直近 1 時間 {count} 回目・上限 {hb.restart_limit} 回）。{reach}"))
            exit_fn(1)
            if once:
                return "exit"
    except Exception:
        # 監視スレッド自身の想定外死は「監視なしの常駐」（偽正常）を生むため fail-closed で落とす
        logger.critical("死活監視スレッドが例外で停止。fail-closed でプロセスを終了する", exc_info=True)
        exit_fn(1)
        return "exit"


def _safe_notify(notify, text: str) -> None:
    try:
        notify(text)
    except Exception:
        logger.exception("死活監視: Slack 通知に失敗（自己終了の判断には影響しない）")


def start_watchdog_thread(hb: Heartbeat, limiter: RestartLimiter, *, notify=None,
                          reachability=None) -> threading.Thread:
    t = threading.Thread(target=run_watchdog, args=(hb, limiter),
                         kwargs={"notify": notify, "reachability": reachability},
                         name="liveness-watchdog", daemon=True)
    t.start()
    logger.info("死活監視スレッドを開始（確認周期 %d 秒 / 途絶閾値 %d 秒 / 再起動上限 %d 回/時）",
                hb.check_interval, hb.stale_threshold, hb.restart_limit)
    return t
