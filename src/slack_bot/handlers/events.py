"""イベントハンドラ — メンション・DM など発話系イベントを会話キューへ流す。

スラッシュコマンドやボタンと違い、通常の発話は「完成タスク」ではなく会話の 1 ターン。
ここでは即タスク化せず会話キューに置き、実行意図の判定・要約・着手確認は sa-ru の脳に委ねる
（§8.3 (A)）。

構築手順書: docs/procedures/03-slack-bot.md
"""

import logging

from services.conversation_queue import (
    derive_conversation_id,
    enqueue_conversation_message,
)
from services.event_dedup import seen_before
from services.inbound_sanitize import load_sanitize_apps, strip_app_attribution
from services.role_check import authorize, check_role
from services.session_lookup import is_awaiting_reply

logger = logging.getLogger("u-zu.events")


def _ack_received(client, channel: str, ts: str):
    """受信直後に元発話へ 👀 リアクションを付け、一次応答（受付済み）を即座に示す。

    sa-ru の脳（会話ループ、2秒ポーリング + LLM 判定）が要約/着手確認を返すまでには
    数秒〜数十秒かかり、その間ユーザーには「受け付けられたか分からない」無反応区間が
    生じる（実運用フィードバックで指摘）。テキスト返信ではなくリアクション
    にするのは、実際の応答本文（会話返信・要約）と競合させず、スレッドを汚さないため。
    リアクション自体の失敗（scope 不足・既に付与済み等）は一次応答の欠落であり本処理を
    止める理由にならないため、例外は握って続行する。
    """
    try:
        client.reactions_add(channel=channel, timestamp=ts, name="eyes")
    except Exception:
        logger.exception("受付リアクション付与に失敗: channel=%s ts=%s", channel, ts)


def register_events(app):
    """app_mention / message イベントのハンドラを Bolt App に登録する。"""
    # 正規化対象アプリの定義は起動時に 1 度だけ読む（発話ごとに yaml を開かない）。
    # キー欠落はここで例外になり、正規化なしのまま常駐する偽正常を防ぐ（§8.3）。
    sanitize_apps = load_sanitize_apps()

    @app.event("app_mention")
    def handle_mention(event, body, say, client):
        """ボットへのメンション受信 — 認可後、一次応答を示してから発話を会話キューへ流す。"""
        # Slack の再送（同一 event_id）は無視する。会話イベントは ack で止まらないため、
        # ここで冪等化しないと同一発話が複数ターン会話へ二重投入される（§8.3 再送の冪等化）。
        event_id = body.get("event_id", "")
        if seen_before(event_id):
            logger.info("再送メンションを無視: event_id=%s", event_id)
            return
        user = event.get("user", "unknown")
        # 搬送路が混ぜた情報はここで落とす。ログ・認可・受付リアクション・キュー投入の
        # すべてより前に効かせ、以降の経路は原文だけを扱う（§8.3 上り発話の正規化）。
        text = strip_app_attribution(event.get("text", ""), event.get("app_id"), sanitize_apps)
        logger.info("メンション受信 from %s: %s", user, text)
        # 認可: 未登録ユーザーの命令は会話キューへ流さず拒否する（設計書 §1.2）。
        if not authorize(user, "user", say):
            return
        _ack_received(client, event.get("channel", ""), event.get("ts", ""))
        # 1 通＝完成タスクではなく会話キューへ流す。実行意図の判定・要約・着手確認は
        # sa-ru の脳が担う（§8.3 (A)）。team_id（event["team"]）で送信元 WS を記録する。
        # thread_ts: 既存スレッド内の発話ならそれを継続、フラットな新規メンションなら
        # 自身の ts をスレッド起点にする（未指定のままだと sa-ru の返信が通常投稿になり、
        # conversation_id のスレッド単位分離＝設計書 §8.3 が機能しない。実機確認）。
        enqueue_conversation_message(
            "slack_mention", text,
            user_id=user,
            team_id=event.get("team", ""),
            channel_id=event.get("channel", ""),
            thread_ts=event.get("thread_ts") or event.get("ts"))

    @app.event("message")
    def handle_message(event, body, say, logger, client, context=None):
        """メッセージ受信 — DM は会話キューへ、チャンネルは B-ID メンション/返答待ち
        スレッド返信を能動受理し、それ以外のスレッド返信は passive 記録する（§8.3）。"""
        # bot 自身の投稿・編集・参加通知など subtype 付きは人間の発話ではないので無視する。
        subtype = event.get("subtype")
        if subtype is not None:
            return

        channel_type = event.get("channel_type", "")
        user = event.get("user", "unknown")
        # メンション経路と同じく、会話キューへ入る前に搬送路由来の混入を落とす（§8.3）。
        text = strip_app_attribution(event.get("text", ""), event.get("app_id"), sanitize_apps)

        if channel_type == "im":
            # Slack の再送（同一 event_id）は無視する（§8.3 再送の冪等化）。DM の会話投入も
            # ack で止まらないため、冪等化しないと同一発話が複数ターン会話へ二重投入される。
            event_id = body.get("event_id", "")
            if seen_before(event_id):
                logger.info("再送 DM を無視: event_id=%s", event_id)
                return
            # DM: 会話キューへ流す（即タスク化はしない）。
            logger.info("DM受信 from %s: %s", user, text)
            # 認可: 未登録ユーザーの DM 命令は拒否する（設計書 §1.2）。
            if not authorize(user, "user", say):
                return
            _ack_received(client, event.get("channel", ""), event.get("ts", ""))
            enqueue_conversation_message(
                "slack_dm", text,
                user_id=user,
                team_id=event.get("team", ""),
                channel_id=event.get("channel", ""),
                thread_ts=event.get("thread_ts") or event.get("ts"))
        else:
            # チャンネル発話。bot 投稿（bot_id 付き）は能動・passive とも対象外
            # （bot どうしの応答ループ・自己記録を作らない）
            thread_ts = event.get("thread_ts")
            if event.get("bot_id"):
                logger.debug("bot 投稿を無視: %s", text)
                return
            # ユーザー ID（<@U…>）メンションは app_mention ハンドラが能動ターンとして
            # 処理する（message イベントと二重配信されるため、ここで拾うと二重投入される）
            auths = body.get("authorizations") or [{}]
            bot_user = auths[0].get("user_id", "")
            if bot_user and f"<@{bot_user}>" in text:
                return
            # アプリ ID（<@B…>）メンションの能動受理（§8.3 (A)）。B-ID メンションでは
            # app_mention イベントが発火せず message しか届かないため、ここで通常メンション
            # と同一の経路（冪等化 → 認可 → 受付リアクション → slack_mention 投入）に乗せる。
            # 従来は passive 記録へ落ちて黙殺されていた（2026-08-25 実障害）
            bot_id = (context or {}).get("bot_id", "")
            if bot_id and f"<@{bot_id}>" in text:
                event_id = body.get("event_id", "")
                if seen_before(event_id):
                    logger.info("再送 B-ID メンションを無視: event_id=%s", event_id)
                    return
                logger.info("B-ID メンション受信 from %s: %s", user, text)
                if not authorize(user, "user", say):
                    return
                _ack_received(client, event.get("channel", ""), event.get("ts", ""))
                enqueue_conversation_message(
                    "slack_mention", text,
                    user_id=user,
                    team_id=event.get("team", ""),
                    channel_id=event.get("channel", ""),
                    thread_ts=thread_ts or event.get("ts"))
                return
            # スレッド外の非メンション発話は対象外（bot が関与しない雑談を収集しない）
            if not thread_ts:
                logger.debug("メッセージ受信: %s", text)
                return
            # Slack の再送（同一 event_id）は無視する（§8.3 再送の冪等化）
            event_id = body.get("event_id", "")
            if seen_before(event_id):
                return
            # 未認可ユーザーは黙って捨てる（bot が呼ばれていない場で拒否メッセージを
            # 自発しない・§8.3 (C)）。受付リアクションも付けない（応答しない発話のため）
            if not check_role(user, "user"):
                return
            # 返答待ちスレッドの能動昇格（§8.3 (C)）: taka-ma が質問・確認を出して返答を
            # 待っている会話では、メンション無しの返信も能動ターンとして受理する
            # （メンションを強いると返答が passive へ落ちて黙殺される・2026-08-25 実障害）。
            # awaiting_reply は sa-ru がセッション永続化ファイルへ書く（u-zu は読むだけ）
            cid = derive_conversation_id(
                team_id=event.get("team", ""), channel_id=event.get("channel", ""),
                thread_ts=thread_ts, user_id=user)
            if is_awaiting_reply(cid):
                logger.info("返答待ちスレッドの返信を能動受理: channel=%s thread=%s",
                            event.get("channel", ""), thread_ts)
                _ack_received(client, event.get("channel", ""), event.get("ts", ""))
                enqueue_conversation_message(
                    "slack_thread_reply", text,
                    user_id=user,
                    team_id=event.get("team", ""),
                    channel_id=event.get("channel", ""),
                    thread_ts=thread_ts)
                return
            logger.info("スレッド内非メンション発話を文脈記録: channel=%s thread=%s",
                        event.get("channel", ""), thread_ts)
            enqueue_conversation_message(
                "slack_thread_passive", text,
                user_id=user,
                team_id=event.get("team", ""),
                channel_id=event.get("channel", ""),
                thread_ts=thread_ts, passive=True)
