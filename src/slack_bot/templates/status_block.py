"""/taka-ma-status 用 — サービス・ollama の稼働状況の Block Kit テンプレート。"""

import subprocess
import logging

logger = logging.getLogger("u-zu.templates.status")

# 稼働確認する launchd サービス（launchctl label, 表示名）。
SERVICES = [
    ("com.taka-ma.sa-ru", "sa-ru"),
    ("com.taka-ma.ya-ta", "ya-ta"),
    ("com.taka-ma.u-zu", "u-zu"),
]


def _check_service(label: str) -> str:
    """launchctl list の終了コードでサービス稼働を判定し、表示用の状態文字列を返す。

    例外時は Stopped と断定せず Unknown を返す（launchctl 自体が呼べない等を取り違えない）。
    """
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return ":white_check_mark: Running"
        return ":x: Stopped"
    except Exception:
        return ":warning: Unknown"


def _check_ollama(host: str, name: str) -> str:
    """ollama プロセスの有無で稼働を判定する。host=="localhost" はローカル、他は SSH 越し。

    例外時は Unknown を返す（SSH 不達などを Stopped と混同しない）。
    """
    try:
        if host == "localhost":
            result = subprocess.run(
                ["pgrep", "-x", "ollama"],
                capture_output=True, text=True,
            )
        else:
            result = subprocess.run(
                ["ssh", host, "pgrep", "-x", "ollama"],
                capture_output=True, text=True, timeout=5,
            )
        if result.returncode == 0:
            return ":white_check_mark: Running"
        return ":x: Stopped"
    except Exception:
        return ":warning: Unknown"


def build_status_blocks() -> list:
    """launchd サービスと両ホストの ollama の稼働状況を集約し Block Kit で返す。"""
    fields = []

    # Mac mini サービス
    for label, name in SERVICES:
        status = _check_service(label)
        fields.append({"type": "mrkdwn", "text": f"*{name}:*\n{status}"})

    # ollama (Mac mini)
    fields.append({
        "type": "mrkdwn",
        "text": f"*ollama (mini):*\n{_check_ollama('localhost', 'mini')}",
    })

    # ollama (MBP)
    fields.append({
        "type": "mrkdwn",
        "text": f"*ollama (MBP):*\n{_check_ollama('mbp', 'MBP')}",
    })

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "System Status"},
        },
        {
            "type": "section",
            "fields": fields,
        },
    ]
