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

# 会話面で最後に現れた「ボタン付き（type=actions のブロックを持つ）」メッセージを提示として返す。
# 「末尾のメッセージがボタン付きか」では判定できない — taka-ma はボタンの後にも通知を投稿する
# （Tier 3 承認依頼の後に保留通知が続く。実機で発生し、末尾判定では NONE に化けて詰んだ）。
# 提示が既に決着済みかどうかの真の判定は decide.sh 側（Mac mini 上の pending 検査）が担う。
# 既決の提示を PRESENT で返しても、選択後の decide.sh が NG（not pending）で止めるため安全。
# オーナー自身の発言を除くのは relay.sh/watch.sh と同じ理由（割込み発話を提示と取り違えない）。
last=$(jq -c --arg owner "$OWNER_USER_ID" \
  '[ .[] | select(.user != $owner)
        | select((.blocks // []) | map(select(.type == "actions")) | length > 0) ]
   | last // empty' <<<"$msgs")

if [[ -z "$last" ]]; then echo "NONE"; exit 0; fi

# 本文は blocks の header / section から組み立てる（relay.sh と同じ規則）。
body=$(jq -r '
  [ (.blocks // [])[]
    | select(.type == "header" or .type == "section")
    | (.text.text // empty), ((.fields // [])[].text) ]
  | join("\n")' <<<"$last")

echo "PRESENT $body"
