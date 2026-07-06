"""§8.3 タスク投入 — Slack 由来の操作を sa-ru 向けタスクファイルとしてキューに置く。

u-zu（Slack Bot）が受け取ったスラッシュコマンド・メンション・DM・ボタン操作を、
sa-ru（dispatcher）が走査する `/opt/taka-ma/data/tasks/` 配下の JSON ファイルとして書き出す。
sa-ru は `status == "init"` のファイルを拾って accepted に更新するため（orchestrator の
共有 FileQueue 経由 `task_q.claim()`）、ここでは設計書 §8.3 のタスクファイル形式・`status="init"` で作成する。
"""

import datetime
import os
import uuid

from services.atomic_io import atomic_write_json

# sa-ru（dispatcher）がタスクファイルを走査するディレクトリ（sa-ru.yaml の queue.dir と一致）。
TASK_DIR = "/opt/taka-ma/data/tasks"


def enqueue_task(source: str, command: str, *, user_id: str, team_id: str,
                 channel_id: str, thread_ts: str | None = None) -> str:
    """§8.3 形式のタスクファイルを 1 件作成し、生成した task_id を返す。

    source: タスクの発生元（slack_command / slack_mention / slack_dm / slack_action）。
    team_id: 送信元ワークスペース。複数ワークスペース運用時に応答先を特定する（§8.3）。
    thread_ts: スレッド返信先。スレッド外（スラッシュコマンド等）では None。
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "status": "init",
        "source": source,
        "command": command,
        "user_id": user_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "created_at": now,
        "updated_at": now,
    }
    os.makedirs(TASK_DIR, exist_ok=True)
    # ファイル名は時刻接頭辞 + task_id。sorted() 走査で投入順に処理されるようにする。
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    task_path = os.path.join(TASK_DIR, f"{ts}_{task_id}.json")
    # 原子書込。sa-ru が部分書込の task JSON を読む torn-read を防ぐ（§8.3 書込の原子性）。
    atomic_write_json(task_path, task)
    return task_id
