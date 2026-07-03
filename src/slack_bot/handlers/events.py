"""イベントハンドラ — メンション・DM など発話系イベントを会話キューへ流す。

スラッシュコマンドやボタンと違い、通常の発話は「完成タスク」ではなく会話の 1 ターン。
ここでは即タスク化せず会話キューに置き、実行意図の判定・要約・着手確認は sa-ru の脳に委ねる
（§8.3 (A)）。

構築手順書: docs/procedures/03-slack-bot.md
"""

import logging

from services.conversation_queue import enqueue_conversation_message
from services.role_check import authorize

logger = logging.getLogger("u-zu.events")


def register_events(app):
    """app_mention / message イベントのハンドラを Bolt App に登録する。"""

    @app.event("app_mention")
    def handle_mention(event, say):
        """ボットへのメンション受信 — 認可後、発話を会話キューへ流す。"""
        user = event.get("user", "unknown")
        text = event.get("text", "")
        logger.info("メンション受信 from %s: %s", user, text)
        # 認可: 未登録ユーザーの命令は会話キューへ流さず拒否する（設計書 §1.2）。
        if not authorize(user, "user", say):
            return
        # 1 通＝完成タスクではなく会話キューへ流す。実行意図の判定・要約・着手確認は
        # sa-ru の脳が担う（§8.3 (A)）。team_id（event["team"]）で送信元 WS を記録する。
        enqueue_conversation_message(
            "slack_mention", text,
            user_id=user,
            team_id=event.get("team", ""),
            channel_id=event.get("channel", ""),
            thread_ts=event.get("thread_ts"),
        )

    @app.event("message")
    def handle_message(event, say, logger):
        """メッセージ受信 — DM は会話キューへ、それ以外のチャンネル発話はログのみ。"""
        # bot 自身の投稿・編集・参加通知など subtype 付きは人間の発話ではないので無視する。
        subtype = event.get("subtype")
        if subtype is not None:
            return

        channel_type = event.get("channel_type", "")
        user = event.get("user", "unknown")
        text = event.get("text", "")

        if channel_type == "im":
            # DM: 会話キューへ流す（即タスク化はしない）。
            logger.info("DM受信 from %s: %s", user, text)
            # 認可: 未登録ユーザーの DM 命令は拒否する（設計書 §1.2）。
            if not authorize(user, "user", say):
                return
            enqueue_conversation_message(
                "slack_dm", text,
                user_id=user,
                team_id=event.get("team", ""),
                channel_id=event.get("channel", ""),
                thread_ts=event.get("thread_ts"),
            )
        else:
            # チャンネルメッセージ: ログのみ
            logger.debug("メッセージ受信: %s", text)
