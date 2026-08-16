#!/bin/bash
# 上り中継の退避経路（設計書 §8.17 第 1 段）。
#
# 主経路は relay.sh（Slack Web API chat.postMessage）による原文投稿で、u-zu の既存
# 受信ハンドラを通るため Slack にも発話が残る。本スクリプトは relay.sh が NG を返すとき
# （Keychain のトークン未登録・投稿失敗・Slack 側の障害）に限って使う退避路であり、u-zu と同一モジュール
# （services/conversation_queue.py の enqueue_conversation_message）を SSH 実行して
# §8.3 会話キューへ直接投入する。会話レコードの生成契約（conversation_id 導出・
# 原子書込）をリレー側に二重実装しないため、モジュールを呼ぶ形にしている。
#
# この経路を通した発話は Slack に残らない（会話の片側が欠ける）。常用しない。
#
# 使い方: say.sh <発話原文> [thread_ts] [channel_id]
#   thread_ts  … 会話面がスレッドのとき指定する。conversation_id は
#                team:channel:(thread_ts または user_id) で決まるため（§8.3）、
#                渡さないと主経路と別の会話に落ちて文脈が分断される。
#   channel_id … 会話面が DM 以外のとき指定する。既定は relay.env の SLACK_DM_CHANNEL。
# 出力: OK <message_id> / エラー時は非0終了
set -euo pipefail

cd "$(dirname "$0")/.."
# OWNER_USER_ID / TEAM_ID / SLACK_DM_CHANNEL を読む（会話レコードの宛先契約）
source ./relay.env

text="${1:?usage: say.sh <text> [thread_ts] [channel_id]}"
thread_ts="${2:-}"
channel_id="${3:-$SLACK_DM_CHANNEL}"

# thread_ts は Slack の ts 形式（1234567890.123456）のみ受理。空は「スレッド外」を意味する。
# SSH コマンド行へ埋め込むため、ここで弾かないとシェルインジェクションの入口になる。
if [[ -n "$thread_ts" ]] && ! [[ "$thread_ts" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "NG: invalid thread_ts"; exit 1
fi
# channel_id は Slack の ID 文字種（英数）のみ受理。同上。
if ! [[ "$channel_id" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "NG: invalid channel_id"; exit 1
fi

# 発話は任意文字列のため、SSH コマンド行に直接埋め込まず base64 で運ぶ
# （クォート破り・シェルインジェクションの余地を残さない）。tr -d は長文での折返し対策。
b64=$(printf '%s' "$text" | base64 | tr -d '\n')

ssh mac-mini "cd /opt/taka-ma/u-zu/slack_bot && /opt/taka-ma-env/bin/python3 - '$b64' '$OWNER_USER_ID' '$TEAM_ID' '$channel_id' '$thread_ts'" <<'PY'
import base64
import sys
from services.conversation_queue import enqueue_conversation_message
text = base64.b64decode(sys.argv[1]).decode("utf-8")
# 空文字は「スレッド外」。None にすると conversation_id の末尾が user_id になる（§8.3）。
thread_ts = sys.argv[5] or None
mid = enqueue_conversation_message(
    "slack_dm", text,
    user_id=sys.argv[2], team_id=sys.argv[3], channel_id=sys.argv[4],
    thread_ts=thread_ts,
)
print("OK", mid)
PY
