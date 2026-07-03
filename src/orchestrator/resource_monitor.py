"""リソースモニター — MBP の Blender 稼働を監視し、競合時に LLM を退避する（§7.1）。

MBP は worker 実行ハブと Blender レンダリングを兼ねるため、両者が GPU/メモリを取り合う。
Blender 起動を検知したら ollama を停止して資源を譲り（レンダリング優先）、Blender 終了で
通常モードへ戻す。停止の実体は RemoteProcessManager.stop_ollama() に委譲する（SSOT）。
"""

import asyncio
import logging
import subprocess

logger = logging.getLogger("sa-ru.resource_monitor")


class ResourceMonitor:
    """Blender 稼働を一定間隔で監視し、検知/終了の遷移で ollama を停止/再開可能にする（§7.1）。"""

    def __init__(self, check_interval: int = 10, *, process_mgr):
        """監視間隔と停止委譲先を受け取る。

        Args:
            check_interval: Blender 検知ポーリングの間隔（秒）。
            process_mgr: ollama 停止を委譲する RemoteProcessManager。SSH 先ホスト・
                タイムアウトもこのインスタンスが保持する値を共有する（供給元を 1 つに保つ）。
                キーワード必須＝未注入なら構築時に落とし、実行時 AttributeError を防ぐ。
        """
        self.check_interval = check_interval
        # Blender 検知中フラグ。検知→未検知の立ち上がり/立ち下がりエッジでのみ動作させるための状態保持
        self.blender_running = False
        # ollama 停止の実体は RemoteProcessManager.stop_ollama() に集約（SSOT）。停止ロジックの
        # 二重実装を避けるため、ここでは停止対象を持たず注入された process_mgr へ委譲する。
        # SSH 先ホスト・タイムアウトも process_mgr が保持する値を共有する（host/timeout の供給元を
        # 1 つに保つ）。process_mgr はキーワード必須＝未注入は構築時に落とし、実行時 AttributeError を防ぐ。
        self.process_mgr = process_mgr

    def detect_blender(self) -> bool:
        """MBP上のBlenderプロセスを検知"""
        # SSH 先ホスト・軽い SSH 操作のタイムアウト（sa-ru.yaml ssh.timeout_sec）は process_mgr が
        # 保持する値を SSOT として共有する（コード側に既定値も別ソースも持たない）。
        result = subprocess.run(
            ["ssh", self.process_mgr.ssh_host, "pgrep -x Blender"],
            capture_output=True, timeout=self.process_mgr.ssh_timeout,
        )
        return result.returncode == 0

    def _stop_llms(self) -> None:
        """MBP で稼働中の ollama モデルを停止する（§7.1 GPU/メモリ解放）。

        停止ロジックの実体は RemoteProcessManager.stop_ollama() に集約（SSOT）。Blender 自動停止/
        将来の手動停止/アイドルスリープが同一実装を共有するため、ここは委譲のみ。
        """
        self.process_mgr.stop_ollama()

    async def watch(self):
        """定期的にBlenderプロセスを監視。

        Orchestrator.run() の asyncio.gather に乗る常駐コルーチン。SSH 呼び出しは同期
        ブロッキングのため to_thread でワーカースレッドに逃がし、イベントループ
        （dispatcher / worker）を止めない。ollama は次回リクエストで自動ロードされるため、
        Blender 終了時の「再開」は明示コマンド不要でフラグを戻すだけでよい（§7.1）。

        ループ本体は try/except で包む。本コルーチンは return_exceptions なしの gather に
        乗るため、SSH の OSError / TimeoutExpired 等が伝播すると他の常駐コルーチンごと
        Orchestrator.run() が落ちる。他コルーチンと同じ per-iteration 防御に揃える。
        """
        logger.info("リソースモニター開始 (間隔: %ds)", self.check_interval)
        while True:
            try:
                blender_detected = await asyncio.to_thread(self.detect_blender)
                if blender_detected and not self.blender_running:
                    logger.info("Blender検知 — LLM一時停止")
                    await asyncio.to_thread(self._stop_llms)
                    self.blender_running = True
                elif not blender_detected and self.blender_running:
                    logger.info("Blender終了 — LLM再開")
                    self.blender_running = False
            except Exception:
                logger.exception("ResourceMonitor watch の1巡が失敗。継続します")
            await asyncio.sleep(self.check_interval)
