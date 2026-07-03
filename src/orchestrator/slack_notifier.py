"""sa-ru から Slack にメッセージを送信する。

構築手順書: docs/procedures/05-orchestrator.md Step 7（Slack 通知実装）
"""

import os
import logging

from slack_sdk import WebClient

logger = logging.getLogger("sa-ru.slack")


class SlackNotifier:
    """sa-ru から Slack にメッセージを送信する。

    複数ワークスペース運用では、応答を送信元ワークスペース（タスクの `team_id`）へ
    返すため、`team_id` ごとに bot トークンとデフォルトチャンネルを切り替える
    （設計書 §8.3 / 構築手順書 03 の「3-4 複数ワークスペース運用時のトークン登録」）。
    """

    def __init__(self):
        """既定トークン/チャンネルを .env から読み、クライアントキャッシュを空で初期化する。"""
        # 既定の bot トークン・チャンネルは .env から取得（team_id 未指定時のフォールバック）
        from dotenv import load_dotenv
        load_dotenv("/opt/taka-ma/config/.env")
        self._default_token = os.environ["SLACK_BOT_TOKEN"]
        self.default_channel = os.environ["SLACK_CHANNEL_ID"]
        # team_id → WebClient のキャッシュ。キー "" は team_id 未指定（既定トークン）。
        self._clients: dict[str, WebClient] = {}

    def _client_for(self, team_id: str | None) -> WebClient:
        """team_id に対応する WebClient を返す。

        `SLACK_BOT_TOKEN_<TEAM_ID>` が登録されていればそのトークンで、無ければ
        既定トークン（`SLACK_BOT_TOKEN`）で送信する。生成済みクライアントは再利用する。
        """
        key = team_id or ""
        if key not in self._clients:
            token = os.environ.get(f"SLACK_BOT_TOKEN_{team_id}") if team_id else None
            self._clients[key] = WebClient(token=token or self._default_token)
        return self._clients[key]

    def _channel_for(self, team_id: str | None, channel: str | None) -> str:
        """送信先チャンネルを決める。

        明示 channel が最優先。未指定なら team_id ごとの既定チャンネル
        （`SLACK_CHANNEL_ID_<TEAM_ID>`）、それも無ければシステム既定チャンネル。
        """
        if channel:
            return channel
        if team_id:
            ws_default = os.environ.get(f"SLACK_CHANNEL_ID_{team_id}")
            if ws_default:
                return ws_default
        return self.default_channel

    def notify(self, text: str, channel: str | None = None, team_id: str | None = None,
               thread_ts: str | None = None):
        """送信元ワークスペース（team_id）の送信元 channel_id に結果を返す。未指定時はデフォルト。

        thread_ts を渡すと当該スレッドへ返信する（会話フロントエンドの返信に使う）。
        """
        target = self._channel_for(team_id, channel)
        try:
            self._client_for(team_id).chat_postMessage(channel=target, text=text, thread_ts=thread_ts)
            logger.info("Slack通知送信 (ws=%s to %s): %s", team_id or "default", target, text[:80])
        except Exception:
            logger.exception("Slack通知送信失敗")

    def send_exec_confirm_request(self, exec_request_id: str, summary: str,
                                  channel: str | None = None,
                                  team_id: str | None = None,
                                  thread_ts: str | None = None):
        """会話から固まった意図の要約 + 着手/やり直すボタンを送信する（§8.3 (B)）。

        ボタンは u-zu の `exec_confirm` / `exec_reject` ハンドラで受信し、確認レコードの
        status を更新する。sa-ru 側ループが confirmed を検知して確定タスクを生成する。
        Block 構築は send_approval_request / send_file_audit_alert と同様にインラインで持つ
        （u-zu 側テンプレートは別プロセス・別パッケージのため import しない）。
        """
        target = self._channel_for(team_id, channel)
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "📝 この内容で着手します"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*要約:*\n{summary}"}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "着手"},
                 "style": "primary", "action_id": "exec_confirm", "value": exec_request_id},
                {"type": "button", "text": {"type": "plain_text", "text": "やり直す"},
                 "action_id": "exec_reject", "value": exec_request_id},
            ]},
        ]
        self._client_for(team_id).chat_postMessage(
            channel=target, thread_ts=thread_ts,
            text="この内容で着手します（着手 / やり直す）", blocks=blocks,
        )

    def send_approval_request(self, request_id: str, command: str,
                              instance_id: str, risk_reason: str,
                              context: str = "",
                              channel: str | None = None,
                              team_id: str | None = None):
        """Tier 3 承認リクエストを Block Kit 付きで送信する。

        context（worker stdout の前後文脈）があれば承認者が「何を実行しようとしているか」を
        判断できるよう本文に併記する。command が "unknown" になる場面でも文脈で補えるようにする。
        """
        target = self._channel_for(team_id, channel)
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "⚠ Tier 3 承認リクエスト"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Instance:*\n{instance_id}"},
                {"type": "mrkdwn", "text": f"*Command:*\n`{command}`"},
                {"type": "mrkdwn", "text": f"*Risk:*\n{risk_reason}"},
                {"type": "mrkdwn", "text": f"*Request ID:*\n{request_id}"},
            ]},
        ]
        if context:
            # Slack section の文字数上限を避けるため末尾 800 字程度に丸める。
            snippet = context[-800:]
            blocks.append({"type": "section", "text": {
                "type": "mrkdwn", "text": f"*Context:*\n```{snippet}```",
            }})
        blocks += [
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
                 "style": "primary", "action_id": "approve_action", "value": request_id},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
                 "style": "danger", "action_id": "reject_action", "value": request_id},
            ]},
        ]
        self._client_for(team_id).chat_postMessage(channel=target, text=f"Tier 3 承認: {command}", blocks=blocks)

    def send_file_audit_alert(self, alert: dict):
        """file_audit アラートを Block Kit (Approve/Reject ボタン付き) で送信する（§8.12）。

        タスク実行中なら thread_ts に Thread 投稿、非実行中なら別投稿。
        callback は u-zu (slack_bot) 側で `audit_approve` / `audit_reject` の action_id で受信。
        """
        team_id = alert.get("team_id")
        target = self._channel_for(team_id, alert.get("channel_id"))
        thread_ts = alert.get("thread_ts")
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": "ファイル変更検知"}},
            # A1 §2 通知ペイロード順: 識別子 → 対象 → 判定 → 根拠 → コンテキスト
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*audit_log_id:*\n{alert['audit_log_id']}"},
                {"type": "mrkdwn", "text": f"*task_id:*\n{alert.get('task_id', '')}"},
                {"type": "mrkdwn", "text": f"*path:*\n`{alert['path']}`"},
                {"type": "mrkdwn", "text": f"*decision:*\n{alert['decision']}"},
                {"type": "mrkdwn", "text": f"*reason:*\n{alert['reason']}"},
                {"type": "mrkdwn", "text": f"*confidence:*\n{alert.get('confidence', '')}"},
                {"type": "mrkdwn", "text": f"*command:*\n{alert.get('command', '')}"},
                {"type": "mrkdwn", "text": f"*status:*\n{alert.get('status', 'none')}"},
            ]},
            # A1 §2「diff サマリ」必須項目
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*diff:*\n```{alert.get('diff_summary', '')}```",
            }},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
                 "style": "primary", "action_id": "audit_approve", "value": alert["audit_log_id"]},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
                 "style": "danger", "action_id": "audit_reject", "value": alert["audit_log_id"]},
            ]},
        ]
        self._client_for(team_id).chat_postMessage(
            channel=target, thread_ts=thread_ts,
            text=f"ファイル変更検知 [{alert['decision']}] {alert['path']}",
            blocks=blocks,
        )
