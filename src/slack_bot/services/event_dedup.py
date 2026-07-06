"""Slack イベント再送の重複排除 — event_id ベースの TTL 付き seen セット。

Slack の Events API 配信は at-least-once で、u-zu の応答（3 秒 ack）が遅い・取りこぼした
と Slack が判断すると、同一発話を同じ `event_id` を付けて再送する。会話イベント
（app_mention / DM）は ack で止まらないため、素朴に処理すると同一発話が複数回
会話キューへ投入され、1 発話が 2 ターン分の会話として扱われる（設計書 §8.3 の再送の冪等化）。

u-zu は単一プロセス常駐なので、プロセス内メモリの seen セットで十分に重複排除できる
（Slack の再送は数分内・同一プロセスへ届く）。無制限成長を避けるため TTL で掃除する。

構築手順書: docs/procedures/03-slack-bot.md
"""

import threading
import time

# 再送は通常 1 分以内に収束する。余裕を持って 10 分保持すれば取りこぼさない。
_TTL_SECONDS = 600

_seen: dict[str, float] = {}
_lock = threading.Lock()


def seen_before(event_id: str) -> bool:
    """event_id を初見なら記録して False、既知なら True を返す（True=再送＝処理しない）。

    空の event_id は重複判定できないため常に False（＝処理する）を返す。
    Bolt のハンドラは複数スレッドで並行実行され得るため、seen セットはロックで保護する。
    """
    if not event_id:
        return False
    now = time.monotonic()
    with _lock:
        # 期限切れエントリを掃除（TTL 超過分を落とし、セットの無制限成長を防ぐ）。
        for k in [k for k, t in _seen.items() if now - t > _TTL_SECONDS]:
            del _seen[k]
        if event_id in _seen:
            return True
        _seen[event_id] = now
        return False
