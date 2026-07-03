"""u-zu（Slack Bot）エントリポイント — Socket Mode で常駐し Slack 操作を受ける。

公開ポートを開けず、Socket Mode（WebSocket）で Slack と双方向接続する常駐サービス。
起動時に .env を読み、スラッシュコマンド／イベント（メンション・DM）／ボタン操作の
3 系統のハンドラを App に登録してから接続を開始する。

構築手順書: docs/procedures/03-slack-bot.md
"""

import os
import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from dotenv import load_dotenv

from handlers.commands import register_commands
from handlers.events import register_events
from handlers.actions import register_actions

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

# 3 系統のハンドラを App に配線する（コマンド / イベント / インタラクティブ要素）。
register_commands(app)
register_events(app)
register_actions(app)

if __name__ == "__main__":
    # App トークンで Socket Mode 接続を確立し、以降は受信待ちのまま常駐する。
    logger.info("Slack Bot 起動 (Socket Mode)")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()
