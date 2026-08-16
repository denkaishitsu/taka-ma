#!/bin/bash
# G2 決着代行（設計書 §8.17 第 1 段）。
# u-zu のボタン押下と同一のファイル契約で pending レコードを決着させるため、
# Mac mini 上の u-zu と同一 store モジュールを同一 venv で SSH 実行する。
# pending 検査・排他・原子書込は既存実装に委ね、リレー側に status 遷移ロジックを
# 持たない（§8.17 決着代行 条件 3: 二重実装禁止）。
#
# 使い方:
#   decide.sh exec     latest            confirmed|rejected [channel_id]  # 計画確認: この会話面の最新 pending を決着（通常はこれ）
#   decide.sh exec     <exec_request_id> confirmed|rejected               # 計画確認: id 指定（§8.10b）
#   decide.sh approval <request_id>      approved|rejected                # Tier 3 承認（§8.10）
#
# exec に latest がある理由: Slack の計画確認メッセージ本文に exec_request_id が
# 含まれず（ボタンの内部値のみ）、リレーは id を知り得ない。対象は「この会話面
# （TEAM_ID:channel_id）の最新 pending」で特定する（§8.17）。
#
# channel_id を引数で受ける理由: リレーは DM 以外のスレッドも会話面に選べる（§8.17
# 「会話面の選択」）。conversation_id は team:channel:… で決まるため、DM 固定にすると
# 別チャンネルで提示された計画を取り違える／見つけられない。既定は SLACK_DM_CHANNEL。
#
# 出力: OK <id> <要約先頭> / NG 理由。exit code も対応。
set -euo pipefail

cd "$(dirname "$0")/.."
# OWNER_USER_ID / TEAM_ID / SLACK_DM_CHANNEL を読む（decided_by と会話面特定に使用）
source ./relay.env

kind="${1:?usage: decide.sh exec|approval <id|latest> <decision> [channel_id]}"
id="${2:?usage: decide.sh exec|approval <id|latest> <decision> [channel_id]}"
decision="${3:?usage: decide.sh exec|approval <id|latest> <decision> [channel_id]}"
channel_id="${4:-$SLACK_DM_CHANNEL}"

# id は uuid 相当（英数・ハイフン）か latest のみ受理。SSH コマンド文字列に埋め込むため、
# ここで弾かないとシェルインジェクション／パストラバーサルの入口になる
# （approval_store.py の _REQUEST_ID_RE と同一制約）。
if ! [[ "$id" =~ ^[A-Za-z0-9-]+$|^latest$ ]]; then
  echo "NG: invalid id"; exit 1
fi
# channel_id は Slack の ID 文字種（英数）のみ受理。同上の理由。
if ! [[ "$channel_id" =~ ^[A-Za-z0-9]+$ ]]; then
  echo "NG: invalid channel_id"; exit 1
fi

case "$kind" in
  exec)
    if ! [[ "$decision" =~ ^(confirmed|rejected)$ ]]; then echo "NG: invalid decision"; exit 1; fi
    ;;
  approval)
    if ! [[ "$decision" =~ ^(approved|rejected)$ ]]; then echo "NG: invalid decision"; exit 1; fi
    if [[ "$id" == "latest" ]]; then echo "NG: approval requires explicit request_id"; exit 1; fi
    ;;
  *)
    echo "NG: invalid kind (exec|approval)"; exit 1
    ;;
esac

# cwd を slack_bot 直下にして実行する（services.* の import 前提が u-zu 本体と同じになる）。
if [[ "$kind" == "approval" ]]; then
  ssh mac-mini "cd /opt/taka-ma/u-zu/slack_bot && /opt/taka-ma-env/bin/python3 - '$id' '$decision' 'g2:${OWNER_USER_ID}'" <<'PY'
import sys
from services.approval_store import resolve_approval
ok = resolve_approval(sys.argv[1], sys.argv[2], user_id=sys.argv[3])
print("OK" if ok else "NG: not pending or not found")
sys.exit(0 if ok else 1)
PY
else
  ssh mac-mini "cd /opt/taka-ma/u-zu/slack_bot && /opt/taka-ma-env/bin/python3 - '$id' '$decision' 'g2:${OWNER_USER_ID}' '${TEAM_ID}' '${channel_id}'" <<'PY'
import glob
import json
import os
import sys
from services.exec_confirm import EXEC_CONFIRM_DIR, resolve_exec_confirm

req_id, decision, decided_by, team, chan = sys.argv[1:6]

if req_id == "latest":
    # この会話面（team:channel:）の pending から最新 1 件を選ぶ。id 不明でも決着できる
    # ようにする（Slack 本文に id が無いため）。古い pending は無期限で残り得る仕様
    # （§8.10b タイムアウト無し）なので、一意性ではなく created_at 最新を採る。
    prefix = f"{team}:{chan}:"
    cands = []
    for p in glob.glob(os.path.join(EXEC_CONFIRM_DIR, "*.json")):
        try:
            with open(p) as f:
                r = json.load(f)
        except Exception:
            continue
        if r.get("status") == "pending" and str(r.get("conversation_id", "")).startswith(prefix):
            cands.append((r.get("created_at", ""), r))
    if not cands:
        print("NG: no pending record for this conversation")
        sys.exit(1)
    cands.sort(key=lambda t: t[0])
    record = cands[-1][1]
    req_id = record["exec_request_id"]
    head = (record.get("summary") or "").replace("\n", " ")[:40]
else:
    head = ""

ok = resolve_exec_confirm(req_id, decision, decided_by=decided_by)
print(("OK " if ok else "NG ") + req_id + (" " + head if head else ""))
sys.exit(0 if ok else 1)
PY
fi
