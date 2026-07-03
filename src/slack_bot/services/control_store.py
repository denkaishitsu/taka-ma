"""§8.10c 制御コマンド投入 — u-zu が sa-ru へ「手動 ollama 停止」等の制御命令を渡す。

経路は Slack→u-zu→sa-ru。停止本体は sa-ru の
`RemoteProcessManager.stop_ollama()`（SSOT 化）に在り、u-zu からは直接呼べない
（別プロセス）。そこで §8.10 承認ファイルと同じ共有 FS ファイル方式で命令を渡す:
u-zu が `/opt/taka-ma/data/controls/{control_id}.json` を status=pending で書き、
sa-ru が制御ループでポーリング検知 → 対応する操作（stop_ollama 等）を実行し結果を Slack へ返す。

承認（approval_store）と分離する理由: あちらは「sa-ru が作った pending を u-zu が approve/reject」
する応答経路。こちらは「u-zu が起点で命令を作り sa-ru が実行」する発行経路で、向きも用途も逆。
"""

import datetime
import json
import os
import uuid

# sa-ru の制御ループが走査するディレクトリ（sa-ru.yaml の controls.dir と一致させる）。
# 両プロセスで供給元を 1 つにするため環境変数 `TAKA_MA_CONTROL_DIR` を優先する（パス直書きの SSOT 化）。
CONTROL_DIR = os.environ.get("TAKA_MA_CONTROL_DIR", "/opt/taka-ma/data/controls")

# 制御コマンド契約値（§8.10c）。sa-ru の制御ループと**同じ文字列**を使う規約。
# 別ツリー配備で import 共有できないため定数化でタイプミスを防ぎ、grep で両側一致を確認する。
COMMAND_STOP_OLLAMA = "stop_ollama"
VALID_COMMANDS = (COMMAND_STOP_OLLAMA,)

STATUS_PENDING = "pending"


def enqueue_control(command: str, *, user_id: str, team_id: str,
                    channel_id: str, thread_ts: str | None = None) -> str:
    """制御コマンドを 1 件作成し、生成した control_id を返す。

    command: 実行させる操作（VALID_COMMANDS のいずれか）。未知の値は呼び出し側のバグなので
    握り潰さず ValueError を投げる（sa-ru 側で黙ってスキップされ Slack に応答が出ないのを防ぐ）。
    user_id/team_id/channel_id/thread_ts: sa-ru が実行結果を同じ場所へ返信するための宛先。

    sa-ru のポーラが中途半端な JSON を読まないよう、一意 tmp へ書いて os.replace で原子的に置く
    （§8.10 承認ファイルと同方針）。
    """
    if command not in VALID_COMMANDS:
        raise ValueError(f"未知の制御コマンド: {command}")

    os.makedirs(CONTROL_DIR, exist_ok=True)
    control_id = uuid.uuid4().hex
    record = {
        "control_id": control_id,
        "command": command,
        "status": STATUS_PENDING,
        "user_id": user_id,
        "team_id": team_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "created_at": datetime.datetime.now().astimezone().isoformat(),
    }
    path = os.path.join(CONTROL_DIR, f"{control_id}.json")
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    # 書込中に落ちても tmp を残さない（os.replace 前の例外で孤児 .tmp が溜まるのを防ぐ。
    # approval_store.resolve_approval と同じ後始末）。
    try:
        with open(tmp, "w") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return control_id
