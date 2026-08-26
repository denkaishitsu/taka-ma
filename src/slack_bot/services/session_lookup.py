"""§8.3 (C) 返答待ちスレッドの能動昇格 — sa-ru セッションの awaiting_reply を参照する。

taka-ma 自身が質問・確認を出して返答を待っているスレッドでは、メンション無しの
ユーザー返信も能動ターンとして受理する。その判定材料（awaiting_reply）は sa-ru が
セッション永続化ファイルへ書く。u-zu はここでファイルを読むだけで、書き換えない
（判定の権威は sa-ru 側の会話状態にある）。
"""

import json
import os
import re

# sa-ru のセッション永続化ディレクトリ（sa-ru.yaml の conversation.sessions_dir と一致）。
SESSIONS_DIR = "/opt/taka-ma/data/conversations/sessions"


def _session_filename(conversation_id: str) -> str:
    """conversation_id をファイル名安全な形へ変換する。

    変換規則は sa-ru 側 ConversationManager._session_path と一致させること
    （`grep -n '0-9A-Za-z._-' src/orchestrator/conversation.py src/slack_bot/services/session_lookup.py`
    で両側の一致を確認する。ズレると常に「セッション不在 = passive」へ落ちる）。
    """
    return re.sub(r"[^0-9A-Za-z._-]", "_", conversation_id) + ".json"


def is_awaiting_reply(conversation_id: str) -> bool:
    """当該会話で taka-ma がユーザーの返答を待っているかを返す（§8.3 (C) 能動昇格）。

    ファイル不在・読取不能・キー不在はすべて False（安全側 = passive のまま。
    bot が呼ばれていない発話へ自発応答しない）。
    """
    path = os.path.join(SESSIONS_DIR, _session_filename(conversation_id))
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("awaiting_reply"))
