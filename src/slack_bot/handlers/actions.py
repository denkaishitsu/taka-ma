"""ボタン・インタラクティブ要素ハンドラ。

構築手順書: docs/procedures/03-slack-bot.md
関連: 設計書 §8.3 / §8.12 / A1 §3
"""

import logging

from services.approval_store import resolve_approval
from services.audit_approval import record_audit_approval
from services.audit_lookup import find_audit_record
from services.task_queue import enqueue_task
from services.exec_confirm import resolve_exec_confirm
from services.role_check import authorize

logger = logging.getLogger("u-zu.actions")


def _thread_ts(body: dict) -> str | None:
    """ボタンを含むメッセージのスレッド起点 ts を返す。

    Bolt の say はボタン押下ハンドラでも thread_ts を自動継承しない（明示しない限り
    常に通常投稿になる）。ボタンを含むメッセージ自身がスレッド内なら thread_ts、
    それ自身がスレッド起点なら ts を使う（events.py の thread_ts 解決と同じ方針。
    実機検証で「着手を承認しました」等の押下確認が通常投稿になる欠陥を確認・是正）。
    """
    message = body.get("message") or {}
    return message.get("thread_ts") or message.get("ts")


def _enqueue_audit_reject_task(record: dict, user: str, team_id: str) -> str:
    """file_audit Reject 押下を §8.3 経路でタスク投入する（§8.12「Reject（§8.3 経由・LLM あり）」）。

    Reject は revert（どう取り消すか）の判断に LLM を要するため §8.3 経路に投入し、
    ya-ta が分解して各主体に振り分ける（プロセス停止=sa-ru、revert=振り分け先 LLM）。
    Approve は判断が人により確定済みの定型処理のため §8.3 に乗せない（record_audit_approval）。
    """
    path = record["path"]
    command = (
        f"path '{path}' の変更を revert し、関連タスクのプロセスを停止すること"
    )
    return enqueue_task(
        "slack_action", command,
        user_id=user,
        team_id=team_id,
        channel_id=record.get("channel_id", ""),
        thread_ts=record.get("thread_ts"))


def register_actions(app):
    """ボタン（Tier3 承認・着手確認・file_audit 判定）のハンドラを Bolt App に登録する。

    各ハンドラは ack 直後に authorize で認可ゲートし、操作内容に応じて承認/確認ファイルの
    更新（§8.10）または §8.3 経路へのタスク投入（A1 §3）を行う。
    """

    @app.action("approve_action")
    def handle_approve(ack, body, say):
        """Tier 3 Approve 押下 — 承認ファイルを approved に更新（§8.10、sa-ru がポーリング検知）。"""
        ack()
        request_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        # 認可: Tier3 承認はスラッシュ版 /taka-ma-approve と同じ admin 要件（ボタン経由の迂回を塞ぐ）。
        if not authorize(user, "admin", say):
            return
        logger.info("承認: request_id=%s by user=%s", request_id, user)
        thread_ts = _thread_ts(body)
        if resolve_approval(request_id, "approved", user_id=user):
            say(f":white_check_mark: <@{user}> が承認しました (ID: {request_id})", thread_ts=thread_ts)
        else:
            say(f":warning: 承認できませんでした（既に処理済みか期限切れ） (ID: {request_id})", thread_ts=thread_ts)

    @app.action("reject_action")
    def handle_reject(ack, body, say):
        """Tier 3 Reject 押下 — 承認ファイルを rejected に更新（§8.10、sa-ru がポーリング検知）。"""
        ack()
        request_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        # 認可: Tier3 拒否も admin 要件（/taka-ma-approve と対称）。
        if not authorize(user, "admin", say):
            return
        logger.info("拒否: request_id=%s by user=%s", request_id, user)
        thread_ts = _thread_ts(body)
        if resolve_approval(request_id, "rejected", user_id=user):
            say(f":x: <@{user}> が拒否しました (ID: {request_id})", thread_ts=thread_ts)
        else:
            say(f":warning: 拒否できませんでした（既に処理済みか期限切れ） (ID: {request_id})", thread_ts=thread_ts)

    @app.action("exec_confirm")
    def handle_exec_confirm(ack, body, say):
        """着手ボタン押下 — 確認レコードを confirmed に更新する。

        sa-ru が会話から提示した「構造化要約」への人間の着手承認。sa-ru 側のループが
        confirmed を検知して確定タスク（status=init）を生成する（§8.3 (B)）。
        """
        ack()
        exec_request_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        # 認可: 着手確認はタスク投入相当。登録ユーザー（user 以上）のみ。
        if not authorize(user, "user", say):
            return
        logger.info("exec_confirm: id=%s by user=%s", exec_request_id, user)
        thread_ts = _thread_ts(body)
        if resolve_exec_confirm(exec_request_id, "confirmed", decided_by=user):
            say(f":rocket: <@{user}> が着手を承認しました", thread_ts=thread_ts)
        else:
            say(":warning: この確認は既に処理済みか、期限切れです", thread_ts=thread_ts)

    @app.action("exec_reject")
    def handle_exec_reject(ack, body, say):
        """やり直すボタン押下 — 確認レコードを rejected に更新する。

        実行はせず、sa-ru は会話を継続する（要約をやり直す）。
        """
        ack()
        exec_request_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        # 認可: やり直しもタスク投入相当。登録ユーザー（user 以上）のみ。
        if not authorize(user, "user", say):
            return
        logger.info("exec_reject: id=%s by user=%s", exec_request_id, user)
        thread_ts = _thread_ts(body)
        if resolve_exec_confirm(exec_request_id, "rejected", decided_by=user):
            say(f":arrows_counterclockwise: <@{user}> がやり直しを選びました。会話を続けてください", thread_ts=thread_ts)
        else:
            say(":warning: この確認は既に処理済みか、期限切れです", thread_ts=thread_ts)

    @app.action("audit_approve")
    def handle_audit_approve(ack, body, say):
        """file_audit Approve 押下 — LLM 非経由の定型処理で承認証跡へ記録（§8.12）。

        Approve は人が「問題ない」と判断を確定した操作であり、§8.3 の worker LLM 経路には
        乗せない（乗せると LLM が指示文を再解釈し思考ダンプを出力する。実機確認済み）。
        audit_log_id でアラートレコードを引き当て、承認済みマークを機械的に記録する。
        """
        ack()
        audit_log_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        # 認可: file_audit の承認/却下は実行系（revert・プロセス停止）を起こすため admin 要件。
        if not authorize(user, "admin", say):
            return
        logger.info("audit_approve: id=%s by user=%s", audit_log_id, user)
        thread_ts = _thread_ts(body)

        record = find_audit_record(audit_log_id)
        if record is None:
            say(f":warning: audit_log_id={audit_log_id} のレコードが見つかりません", thread_ts=thread_ts)
            return
        record_audit_approval(audit_log_id, record, user)
        say(f":white_check_mark: <@{user}> が approve しました (id: {audit_log_id})", thread_ts=thread_ts)

    @app.action("audit_reject")
    def handle_audit_reject(ack, body, say):
        """file_audit Reject 押下 — §8.3 経路で revert タスクを投入（A1 §3）。

        プロセス停止は sa-ru の process_manager、revert は ya-ta が振り分けた LLM（A1 §3.1）。
        """
        ack()
        audit_log_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        team_id = body.get("team", {}).get("id", "")   # ボタン押下元のワークスペース
        # 認可: file_audit の承認/却下は実行系（revert・プロセス停止）を起こすため admin 要件。
        if not authorize(user, "admin", say):
            return
        logger.info("audit_reject: id=%s by user=%s", audit_log_id, user)
        thread_ts = _thread_ts(body)

        record = find_audit_record(audit_log_id)
        if record is None:
            say(f":warning: audit_log_id={audit_log_id} のレコードが見つかりません", thread_ts=thread_ts)
            return
        _enqueue_audit_reject_task(record, user, team_id)
        say(f":x: <@{user}> が reject しました (id: {audit_log_id})", thread_ts=thread_ts)
