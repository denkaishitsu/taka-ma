"""decide デーモン — headless 承認判定の常駐サーバ（Mac mini・launchd 常駐）。

MBP の PreToolUse フックが SSH で起動する薄いクライアント（decide_client.py）から
Unix ドメインソケット経由で判定リクエストを受け、CLI 非依存の承認中核
ApprovalPipeline.decide() を常駐状態で実行して {"allow", "reason"} を返す。

旧方式（decide_cli.py の 1 ショット SSH 起動）はツール呼び出しごとに Python コールド
スタート＋依存 import（yaml / slack_sdk / ai_gateway）＋config ロード＋SlackNotifier
構築を払い、承認レイテンシがツール数に比例して累積した。本デーモンは起動時に一度
だけ構築して常駐する（設計: 本体 §8.5 / Appendix_worker-execution-adapters §2.1）。

- 並行性: 接続ごとに asyncio タスクで捌く。Tier3 人間待ち（最大 300 秒）が他 worker の
  判定をブロックしない。
- 障害の波及範囲: 1 判定内の例外はその接続への deny 応答に閉じる（デーモンは落とさない）。
  デーモンはタスク状態を持たない（Tier3 待ちも §8.10 の承認ファイル＝ディスク上）ため、
  デーモン再起動でも worker タスク（MBP 側 claude -p）は消失しない。
- config 再ロード: リクエストごとに ya-ta.yaml / sa-ru.yaml / pipeline.yaml の mtime を
  照合し、変化していればパイプラインを再構築する（デーモン再起動不要）。
- 運用: launchd（com.taka-ma.decide-daemon・KeepAlive=true で crash 自動再起動）。

構築手順書: docs/procedures/08-approval-pipeline.md
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from approval_types import PendingApproval

logger = logging.getLogger("decide-daemon")

# config パス（sa-ru の __main__ と同じ既定。テスト・別環境向けに env で上書き可）。
_YA_TA_CONFIG = os.environ.get("YA_TA_CONFIG", "/opt/taka-ma/ya-ta/config/ya-ta.yaml")
_SA_RU_CONFIG = os.environ.get("SA_RU_CONFIG", "/opt/taka-ma/sa-ru/config/sa-ru.yaml")
# pipeline.yaml は ApprovalPipeline が自モジュール相対でロードする（main._PIPELINE_YAML と同一パス）。
# ここでは mtime 監視（再ロード判定）のためだけに同じ実体を参照する。
_PIPELINE_CONFIG = str(Path(__file__).parent / "config" / "pipeline.yaml")
_SOCKET_PATH = os.environ.get("DECIDE_SOCKET", "/opt/taka-ma/data/decide.sock")

# 1 判定の上限（秒）。Tier3 人間待ち最大 300 秒＋ハンドラ処理の余裕。クライアントの
# 応答待ち（308 秒）・フック timeout（310 秒）より内側に置き、判定側のハング（ya-ta
# 障害等）も必ずクライアントへの deny 応答で確定させる（Appendix §2.1 タイムアウト設計）。
_DECIDE_TIMEOUT_SEC = 305
# リクエスト 1 行の受信上限（バイト）。asyncio ストリームの既定 limit（64KB）のままだと、
# Write 等の大きな tool_input を含むフック payload が LimitOverrunError となり誤 deny される
# （正当なツール実行が内容の大きさだけで拒否される）ため、余裕を持って引き上げる。
_SOCKET_READ_LIMIT = 16 * 1024 * 1024
# リクエスト行の受信上限（秒）。接続だけ張って送らないクライアントの滞留を防ぐ。
_REQUEST_READ_TIMEOUT_SEC = 10


class PipelineHolder:
    """ApprovalPipeline を常駐保持し、config 変更（mtime）検知時のみ再構築する。"""

    _WATCH_FILES = (_YA_TA_CONFIG, _SA_RU_CONFIG, _PIPELINE_CONFIG)

    def __init__(self):
        self._pipeline = None
        self._mtimes: tuple = ()

    def _current_mtimes(self) -> tuple:
        # 不在ファイルは None として扱う（出現・消失そのものも「変更」として検知する）。
        result = []
        for path in self._WATCH_FILES:
            try:
                result.append(os.stat(path).st_mtime_ns)
            except OSError:
                result.append(None)
        return tuple(result)

    def get(self):
        """現行 config のパイプラインを返す（mtime 変化時のみ再構築）。"""
        mtimes = self._current_mtimes()
        if self._pipeline is None or mtimes != self._mtimes:
            self._pipeline = self._build()
            self._mtimes = mtimes
            logger.info("ApprovalPipeline を構築しました（config mtime: %s）", mtimes)
        return self._pipeline

    @staticmethod
    def _build():
        # 依存（yaml / slack_sdk / ai_gateway）はここで初めて import する。デーモン起動時に
        # main() が serve 前に一度 get() するため、import・config 不備は「起動失敗」として
        # launchd ログに顕在化する（リクエスト処理中に初めて踏むことはない）。テストは
        # Holder ごと差し替えるため、この重い import に依存しない。
        import yaml
        from main import ApprovalPipeline
        from slack_notifier import SlackNotifier
        config = {
            **yaml.safe_load(Path(_YA_TA_CONFIG).read_text()),
            **yaml.safe_load(Path(_SA_RU_CONFIG).read_text()),
        }
        return ApprovalPipeline(config, slack_notifier=SlackNotifier(),
                                ssh_host=config["ssh"]["mbp_host"])


class DecideDaemon:
    """Unix ドメインソケット上で「1 接続 = 1 判定」を捌く asyncio サーバ。

    Args:
        socket_path: 待ち受ける UDS のパス（クライアントの --socket と一致させる）。
        holder: ApprovalPipeline の供給元（テストでは FakeHolder に差し替える）。
        decide_timeout: 1 判定の上限秒（テストでは短縮して timeout 経路を検証する）。
    """

    def __init__(self, socket_path: str = _SOCKET_PATH, holder: PipelineHolder | None = None,
                 decide_timeout: float = _DECIDE_TIMEOUT_SEC):
        self.socket_path = socket_path
        self.holder = holder or PipelineHolder()
        self.decide_timeout = decide_timeout

    async def handle(self, reader, writer):
        """1 判定リクエストを処理する。例外はこの接続への deny 応答に閉じる（fail-closed）。"""
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=_REQUEST_READ_TIMEOUT_SEC)
            response = await self._decide(raw)
        except Exception as e:
            # 判定不能＝deny（fail-safe）。デーモン自体は落とさず、並行中の他判定へ波及させない。
            response = {"allow": False, "reason": f"decide_daemon error (fail-safe deny): {e}"}
        try:
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
        except Exception:
            # 応答書き込み失敗（クライアント切断等）。クライアント側は無応答＝exit 2 に確定
            # するため、ここでは接続を閉じるだけでよい。
            pass
        finally:
            writer.close()

    async def _decide(self, raw: bytes) -> dict:
        """リクエスト 1 行 JSON → PendingApproval → 中核 decide() → 応答 dict。"""
        req = json.loads(raw.decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("request is not a JSON object")
        payload = req.get("payload") or {}
        pending = PendingApproval(
            tool_name=payload.get("tool_name", ""),
            tool_input=payload.get("tool_input") or {},
            tool_use_id=payload.get("tool_use_id", ""),
        )
        # task_id はクライアント argv 優先。未指定なら cwd（=/opt/taka-ma/work/{task_id}）末尾から補う。
        task_id = req.get("task_id") or os.path.basename((payload.get("cwd") or "").rstrip("/"))
        # deadline: 前段（リスク分類・qu-e 審査）の所要時間が Tier3 の人間待ちを圧迫しても、
        # 「内側（Tier3）が先に確定」を保つため、decide 全体の締切を中核へ渡す。5 秒の余白は
        # Tier3 確定後の監査記録・done/ 退避・応答書き込みが wait_for に切られないための猶予。
        decision = await asyncio.wait_for(
            self.holder.get().decide(
                pending,
                instance_id=req.get("instance_id") or "",
                team_id=req.get("team_id"),
                channel=req.get("channel"),
                task_id=task_id,
                thread_ts=req.get("thread_ts"),
                deadline=time.monotonic() + self.decide_timeout - 5.0,
            ),
            timeout=self.decide_timeout,
        )
        return {"allow": bool(decision.allow), "reason": decision.reason or ""}

    async def start(self):
        """UDS の待ち受けサーバを生成して返す（serve() とテストの共通経路）。"""
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        # 前回異常終了の残骸ソケットは bind を妨げるため除去する（本デーモンは launchd で
        # 単一インスタンス管理のため、稼働中の別インスタンスを踏む事故はない）。
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = await asyncio.start_unix_server(
            self.handle, path=self.socket_path, limit=_SOCKET_READ_LIMIT)
        # 同一ユーザーのみ接続可（別ユーザーからの判定リクエスト偽装を防ぐ）。
        os.chmod(self.socket_path, 0o600)
        return server

    async def serve(self):
        """UDS の待ち受けを開始して常駐する。"""
        server = await self.start()
        logger.info("decide デーモン待受開始: %s", self.socket_path)
        async with server:
            await server.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    daemon = DecideDaemon()
    # 起動時に構築まで完了させる。import・config の不備をここで顕在化させ、launchd の
    # 再起動ループ＋エラーログで気付けるようにする（リクエスト処理中に初めて踏ませない）。
    daemon.holder.get()
    asyncio.run(daemon.serve())


if __name__ == "__main__":
    main()
