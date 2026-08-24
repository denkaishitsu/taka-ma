#!/bin/bash
# 決着後の進捗・完了追跡を 1 コマンドで完結させる（設計書 §8.17）。
#
# なぜスクリプトへまとめるか: 追跡を Slack MCP の `slack_read_thread` で行うと、読取 1 回
# ごとにリレー Claude の推論が挟まる。実機では完了までに読取 7 回・39 秒を要し、その大半が
# 推論待ちだった（relay.sh を 1 本化したのと同じ理由）。シェル側にループを閉じ込めれば、
# 何秒待っても LLM のコストは増えない。
#
# 使い方: watch.sh <thread_ts> <after_ts> [rounds]
#   after_ts より後に現れた taka-ma の発言だけを拾う。after_ts には直前に自分が投稿した ts
#   （relay.sh が返した TS、または decide.sh 実行前に控えた ts）を渡す。
#   rounds 省略時は 35（3 秒間隔＝最大約 105 秒）。締め直しの一言だけを拾いたいときは 5 程度。
# 出力（1 行目が状態、以降が本文）:
#   DONE <本文>      … 「タスク完了（結果ファイル: …）」または「タスク失敗」を検出して終了
#   PROGRESS <本文>  … 新着はあるが完了に至らないまま回数上限
#   NONE             … 新着なし
#   NG <理由>        … 失敗
set -euo pipefail

cd "$(dirname "$0")/.."
# SLACK_DM_CHANNEL / SLACK_TOKEN_KEYCHAIN_SERVICE を読む
source ./relay.env

thread_ts="${1:?usage: watch.sh <thread_ts> <after_ts> [rounds]}"
after_ts="${2:?usage: watch.sh <thread_ts> <after_ts> [rounds]}"
rounds="${3:-35}"

for v in "$thread_ts" "$after_ts"; do
  if ! [[ "$v" =~ ^[0-9]+\.[0-9]+$ ]]; then echo "NG invalid ts"; exit 1; fi
done
if ! [[ "$rounds" =~ ^[0-9]+$ ]]; then echo "NG invalid rounds"; exit 1; fi

# トークンは Keychain からのみ取得する（平文ファイルに置かない。設計書 §8.17）。
token=$(security find-generic-password -s "$SLACK_TOKEN_KEYCHAIN_SERVICE" -a slack -w 2>/dev/null) || {
  echo "NG keychain item not found: $SLACK_TOKEN_KEYCHAIN_SERVICE"; exit 1; }

body=""
for _ in $(seq 1 "$rounds"); do
  sleep 3
  # 読取系は JSON ボディを受け付けず invalid_arguments になる（実測）。GET + クエリで叩く。
  # oldest=after_ts で「自分の投稿より後」だけを取る（relay.sh と同じ理由。無いと長い
  # スレッドで新着が 2 ページ目以降に落ち、進捗・完了が来ているのに NONE になる）。
  rep=$(curl -sS -G "https://slack.com/api/conversations.replies" \
    -H "Authorization: Bearer $token" \
    --data-urlencode "channel=$SLACK_DM_CHANNEL" \
    --data-urlencode "ts=$thread_ts" \
    --data-urlencode "oldest=$after_ts" \
    --data-urlencode "limit=50") || continue
  [[ "$(jq -r '.ok' <<<"$rep")" == "true" ]] || continue
  # relay.sh と同じ抽出規則（blocks 優先）。進捗通知は text のみだが、完了直前に計画確認等の
  # block 付きメッセージが挟まっても本文を落とさないため規則を揃える。
  # 発言者で絞る理由は relay.sh と同じ（オーナーの割込み発話を taka-ma の通知と取り違えない）。
  body=$(jq -r --arg own "$after_ts" --arg owner "$OWNER_USER_ID" '
    [ .messages[]
      | select((.ts|tonumber) > ($own|tonumber))
      | select(.user != $owner)
      | ( [ (.blocks // [])[]
            | select(.type == "header" or .type == "section")
            | (.text.text // empty), ((.fields // [])[].text) ]
          | join("\n") ) as $b
      | if ($b | length) > 0 then $b else (.text // "") end
    ] | join("\n---\n")' <<<"$rep")
  # 完了・失敗は sa-ru の通知本文で判定する（orchestrator/__init__.py の通知文言に対応）。
  if grep -qE 'タスク完了（結果ファイル:|タスク失敗' <<<"$body"; then
    echo "DONE $body"; exit 0
  fi
  # ボタン付きメッセージ（計画確認・Tier 3 承認依頼）を検出したら回数を待たず即返す。
  # 承認依頼には保留猶予（60 秒）内の決着で worker がその場で再開できるという時間制約があり、
  # 回数上限まで握ると G2 への到達が猶予を超え「承認 → 保留 → 再投入 → 再承認依頼」の
  # ループに落ちる（実機で発生）。
  has_buttons=$(jq -r --arg own "$after_ts" --arg owner "$OWNER_USER_ID" '
    [ .messages[]
      | select((.ts|tonumber) > ($own|tonumber))
      | select(.user != $owner)
      | (.blocks // []) | map(select(.type == "actions")) | length
    ] | add // 0' <<<"$rep")
  if [[ "$has_buttons" != "0" ]]; then
    echo "PROGRESS $body"; exit 0
  fi
done

if [[ -n "$body" ]]; then
  echo "PROGRESS $body"
else
  echo "NONE"
fi
