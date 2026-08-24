"""intent レコードの読み書き — 依頼の寿命（goal_status）を持つ台帳（設計書 §8.10e / §8.10f）。

タスクが終端しても完了条件（acceptance）が PASS するまで依頼は `open` のまま閉じない。
本モジュールはファイル操作のみを担い、検査の実行（世界に対する再評価）は呼び出し側
（orchestrator）が GroundingVerifier で行う。書込は他レコードと同じ原子書込（§8.3 の規律）。
"""

import datetime
import json
import os
import re

from orchestrator.file_queue import atomic_write_json

# ファイル名は {task_id}.json（§8.10e）。task_id はタスクファイル由来のためパス安全性を検証する
_TASK_ID_RE = re.compile(r"\A[A-Za-z0-9-]+\Z")

GOAL_OPEN = "open"
GOAL_ACHIEVED = "achieved"
GOAL_WITHDRAWN = "withdrawn"


def _path(intents_dir: str, task_id: str) -> str | None:
    if not intents_dir or not _TASK_ID_RE.match(task_id or ""):
        return None
    return os.path.join(intents_dir, f"{task_id}.json")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def create(intents_dir: str, *, task_id: str, conversation_id: str | None,
           summary: str, acceptance: list, workspace: str | None,
           user_id: str = "") -> None:
    """確定タスク生成と同時に intent レコードを作る（§8.10e。初期要件は承認済み確定要約 1 件）。

    acceptance が空の依頼は検査で閉じる根拠が無いため、goal_status を持つ意味がない —
    それでもレコードは作る（要件の記録は完了条件の有無と独立）。goal_status は
    acceptance が有るときのみ open、無ければ achieved（検査対象なし＝終端で閉じる従来動作）。
    """
    path = _path(intents_dir, task_id)
    if path is None:
        return
    os.makedirs(intents_dir, exist_ok=True)
    now = _now()
    atomic_write_json(path, {
        "task_id": task_id,
        "conversation_id": conversation_id,
        "summary": summary,
        "requirements": [{
            "seq": 1, "kind": "initial", "text": summary, "target_seq": None,
            "source_ts": None, "appended_at": now, "approved_by": user_id,
        }],
        "acceptance": acceptance or [],
        "workspace": workspace,   # 再検査（後続タスク完了時の open 目標の再評価）の対象
        "goal_status": GOAL_OPEN if acceptance else GOAL_ACHIEVED,
        "created_at": now,
        "updated_at": now,
    })


def load(intents_dir: str, task_id: str) -> dict | None:
    """intent レコードを読む。不在・破損は None（呼び出し側は goal 更新をスキップ）。"""
    path = _path(intents_dir, task_id)
    if path is None:
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def set_goal_status(intents_dir: str, task_id: str, status: str) -> None:
    """goal_status を更新する（宣言では閉じない — 呼び出し側は検査 PASS の時のみ achieved を渡す）。"""
    record = load(intents_dir, task_id)
    path = _path(intents_dir, task_id)
    if record is None or path is None:
        return
    record["goal_status"] = status
    record["updated_at"] = _now()
    atomic_write_json(path, record)


def list_open(intents_dir: str, conversation_id: str | None) -> list[dict]:
    """同一会話の open な intent（acceptance と workspace を持つもの）を返す（§8.10f 依頼の寿命）。

    タスク完了のたびに呼び、open 目標を世界に対して再検査する材料にする。conversation_id が
    無いタスク（会話を経ない経路）は対象外。破損レコードは読み飛ばす。
    """
    if not intents_dir or not conversation_id or not os.path.isdir(intents_dir):
        return []
    found = []
    for name in sorted(os.listdir(intents_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(intents_dir, name)) as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if (record.get("conversation_id") == conversation_id
                and record.get("goal_status") == GOAL_OPEN
                and record.get("acceptance") and record.get("workspace")):
            found.append(record)
    return found
