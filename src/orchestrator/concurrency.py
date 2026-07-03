"""動的並行数リミッタ — 実行時に上限を変更できる Semaphore 代替（設計書 §8.14）。

`asyncio.Semaphore` は生成後に上限を変更できないため、qu-e からのリソース最適化通知
（§8.14）で heavy worker の `max_heavy_instances` を実行時に増減させる用途に用いる。
"""

import asyncio


class DynamicConcurrencyLimiter:
    """実行時に上限を変更できる並行数制御。`asyncio.Semaphore` のドロップイン代替。

    - `acquire()` / `release()` および `async with` をサポート
    - `set_limit(n)` で上限を変更。増加時は待機中タスクを即時起こし、減少時は
      アクティブ数が新上限を下回るまで新規 `acquire()` がブロックされる
      （実行中タスクは強制終了しない＝OOM 回避は新規起動の抑制で実現する）
    """

    def __init__(self, limit: int):
        """初期上限と内部状態を用意する。limit は最低 1 に丸める（0/負値での全閉塞を防ぐ）。"""
        # 同時に acquire を許す上限。set_limit で実行時に増減する
        self._limit = max(1, limit)
        # 現在 acquire 済みで release 待ちの数（アクティブ数）
        self._active = 0
        # 上限変更・release を待機側へ通知するための条件変数（待機/起床はこの 1 本で同期）
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        """現在の同時実行上限。"""
        return self._limit

    async def acquire(self):
        """枠を 1 つ確保する。アクティブ数が上限に達していれば空くまで待機する。"""
        async with self._cond:
            # 上限到達中は release / set_limit による通知を待つ。再判定するため while で囲う
            while self._active >= self._limit:
                await self._cond.wait()
            self._active += 1

    async def release(self):
        """枠を 1 つ返す。待機中タスクを起こせるよう全員に通知する。"""
        async with self._cond:
            self._active -= 1
            self._cond.notify_all()

    async def set_limit(self, new_limit: int):
        """上限を変更する。増加分は待機中タスクへ即時開放される。"""
        async with self._cond:
            self._limit = max(1, new_limit)
            self._cond.notify_all()

    async def __aenter__(self):
        """`async with` 入口。枠を確保して自身を返す。"""
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        """`async with` 出口。例外の有無に関わらず枠を必ず返す（解放漏れによる枯渇を防ぐ）。"""
        await self.release()
