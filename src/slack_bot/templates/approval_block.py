"""Tier 3 承認リクエストの Block Kit テンプレート（notifier から利用）。"""


def build_approval_request(request_id: str, command: str,
                           instance_id: str, risk_reason: str) -> list:
    """Tier 3 承認リクエストの Block Kit を組み立てる。

    Approve / Reject ボタンの value に request_id を載せ、押下時に actions ハンドラが
    どの承認かを特定できるようにする（action_id は handlers/actions.py と対応）。
    """
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": ":warning: Tier 3 承認リクエスト"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Instance:*\n{instance_id}"},
                {"type": "mrkdwn", "text": f"*Command:*\n`{command}`"},
                {"type": "mrkdwn", "text": f"*Risk:*\n{risk_reason}"},
                {"type": "mrkdwn", "text": f"*Request ID:*\n{request_id}"},
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve_action",
                    "value": request_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_action",
                    "value": request_id,
                },
            ],
        },
    ]
