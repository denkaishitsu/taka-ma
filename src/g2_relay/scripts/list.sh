#!/bin/bash
# 会話面の候補一覧を 1 コマンドで返す（設計書 §8.17）。
#
# なぜスクリプトへまとめるか: 一覧作成を Slack MCP で行うと (a) ツール定義の取得（ToolSearch）
# と読取で推論が複数回挟まり、(b) `slack_read_channel` が要求する channel_id をリレーが知らない
# ため、実機ではチャンネル探索（slack_search_users 等）に迷い込んで 1 ターンが数十秒に伸びた。
# channel は relay.env が持っているので、スクリプト側で完結させれば探索も推論も要らない。
#
# 使い方: list.sh [days]
#   days 省略時は 1（直近 1 日）。「もっと前」を選ばれたら 3 → 7 を渡す。
# 出力: 1 行 1 候補（新しい順・最大 5 件）。そのまま選択肢のラベルに使える形にしてある。
#   M/D HH:MM 要点 | <ts>
#   NONE               … 候補なし
#   NG <理由>          … 失敗
set -euo pipefail

cd "$(dirname "$0")/.."
# SLACK_DM_CHANNEL / SLACK_TOKEN_KEYCHAIN_SERVICE を読む
source ./relay.env

days="${1:-1}"
if ! [[ "$days" =~ ^[0-9]+$ ]]; then echo "NG invalid days"; exit 1; fi

token=$(security find-generic-password -s "$SLACK_TOKEN_KEYCHAIN_SERVICE" -a slack -w 2>/dev/null) || {
  echo "NG keychain item not found: $SLACK_TOKEN_KEYCHAIN_SERVICE"; exit 1; }

# 基準時刻はここで計算する。リレーに epoch を暗算させると未来の値を渡して 0 件になる
# （実機で発生。「直近 1 日は空でした」と誤報告した）。
oldest=$(date -v-"${days}"d +%s)

# 読取系は JSON ボディを受け付けず invalid_arguments になる（実測）。GET + クエリで叩く。
rep=$(curl -sS -G "https://slack.com/api/conversations.history" \
  -H "Authorization: Bearer $token" \
  --data-urlencode "channel=$SLACK_DM_CHANNEL" \
  --data-urlencode "oldest=$oldest" \
  --data-urlencode "limit=50")

if [[ "$(jq -r '.ok' <<<"$rep")" != "true" ]]; then
  echo "NG read failed: $(jq -r '.error // "unknown"' <<<"$rep")"; exit 1
fi

# conversations.history はスレッド親（と単発投稿）だけを返す。Slack ではスレッド親の ts が
# そのまま thread_ts なので、候補の ts をラベル末尾へ書けばリレーはそれを渡すだけで済む。
out=$(jq -r --arg tz "$(date +%z)" '
  [ .messages[]
    | select((.subtype // "") == "")
    | { ts: .ts,
        head: ((.text // "") | gsub("\n"; " ") | .[0:20]) }
  ] | .[0:5][]
  | (.ts | tonumber | strflocaltime("%-m/%-d %H:%M")) + " " + .head + " | " + .ts' <<<"$rep")

if [[ -z "$out" ]]; then echo "NONE"; else echo "$out"; fi
