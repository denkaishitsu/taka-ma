"""監査ログ — 全 Tier 判定結果を jsonl に記録。

構築手順書: docs/procedures/08-approval-pipeline.md Step 5（監査ログ）
"""

import json
import datetime


class AuditLogger:
    """承認パイプラインの全判定結果を jsonl に追記する監査ロガー。

    Tier 0（安全性チェック）〜Tier 3（人間承認）まで、どの判定でも同形のレコードを 1 行 1 件で残し、
    後から「どのコマンドが・どの Tier で・どう裁定されたか」を追跡できるようにする（§3.5）。
    """

    def __init__(self, log_path: str = "/opt/taka-ma/logs/approval-audit.jsonl"):
        """出力先 jsonl のパスを保持する。

        Args:
            log_path: 監査ログの追記先。既定はパイプライン未設定時のフォールバック先で、
                通常は pipeline.yaml の audit.log_path（SSOT）が ApprovalPipeline から渡される。
        """
        self.log_path = log_path

    def log(self, entry: dict):
        """1 件の判定結果を当該 jsonl に 1 行追記する。

        呼び出し側が渡すキーの揺れに強くするため、各フィールドは get で取り出し、
        欠落時は None / 空文字に倒す。記録時刻はここで付与する（呼び出し側に委ねない）。
        日本語の reason をそのまま残すため ensure_ascii=False。
        """
        # 受け取った entry を監査スキーマに正規化（必要キーのみ・順序固定）してから書く
        record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "instance_id": entry.get("instance_id"),
            "command": entry.get("command"),
            "tier": entry.get("tier"),
            "handler": entry.get("handler"),
            "decision": entry.get("decision"),
            "reason": entry.get("reason", ""),
            "duration_ms": entry.get("duration_ms"),
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
