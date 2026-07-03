"""Tier 3 承認リクエストのインメモリ状態管理。

cross-process な承認本体（承認ファイルの status 更新）は approval_store が担う。
こちらは同一プロセス内で pending リクエストを保持・遷移させる軽量マネージャ
（プロセス再起動で揮発する）。
"""

import logging
import time

logger = logging.getLogger("u-zu.approval")


class ApprovalManager:
    """承認リクエストを request_id で索引し、pending → approved/rejected を辿る。"""

    def __init__(self):
        """空の pending テーブルで初期化する。"""
        # request_id -> リクエスト情報。pending のものだけ保持し、決着時に取り除く。
        self._pending = {}

    def create_request(self, request_id: str, command: str,
                       instance_id: str, risk_reason: str) -> dict:
        """新規承認リクエストを pending で登録し、作成したレコードを返す。"""
        request = {
            "request_id": request_id,
            "command": command,
            "instance_id": instance_id,
            "risk_reason": risk_reason,
            "status": "pending",
            "created_at": time.time(),
        }
        self._pending[request_id] = request
        logger.info("承認リクエスト作成: %s — %s", request_id, command)
        return request

    def approve(self, request_id: str) -> dict | None:
        """対象を pending から取り出し approved にして返す。未知 ID は None。"""
        req = self._pending.pop(request_id, None)
        if req:
            req["status"] = "approved"
            logger.info("承認: %s", request_id)
        else:
            logger.warning("不明なリクエストID: %s", request_id)
        return req

    def reject(self, request_id: str) -> dict | None:
        """対象を pending から取り出し rejected にして返す。未知 ID は None。"""
        req = self._pending.pop(request_id, None)
        if req:
            req["status"] = "rejected"
            logger.info("拒否: %s", request_id)
        else:
            logger.warning("不明なリクエストID: %s", request_id)
        return req

    def get_pending(self) -> list[dict]:
        """まだ決着していない承認リクエストの一覧を返す。"""
        return list(self._pending.values())
