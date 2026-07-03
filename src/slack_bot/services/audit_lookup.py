"""file_audit jsonl レコードのルックアップ（A1 §4）。

audit_log_id でレコードを引き当てる。当日 + 前日の jsonl まで遡る（日跨ぎ Reject 対応）。

構築手順書: docs/procedures/03-slack-bot.md（audit_approve / audit_reject ハンドラから利用）
関連: 設計書 §8.12 / A1 §4
"""

import datetime
import json
import logging
import os

logger = logging.getLogger("u-zu.audit_lookup")

DEFAULT_LOG_DIR = "/opt/taka-ma/logs/file-audit"


def find_audit_record(audit_log_id: str, log_dir: str = DEFAULT_LOG_DIR) -> dict | None:
    """audit_log_id で当日・前日の jsonl からレコードを引き当てる。

    A1 §4 で「レコードは `id` フィールドを持ち Slack ボタン callback で特定」と確定。
    見つからなければ None を返す（呼出側で警告メッセージを返す）。
    """
    today = datetime.date.today()
    for offset in (0, 1):
        date = today - datetime.timedelta(days=offset)
        path = os.path.join(log_dir, f"file-audit-{date.isoformat()}.jsonl")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("id") == audit_log_id:
                        return rec
        except OSError:
            logger.exception("jsonl 読み込み失敗: %s", path)
    return None
