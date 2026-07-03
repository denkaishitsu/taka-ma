"""§8.3 (B) 実行確認ゲート — 着手/やり直すボタン押下を確認レコードへ反映する。

新設。sa-ru が「構造化要約」を提示する際に確認レコード
（`{exec_request_id}.json`, status=pending）を作る。u-zu はユーザーのボタン押下を
受けて status を confirmed / rejected に更新するだけ（§8.10 の承認ファイル方式と同形）。
sa-ru 側のループが status 変化をポーリングで検知し、confirmed なら実行タスクを生成する。
"""

import datetime
import json
import os

# sa-ru と共有する確認レコードのディレクトリ（sa-ru.yaml の exec_confirm.dir と一致）。
EXEC_CONFIRM_DIR = "/opt/taka-ma/data/exec-confirmations"


def resolve_exec_confirm(exec_request_id: str, decision: str, *, decided_by: str) -> bool:
    """確認レコードの status を confirmed / rejected に更新する。

    decision: "confirmed"（着手）/ "rejected"（やり直す）。
    Returns: 更新できれば True。レコード不在・status が pending でない場合は False
      （二重押下や期限切れの取りこぼしを安全側に倒す）。
    """
    path = os.path.join(EXEC_CONFIRM_DIR, f"{exec_request_id}.json")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        record = json.load(f)
    if record.get("status") != "pending":
        return False
    record["status"] = decision
    record["decided_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    record["decided_by"] = decided_by
    with open(path, "w") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return True
