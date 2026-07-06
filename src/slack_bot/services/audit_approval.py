"""file_audit Approve の定型記録（設計書 §8.12「Approve（定型処理・LLM 非経由）」）。

Approve は「この変更は問題ない」と人が確定する操作であり、判断は既に人が下している。
したがって自然言語コマンドとして worker LLM に投げず（投げると LLM が指示文を再解釈し
思考ダンプを出力するなどの逸脱が起きる。実機確認済み）、u-zu が audit_log_id で
アラートレコードを引き当て、承認済みマークを機械的に jsonl へ追記する。

参照元アラートは Mac mini ローカル（find_audit_record が引く）で、本記録も Mac mini
ローカルの jsonl に残す。qu-e の監査 jsonl（MBP・変更検知の証跡）とは別ファイルで、
「誰がいつ何を承認したか」の承認証跡を担う。

構築手順書: docs/procedures/03-slack-bot.md（audit_approve ハンドラから利用）
"""

import datetime
import json
import logging
import os

logger = logging.getLogger("u-zu.audit_approval")

# 承認証跡の出力先（Mac mini ローカル。file_audit の監査 jsonl とは別ファイル）。
DEFAULT_APPROVAL_LOG = os.environ.get(
    "TAKA_MA_FILE_AUDIT_APPROVAL_LOG",
    "/opt/taka-ma/logs/file-audit/file-audit-approvals.jsonl")


def record_audit_approval(audit_log_id: str, record: dict, user_id: str,
                          approval_log: str = DEFAULT_APPROVAL_LOG) -> bool:
    """Approve の事実を承認証跡 jsonl に 1 行追記する（LLM 非経由の定型処理）。

    record は find_audit_record が引き当てたアラート JSON。識別・突合キー（audit_log_id /
    path / task_id）と承認者・時刻を記録する。1 行を O_APPEND で単一 write するため、
    複数押下が競合しても行が混ざらない（追記の原子性）。成功で True。
    """
    entry = {
        "audit_log_id": audit_log_id,
        "path": record.get("path", ""),
        "task_id": record.get("task_id", ""),
        "decision": "approved",
        "decided_by": user_id,
        "decided_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    # ベア名（ディレクトリ成分なし）だと dirname="" で makedirs が落ちるため "." に倒す。
    os.makedirs(os.path.dirname(approval_log) or ".", exist_ok=True)
    # O_APPEND は書き込みごとにファイル末尾へ atomic に seek+write するため、
    # 1 行を 1 回の write で出せば並行追記でも行が破損しない。
    fd = os.open(approval_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    logger.info("audit approval 記録: id=%s path=%s by=%s",
                audit_log_id, entry["path"], user_id)
    return True
