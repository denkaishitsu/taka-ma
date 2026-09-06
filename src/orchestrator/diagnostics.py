"""ハング診断（設計書 §8.5「ハング診断の常設」・ADR 0002）。

sa-ru が「プロセスは生きているがログが止まっている」状態（2026-09-04 18:46 の実例）に
なったとき、再起動の前に全スレッドの Python スタックを採取できるようにする。
faulthandler は C レベルで動くため、イベントループもスレッドプールも死んでいても効く。

使い方（運用手順書 runbook-shutdown-restart.md「ハング時の手順」）:
    kill -USR1 <sa-ru の pid>   → stderr（launchd の StandardErrorPath = sa-ru-error.log）へ出力
"""

import faulthandler
import signal
import sys


def install_hang_diagnostics(stream=None) -> int:
    """SIGUSR1 で全スレッドのスタックを stream（既定 stderr）へ吐く登録を行う。

    戻り値は登録したシグナル番号（テストが同じ番号を送るため）。既に登録済みなら
    faulthandler 側で上書きされる（多重登録で壊れない）。
    """
    faulthandler.register(signal.SIGUSR1, file=stream or sys.stderr, all_threads=True)
    return signal.SIGUSR1
