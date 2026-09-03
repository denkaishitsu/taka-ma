"""判定ログ集計 — ローカル脳の換装判断基準の指標算出（設計書 §8.4.1）。

判定ログ（ya-ta-decisions-YYYY-MM-DD.jsonl）を期間指定でパースし、次を算出する:

- 分解フォールバック率: kind=decompose_call の fallback=true / 総数（発動基準 5% 超）
- 契約化のローカル縮退率: kind=contract の degraded=true / 総数（発動基準 20% 超）

LLM は使わない（JSONL パースのみ）。発動基準に達した指標があれば exit code 1 で終える
（cron・手動どちらの実行でも「提案が必要」を機械判定できる）。会話脳の重大誤出力
（例文エコー等）は実障害の手動記録が正であり、本スクリプトの対象外。

実行例:
    /opt/taka-ma-env/bin/python3 decision_stats.py --days 7
"""

import argparse
import datetime
import json
import os
import sys

DEFAULT_LOG_DIR = "/opt/taka-ma/logs"

# 発動基準（§8.4.1 の初期値。実データで較正する）
DECOMPOSE_FALLBACK_THRESHOLD = 0.05
CONTRACT_DEGRADED_THRESHOLD = 0.20


def _iter_entries(log_dir: str, days: int):
    """直近 days 日分の判定ログの各行を dict で列挙する（壊れ行はスキップ）。"""
    today = datetime.date.today()
    for offset in range(days):
        date = (today - datetime.timedelta(days=offset)).isoformat()
        path = os.path.join(log_dir, f"ya-ta-decisions-{date}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 壊れ行で集計全体を止めない
                if isinstance(entry, dict):
                    yield entry


def collect_stats(log_dir: str, days: int) -> dict:
    """期間内の指標を算出して返す（表示・閾値判定から分離した純集計）。"""
    decompose_total = decompose_fallback = 0
    contract_total = contract_degraded = 0
    for entry in _iter_entries(log_dir, days):
        kind = entry.get("kind")
        if kind == "decompose_call":
            decompose_total += 1
            if entry.get("fallback"):
                decompose_fallback += 1
        elif kind == "contract":
            contract_total += 1
            if entry.get("degraded"):
                contract_degraded += 1
    return {
        "days": days,
        "decompose_total": decompose_total,
        "decompose_fallback": decompose_fallback,
        "decompose_fallback_rate": (
            decompose_fallback / decompose_total if decompose_total else None),
        "contract_total": contract_total,
        "contract_degraded": contract_degraded,
        "contract_degraded_rate": (
            contract_degraded / contract_total if contract_total else None),
    }


def _fmt_rate(rate) -> str:
    """率の表示（データ不在は N/A と明示し、0% と混同させない）。"""
    return "N/A（記録なし）" if rate is None else f"{rate:.1%}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ローカル脳の換装判断指標の集計（§8.4.1）")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args(argv)

    stats = collect_stats(args.log_dir, args.days)
    triggered = []
    rate = stats["decompose_fallback_rate"]
    if rate is not None and rate > DECOMPOSE_FALLBACK_THRESHOLD:
        triggered.append("分解フォールバック率")
    rate = stats["contract_degraded_rate"]
    if rate is not None and rate > CONTRACT_DEGRADED_THRESHOLD:
        triggered.append("契約化のローカル縮退率")

    print(f"判定ログ集計（直近 {stats['days']} 日・{args.log_dir}）")
    print(f"- 分解フォールバック率: {_fmt_rate(stats['decompose_fallback_rate'])} "
          f"（{stats['decompose_fallback']}/{stats['decompose_total']}・"
          f"発動基準 {DECOMPOSE_FALLBACK_THRESHOLD:.0%} 超）")
    print(f"- 契約化のローカル縮退率: {_fmt_rate(stats['contract_degraded_rate'])} "
          f"（{stats['contract_degraded']}/{stats['contract_total']}・"
          f"発動基準 {CONTRACT_DEGRADED_THRESHOLD:.0%} 超。CLI 側の可用性問題として切り分け）")
    if triggered:
        print(f"発動: {', '.join(triggered)} — 該当業務の worker CLI 換装"
              "（と qwen3.8:27b の常駐解除の要否）をユーザーへ提案する")
        return 1
    print("発動基準に達した指標なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
