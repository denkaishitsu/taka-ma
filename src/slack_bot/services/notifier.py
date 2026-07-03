"""sa-ru -> Slack 通知送信"""

import os
import logging

from slack_sdk import WebClient

logger = logging.getLogger("u-zu.notifier")


class Notifier:
    """sa-ru からの通知を Slack に送信する。送信先はタスクの送信元 channel_id。

    複数ワークスペース運用では、応答を送信元ワークスペース（タスクの `team_id`）へ
    返すため、`team_id` ごとに bot トークンとデフォルトチャンネルを切り替える
    （設計書 §8.3 / 構築手順書 03 の「3-4 複数ワークスペース運用時のトークン登録」）。
    """

    def __init__(self):
        """既定トークン/チャンネルを環境変数から読み、クライアントキャッシュを空で初期化する。"""
        self._default_token = os.environ["SLACK_BOT_TOKEN"]
        self.default_channel = os.environ["SLACK_CHANNEL_ID"]
        # team_id → WebClient のキャッシュ。キー "" は team_id 未指定（既定トークン）。
        self._clients: dict[str, WebClient] = {}

    def _client_for(self, team_id: str | None) -> WebClient:
        """team_id に対応する WebClient を返す（未登録/未指定は既定トークン）。"""
        key = team_id or ""
        if key not in self._clients:
            token = os.environ.get(f"SLACK_BOT_TOKEN_{team_id}") if team_id else None
            self._clients[key] = WebClient(token=token or self._default_token)
        return self._clients[key]

    def _channel_for(self, team_id: str | None, channel: str | None) -> str:
        """送信先チャンネル決定。明示 channel 優先 → team_id 既定 → システム既定。"""
        if channel:
            return channel
        if team_id:
            ws_default = os.environ.get(f"SLACK_CHANNEL_ID_{team_id}")
            if ws_default:
                return ws_default
        return self.default_channel

    def send(self, text: str, channel: str | None = None, blocks: list | None = None,
             team_id: str | None = None):
        """テキストまたは Block Kit メッセージを送信する。
        channel 未指定時は team_id の既定、それも無ければシステム既定チャンネル（#taka-ma）に送信。
        """
        target = self._channel_for(team_id, channel)
        try:
            self._client_for(team_id).chat_postMessage(
                channel=target,
                text=text,
                blocks=blocks,
            )
            logger.info("通知送信 (ws=%s to %s): %s", team_id or "default", target, text[:80])
        except Exception:
            logger.exception("通知送信失敗")

    def send_approval_request(self, request_id: str, command: str,
                              instance_id: str, risk_reason: str,
                              channel: str | None = None,
                              team_id: str | None = None):
        """Tier 3 承認リクエストを Block Kit 付きで送信する。"""
        from templates.approval_block import build_approval_request
        blocks = build_approval_request(request_id, command, instance_id, risk_reason)
        self.send(
            text=f"Tier 3 承認リクエスト: {command} (ID: {request_id})",
            channel=channel,
            blocks=blocks,
            team_id=team_id,
        )
