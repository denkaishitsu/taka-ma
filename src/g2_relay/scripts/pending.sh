#!/bin/bash
# 会話面に未決着の提示（計画確認・Tier 3 承認）が残っているかを 1 コマンドで返す（設計書 §8.17）。
#
# なぜスクリプトへまとめるか: これを Slack MCP の `slack_read_thread` で確かめると、ツール定義の
# 取得（ToolSearch）と読取と解釈で推論が複数回挟まり、選択 UI を出すまでに十数秒かかる（実機で計測）。
# 判定材料は「ボタン付きメッセージが最後に来ているか」だけなので、シェルで完結できる。
#
# 使い方: pending.sh <thread_ts>
# 出力:
#   PRESENT <本文>  … 未決着の提示が残っている（本文をそのまま選択 UI の question に使う）
#   NONE            … 提示が無い、または既に決着済み（ボタンより後に taka-ma の別発言がある）
#   NG <理由>
set -euo pipefail

cd "$(dirname "$0")/.."
# SLACK_DM_CHANNEL / SLACK_TOKEN_KEYCHAIN_SERVICE / OWNER_USER_ID を読む
source ./relay.env

thread_ts="${1:?usage: pending.sh <thread_ts>}"
if ! [[ "$thread_ts" =~ ^[0-9]+\.[0-9]+$ ]]; then echo "NG invalid thread_ts"; exit 1; fi

token=$(security find-generic-password -s "$SLACK_TOKEN_KEYCHAIN_SERVICE" -a slack -w 2>/dev/null) || {
  echo "NG keychain item not found: $SLACK_TOKEN_KEYCHAIN_SERVICE"; exit 1; }

# スレッド全ページを辿って末尾までのメッセージを集める。conversations.replies は古い順＋
# ページングのため、1 ページ目だけでは limit を超える長さのスレッドの「末尾」が取れず、
# 提示が残っているのに NONE と誤判定する。relay.sh/watch.sh と違い「後方だけ」では済まない
# （提示の ts を知らないため oldest で絞れない）ので cursor で最後まで読む。
# 読取系は JSON ボディを受け付けず invalid_arguments になる（実測）。GET + クエリで叩く。
msgs="[]"
cursor=""
for _ in $(seq 1 20); do
  args=(--data-urlencode "channel=$SLACK_DM_CHANNEL" --data-urlencode "ts=$thread_ts" \
        --data-urlencode "limit=200")
  [[ -n "$cursor" ]] && args+=(--data-urlencode "cursor=$cursor")
  rep=$(curl -sS -G "https://slack.com/api/conversations.replies" \
    -H "Authorization: Bearer $token" "${args[@]}")
  if [[ "$(jq -r '.ok' <<<"$rep")" != "true" ]]; then
    echo "NG read failed: $(jq -r '.error // "unknown"' <<<"$rep")"; exit 1
  fi
  msgs=$(jq -c --argjson acc "$msgs" '$acc + .messages' <<<"$rep")
  cursor=$(jq -r '.response_metadata.next_cursor // ""' <<<"$rep")
  [[ -z "$cursor" ]] && break
done

# 「taka-ma 側の最後のメッセージがボタン付き（type=actions のブロックを持つ）」を未決着の条件とする。
# 決着するとレコード側が動くだけで Slack のメッセージは残るため、ボタンの有無だけでは判定できない。
# 一方 taka-ma は決着後に必ず別の発言（着手します／やり直します／進捗）を投稿するので、
# ボタン付きが taka-ma の発言の末尾に残っていることを未決着の代理指標にできる。
# オーナー自身の発言を除くのは relay.sh/watch.sh と同じ理由 — 提示の後にユーザーが Slack から
# 直接発話しても提示は pending のままであり、それで NONE に化けてはならない。
last=$(jq -c --arg owner "$OWNER_USER_ID" \
  '[ .[] | select(.user != $owner) ] | last // empty' <<<"$msgs")

if [[ -z "$last" ]]; then echo "NONE"; exit 0; fi

last_has_buttons=$(jq -r '(.blocks // []) | map(select(.type == "actions")) | length > 0' <<<"$last")

if [[ "$last_has_buttons" != "true" ]]; then
  echo "NONE"; exit 0
fi

# 本文は blocks の header / section から組み立てる（relay.sh と同じ規則）。
body=$(jq -r '
  [ (.blocks // [])[]
    | select(.type == "header" or .type == "section")
    | (.text.text // empty), ((.fields // [])[].text) ]
  | join("\n")' <<<"$last")

echo "PRESENT $body"
