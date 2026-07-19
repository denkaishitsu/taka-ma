"""u-zu（Slack Bot）エントリポイント — Socket Mode で常駐し Slack 操作を受ける。

公開ポートを開けず、Socket Mode（WebSocket）で Slack と双方向接続する常駐サービス。
起動時に .env を読み、スラッシュコマンド／イベント（メンション・DM）／ボタン操作の
3 系統のハンドラを App に登録してから接続を開始する。

構築手順書: docs/procedures/03-slack-bot.md
"""

import os
import logging
import sys
import threading
import time

import yaml
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

from handlers.commands import register_commands
from handlers.events import register_events
from handlers.actions import register_actions
from services.socket_watchdog import SocketWatchdog

# Slack/ワークスペース別のトークン等は launchd 環境ではなく配備先の .env に置く。
load_dotenv("/opt/taka-ma/config/.env")

# launchd 配下では stdout がそのままサービスログになるため、標準出力へ集約する。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("u-zu")

app = App(token=os.environ["SLACK_BOT_TOKEN"])


def _load_watchdog_config() -> tuple[float, float]:
    """u-zu.yaml（SSOT）から死活監視の運用値を読む。

    キー欠落・ファイル不在は起動時に例外で落とす（監視なしで常駐する偽正常を許さない。
    コード側に既定値を置かない #103 の方針）。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "u-zu.yaml")
    with open(path, encoding="utf-8") as f:
        watchdog = (yaml.safe_load(f) or {})["watchdog"]
    return float(watchdog["check_interval_sec"]), float(watchdog["stale_threshold_sec"])


def _run_watchdog(handler: SocketModeHandler, check_interval_sec: float, stale_threshold_sec: float) -> None:
    """Socket Mode の受信死（half-open／再接続ストーム）を検出したらプロセスごと終了する常駐ループ。

    再接続はプロセス内で行わない。slack_sdk は「再接続したつもりで受信が死んだまま」
    （2026-07-17 障害）や「再接続を繰り返して自力復帰しない」（E2E 実測）に陥るため、
    異常検出＝異常終了とし、まっさらな接続の張り直しを launchd（KeepAlive）の再起動に委ねる。
    """
    watchdog = SocketWatchdog(stale_threshold_sec)
    try:
        while True:
            time.sleep(check_interval_sec)
            session = getattr(handler.client, "current_session", None)
            if watchdog.is_stale(session):
                logger.critical(
                    "Socket Mode 死活監視: pong 途絶 %d 秒（閾値 %d 秒）。受信死と判定し"
                    "プロセスを終了する（launchd KeepAlive が再起動）",
                    int(watchdog.stale_seconds()), int(stale_threshold_sec),
                )
                os._exit(1)
    except Exception:
        # 監視スレッド自身の想定外死は「監視なしの常駐」（偽正常）を生むため、
        # fail-closed でプロセスごと落とし launchd の再起動に倒す。
        logger.critical("Socket Mode 死活監視スレッドが例外で停止。fail-closed でプロセスを終了する", exc_info=True)
        os._exit(1)

# 3 系統のハンドラを App に配線する（コマンド / イベント / インタラクティブ要素）。
register_commands(app)
register_events(app)
register_actions(app)

if __name__ == "__main__":
    # App トークンで Socket Mode 接続を確立し、以降は受信待ちのまま常駐する。
    logger.info("Slack Bot 起動 (Socket Mode)")
    # 運用値の読込は接続前に行い、設定不備を起動失敗として顕在化させる。
    check_interval_sec, stale_threshold_sec = _load_watchdog_config()
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    threading.Thread(
        target=_run_watchdog,
        args=(handler, check_interval_sec, stale_threshold_sec),
        name="socket-watchdog",
        daemon=True,
    ).start()
    logger.info(
        "Socket Mode 死活監視を開始（周期 %d 秒 / pong 途絶閾値 %d 秒）",
        int(check_interval_sec), int(stale_threshold_sec),
    )
    handler.start()
