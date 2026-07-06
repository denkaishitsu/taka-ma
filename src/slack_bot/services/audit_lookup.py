"""file_audit 承認レコードのルックアップ（設計書 §8.12「承認レコードの参照元」）。

audit_log_id で承認に必要なレコードを引き当てる。

参照元はローカル（Mac mini）の **file_audit アラート JSON** である。qu-e が書く監査
jsonl は MBP ローカルの監査証跡であり、u-zu（Mac mini）からは SSH 無しには読めない。
そこで、qu-e が sa-ru へ push 済みのアラート（承認に必要な全フィールドを含む）を唯一の
参照元とする。sa-ru の FileAuditHandler は着信アラートを Slack 転送後 `done/` へ退避する
ため、以下の順で引き当てる:

1. `{alert_dir}/done/{audit_log_id}.json`（Slack 転送済み。通常はこちら）
2. `{alert_dir}/{audit_log_id}.json`（転送直前の未退避。押下との競合時の保険）

見つからなければ None を返す（呼出側で警告メッセージを返す）。

構築手順書: docs/procedures/03-slack-bot.md（audit_approve / audit_reject ハンドラから利用）
関連: 設計書 §8.12
"""

import json
import logging
import os
import re

logger = logging.getLogger("u-zu.audit_lookup")

# audit_log_id は qu-e が採番する uuid4().hex（16進 32 桁）。ここではファイル名の一部として
# パスに組み込むため、区切り・親参照（`/` や `..`）を含む値でパストラバーサル読み取りに
# 化けないよう、英数字・ハイフン・アンダースコアのみを許可する（想定外の値は None に倒す）。
_AUDIT_ID_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# sa-ru の file_audit アラート受信先（Mac mini ローカル。sa-ru.yaml file_audit.alert_dir と一致）。
# u-zu は sa-ru と同一ホストのためローカル open で到達する（設計書 §8.12「承認レコードの参照元」）。
DEFAULT_ALERT_DIR = os.environ.get(
    "TAKA_MA_FILE_AUDIT_ALERT_DIR", "/opt/taka-ma/data/file-audit-alerts")


def find_audit_record(audit_log_id: str, alert_dir: str = DEFAULT_ALERT_DIR) -> dict | None:
    """audit_log_id で承認レコード（= push 済みアラート JSON）を引き当てる。

    Slack ボタンの value に載る `audit_log_id` は、そのままアラートファイル名
    `{audit_log_id}.json` に対応する。転送後の `done/` を先に見て、無ければ未退避の
    受信直下を見る。どちらにも無ければ None。

    `audit_log_id` が想定形式（英数字・ハイフン・アンダースコアのみ）でない場合は、
    パストラバーサルを避けるため引き当てを行わず None を返す。
    """
    # 不正な id（区切り・親参照を含む等）はパスに組み込まず即 None（トラバーサル防止）。
    if not audit_log_id or not _AUDIT_ID_RE.match(audit_log_id):
        logger.warning("不正な audit_log_id を拒否: %r", audit_log_id)
        return None
    # done/（転送済み）→ 受信直下（未退避）の順で探す。ファイル名は audit_log_id 一致で確定。
    for candidate in (
        os.path.join(alert_dir, "done", f"{audit_log_id}.json"),
        os.path.join(alert_dir, f"{audit_log_id}.json"),
    ):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            # 破損・読取失敗は原因追跡のため記録し、次候補へ（見つからなければ最終的に None）
            logger.exception("アラート JSON 読み込み失敗: %s", candidate)
    return None
