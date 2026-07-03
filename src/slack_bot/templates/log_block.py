"""/taka-ma-logs 用 — 各サービスログ末尾の Block Kit テンプレート。"""

import os
import logging

logger = logging.getLogger("u-zu.templates.log")

# 表示対象ログ（path, 表示名）。3 コンポーネントは u-zu と同居なのでローカルファイルを直読みする。
LOG_FILES = [
    ("/opt/taka-ma/logs/sa-ru.log", "sa-ru"),
    ("/opt/taka-ma/logs/ya-ta.log", "ya-ta"),
    ("/opt/taka-ma/logs/u-zu.log", "u-zu"),
]

# 各ログから表示する末尾行数。
TAIL_LINES = 20


def build_log_blocks() -> list:
    """各サービスログの末尾を読み、Block Kit セクションの一覧を組み立てて返す。

    ファイル不在・読み取り失敗もブロックに明示し、1 つ落ちても他のログ表示は止めない。
    Slack の section テキスト上限に収めるため、末尾を約 2900 文字に切り詰める。
    """
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Recent Logs"},
        },
    ]

    for path, name in LOG_FILES:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    lines = f.readlines()
                tail = "".join(lines[-TAIL_LINES:])
                # Slack の 1 section テキスト上限（約 3000 字）に収める。コードブロック装飾分の
                # 余白を残して末尾優先で切り詰める（直近ログを見せたいので先頭側を捨てる）。
                if len(tail) > 2900:
                    tail = tail[-2900:]
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{name}* (`{path}`):\n```\n{tail}\n```",
                    },
                })
            except Exception:
                logger.exception("ログ読み取り失敗: %s", path)
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{name}*: :x: 読み取り失敗"},
                })
        else:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{name}*: ファイルなし"},
            })

    return blocks
