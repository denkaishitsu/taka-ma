#!/bin/bash
# 完了タスク結果の直読（設計書 §8.17 第 1 段）。
# Slack 通知が切り詰め・分割・パス併記になる長文結果を、Mac mini の正本
# （/opt/taka-ma/data/tasks/done/{YYYY-MM-DD}/ の task JSON）から全文取得する。
#
# 使い方:
#   read_done.sh <task_id>                                  # task_id で検索して表示
#   read_done.sh /opt/taka-ma/data/tasks/done/....json      # パス指定で表示
set -euo pipefail

arg="${1:?usage: read_done.sh <task_id | /opt/taka-ma/data/tasks/done/...json>}"

DONE_ROOT="/opt/taka-ma/data/tasks/done"

if [[ "$arg" == ${DONE_ROOT}/* ]]; then
  # パス指定: done 配下のみ許可。`..` を弾いて done 外への抜け道を塞ぎ、文字種も
  # パスに現れる範囲へ制限する（SSH コマンド行への埋め込みのため。クォート等が
  # 混ざるとリモートシェルでの引用が破れる）。
  if [[ "$arg" == *..* ]] || ! [[ "$arg" =~ ^[A-Za-z0-9/._-]+$ ]]; then
    echo "NG: invalid path"; exit 1
  fi
  ssh mac-mini "cat '$arg'"
else
  # task_id 指定: uuid 相当のみ受理（SSH コマンドへの埋め込みのため。decide.sh と同一制約）。
  if ! [[ "$arg" =~ ^[A-Za-z0-9-]+$ ]]; then
    echo "NG: invalid task_id"; exit 1
  fi
  ssh mac-mini "find ${DONE_ROOT} -name '*${arg}*.json' | head -1 | xargs -r cat"
fi
