#!/bin/bash
# 上り中継＋返信取得を 1 コマンドで完結させる（設計書 §8.17）。
#
# なぜスクリプトへまとめるか: Slack MCP のツールを 1 個ずつ呼ぶと、呼び出しごとに
# リレー Claude の推論が挟まる。基準時刻の取得→投稿→返信読取を個別に呼ぶと 5 往復・
# 1 ターン 45 秒に達し会話にならない（実機で計測）。ここで完結させれば LLM の思考は 1 回で済む。
#
# 使い方: relay.sh <発話原文> [thread_ts]
#   thread_ts 省略時はスレッド外へ新規投稿し、その ts を会話面の起点として返す。
# 出力（1 行目が状態、以降が本文）:
#   TS <投稿の ts>            … 投稿した自分の発話の ts（新規なら以後の thread_ts）
#   REPLY <本文>              … taka-ma の返信が得られたとき（複数行は改行のまま）
#   NOREPLY                   … 規定回数読んでも返信が無いとき
#   NG <理由>                 … 失敗
set -euo pipefail

cd "$(dirname "$0")/.."
# SLACK_DM_CHANNEL / SLACK_TOKEN_KEYCHAIN_SERVICE / OWNER_USER_ID を読む
source ./relay.env

text="${1:?usage: relay.sh <text> [thread_ts]}"
thread_ts="${2:-}"

if [[ -n "$thread_ts" ]] && ! [[ "$thread_ts" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "NG invalid thread_ts"; exit 1
fi

# トークンは Keychain からのみ取得する。平文ファイルに置かないことで、コミット・rsync・
# OSS 公開のいずれの経路でも漏れない（設計書 §8.17）。
token=$(security find-generic-password -s "$SLACK_TOKEN_KEYCHAIN_SERVICE" -a slack -w 2>/dev/null) || {
  echo "NG keychain item not found: $SLACK_TOKEN_KEYCHAIN_SERVICE"; exit 1; }

api() {  # api <method> <json>
  curl -sS -X POST "https://slack.com/api/$1" \
    -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json; charset=utf-8' \
    --data "$2"
}

# 投稿。本文は任意文字列なので jq で JSON エスケープする（クォート破りを防ぐ）。
payload=$(jq -nc --arg c "$SLACK_DM_CHANNEL" --arg t "$text" --arg th "$thread_ts" \
  '{channel:$c, text:$t} + (if $th == "" then {} else {thread_ts:$th} end)')
post=$(api chat.postMessage "$payload")

if [[ "$(jq -r '.ok' <<<"$post")" != "true" ]]; then
  echo "NG post failed: $(jq -r '.error // "unknown"' <<<"$post")"; exit 1
fi
own_ts=$(jq -r '.ts' <<<"$post")
echo "TS $own_ts"

# 返信待ち。会話面のスレッド（新規なら今の投稿自身）を読み、自分の投稿より後に
# 現れた他者（taka-ma）の発言を返信とみなす。時間ではなく回数で打ち切る。
root_ts="${thread_ts:-$own_ts}"
# 10 回×3 秒＝約 30 秒。sa-ru の会話返信は実測 11〜12 秒だが、分解を伴うと伸びる。
# ここはシェルの sleep なので待っても LLM のコストは掛からない。
for _ in $(seq 1 10); do
  sleep 3
  # 読取系は JSON ボディを受け付けず invalid_arguments になる（実測）。GET + クエリで叩く。
  # oldest=own_ts で「自分の投稿より後」だけを取る。conversations.replies は古い順で
  # 返すため、これが無いと長いスレッド（limit 超のメッセージ数）では新着が 2 ページ目以降に
  # 落ち、返信が来ているのに NOREPLY になる。
  rep=$(curl -sS -G "https://slack.com/api/conversations.replies" \
    -H "Authorization: Bearer $token" \
    --data-urlencode "channel=$SLACK_DM_CHANNEL" \
    --data-urlencode "ts=$root_ts" \
    --data-urlencode "oldest=$own_ts" \
    --data-urlencode "limit=20") || continue
  [[ "$(jq -r '.ok' <<<"$rep")" == "true" ]] || continue
  # 本文は blocks を優先して組み立てる。計画確認・Tier 3 承認の提示は本文が blocks 側に
  # あり、top-level の text は "この内容で着手します（着手 / やり直す）" 等の短い代替文しか
  # 入らない（slack_notifier.send_exec_confirm_request）。text だけを読むと計画の要点
  # （wave 段組み・サブタスク数・重さ・モデル）がリレーに届かず、選択 UI に判断材料を
  # 載せられない（契約 §計画確認の決着 2）。実機で発生・確認済み。
  # 発言者で絞る。オーナー自身の投稿（リレーの中継・Slack から人が直接打った割込み）を
  # 除かないと、割込み発話をそのまま「taka-ma の返信」として G2 に出してしまう。
  body=$(jq -r --arg own "$own_ts" --arg owner "$OWNER_USER_ID" '
    [ .messages[]
      | select((.ts|tonumber) > ($own|tonumber))
      | select(.user != $owner)
      | ( [ (.blocks // [])[]
            | select(.type == "header" or .type == "section")
            | (.text.text // empty), ((.fields // [])[].text) ]
          | join("\n") ) as $b
      | if ($b | length) > 0 then $b else (.text // "") end
    ] | last // empty' <<<"$rep")
  if [[ -n "$body" ]]; then
    echo "REPLY $body"; exit 0
  fi
done
echo "NOREPLY"
