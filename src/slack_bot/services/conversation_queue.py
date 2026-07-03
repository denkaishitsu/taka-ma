"""§8.3 (A) 会話投入 — Slack 発話を sa-ru の会話ループ向けメッセージとしてキューに置く。

会話 → 実行トリガーで新設。従来は u-zu が Slack 1 通ごとに生文を
task JSON 化して即実行させていた（task_queue.enqueue_task）。会話フロントエンド化に伴い、
通常の発話は「会話キュー」へ流し、sa-ru の脳（sa-ru.model）が会話・要約・実行意図判定を担う。

確定した実行タスク（status=init の task JSON）は、ここではなく sa-ru が
「人間の着手確認」を得てから生成する（責任分界の移動。§8.3 (B)）。
"""

import datetime
import json
import os
import uuid

# sa-ru の会話ループが走査するディレクトリ（sa-ru.yaml の conversation.dir と一致）。
CONVERSATION_DIR = "/opt/taka-ma/data/conversations"


def derive_conversation_id(*, team_id: str, channel_id: str,
                           thread_ts: str | None, user_id: str) -> str:
    """会話セッションの一意キーを導出する。

    複数ワークスペース・複数ユーザー・複数スレッドの会話を分離するため、
    (team_id, channel_id, thread_ts または user_id) から決定する。
    スレッド外の DM・スラッシュコマンドは thread_ts が無いので user_id を末尾に使う
    （= DM は人単位、スレッドはスレッド単位の会話になる）。
    """
    tail = thread_ts or user_id
    return f"{team_id or '-'}:{channel_id or '-'}:{tail}"


def enqueue_conversation_message(source: str, text: str, *, user_id: str,
                                 team_id: str, channel_id: str,
                                 thread_ts: str | None = None,
                                 force_ready: bool = False) -> str:
    """会話メッセージを 1 件作成し、生成した message_id を返す。

    source: 発生元（slack_mention / slack_dm / slack_command / slack_go）。
    force_ready: `/taka-ma-go` 等で「LLM 判定を待たず締める」明示エスケープ。True なら
      sa-ru は意図判定をスキップし、直近会話を要約して着手確認へ進む（§8.3 (B)）。
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    message_id = str(uuid.uuid4())
    conversation_id = derive_conversation_id(
        team_id=team_id, channel_id=channel_id, thread_ts=thread_ts, user_id=user_id,
    )
    message = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "status": "init",
        "source": source,
        "text": text,
        "force_ready": force_ready,
        "user_id": user_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "created_at": now,
    }
    os.makedirs(CONVERSATION_DIR, exist_ok=True)
    # 時刻接頭辞 + message_id。sorted() 走査で発話順に処理されるようにする。
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    path = os.path.join(CONVERSATION_DIR, f"{ts}_{message_id}.json")
    with open(path, "w") as f:
        json.dump(message, f, ensure_ascii=False, indent=2)
    return message_id
