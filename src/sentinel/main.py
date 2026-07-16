"""qu-e — 守護プロセス エントリポイント。

構築手順書: docs/procedures/07-sentinel.md
- file_audit 監視（src/sentinel/file_auditor.py）
- ヘルスチェック（src/sentinel/health_checker.py）
- リソース最適化通知（src/sentinel/resource_optimizer.py、設計書 §8.14）
- task_context 受信（A1 §5 / 設計書 §8.13）
- jsonl retention rotation（A1 §4）
"""

import asyncio
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
import uuid

import yaml
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from sentinel.reviewer import QueReviewer
from sentinel.health_checker import HealthChecker
from sentinel.resource_optimizer import ResourceOptimizer
from sentinel.file_auditor import DynamicWatchManager, start_audit, rotate_jsonl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("qu-e")


class TaskContextHandler(FileSystemEventHandler):
    """sa-ru が SSH push する task_context json を即時受信し、メモリ dict に反映（§8.13）。"""

    def __init__(self, store: dict, watch_manager=None):
        """task_id → context dict を共有する store と、動的監視マネージャを受け取る。

        watch_manager（DynamicWatchManager）は、workspace が静的 watch_paths 外の
        実開発リポジトリを指すタスクの監視登録・解除に使う（§8.12 動的監視）。
        """
        self.store = store
        self.watch_manager = watch_manager

    def on_created(self, event):
        """新規 task_context json の作成イベント。"""
        self._load(event)

    def on_modified(self, event):
        """既存 task_context json の更新イベント（status 遷移等）。"""
        self._load(event)

    def _load(self, event):
        """イベント経由の入口。ディレクトリ・json 以外を除外して load_file へ委譲する。"""
        # 監視ディレクトリ直下の json 以外（ディレクトリ・一時ファイル）は対象外
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        self.load_file(event.src_path)

    def load_file(self, path: str):
        """task_context json を読み込み、status により store を更新または除去する。

        watchdog イベントと起動時初期スキャン（§8.13）の両方から呼ばれる共通実体。
        終了系 status（completed/failed）は store から外し、それ以外（in_progress 等）は
        最新内容で上書きする。読み込み中の書き込み等で壊れた json を踏んでも監視を
        止めないよう、例外はログに残して握り潰す。
        """
        try:
            with open(path) as f:
                ctx = json.load(f)
            # task_id の無いファイルは紐付け先が無いので無視
            task_id = ctx.get("task_id")
            if not task_id:
                return
            # workspace は sa-ru から絶対パスで届く契約（§8.13 repo: 検証）だが、`~` 前置きが
            # 混入しても照合（_pick_task_context の abspath 接頭辞一致）と動的監視が成立する
            # よう、qu-e 自身（MBP ローカル）の home で防御的に展開してから保持する
            if ctx.get("workspace"):
                ctx["workspace"] = os.path.expanduser(ctx["workspace"])
            status = ctx.get("status")
            if status in ("completed", "failed"):
                # タスク終了時は store から除去（指示範囲を保持しない）
                self.store.pop(task_id, None)
                # 実開発リポジトリの動的監視も解除する（参照が残る間は manager 側が維持）
                if self.watch_manager:
                    self.watch_manager.sync(task_id, None)
            else:
                # 実行中タスクは最新の文脈で上書き（status 遷移や workspace 確定を反映）
                self.store[task_id] = ctx
                # workspace が静的 watch_paths 外なら動的監視へ登録（§8.12 動的監視）
                if self.watch_manager:
                    self.watch_manager.sync(task_id, ctx)
            # 受信を可視化する（構築手順書 07-sentinel.md 動作確認6の検証観点。
            # 従来は成功パスに一切ログが無く、受信の有無をログから確認できなかった）
            logger.info("task_context received: task_id=%s status=%s", task_id, status)
        except Exception:
            # 壊れた json 等を踏んでも受信ループは止めない
            logger.exception("task_context 読み込み失敗: %s", path)


async def health_check_loop(checker: HealthChecker, interval: int):
    """interval 秒間隔でヘルスチェックを実行し、warning/critical 時にログ警告。

    1 回の反復の例外（設定キー欠落・psutil の一時失敗等）でループを止めない
    （§4.2「常駐ループの堅牢化」）。ループが例外で消滅するとプロセス生存のまま
    監視だけが止まり false healthy になるため、記録して次周期へ継続する。
    """
    while True:
        try:
            # check_all() は psutil.cpu_percent(interval=1) と subprocess.run(ping) の同期ブロックを含むため
            # asyncio.to_thread で別スレッド実行し、event loop を塞がない。
            result = await asyncio.to_thread(checker.check_all)
            overall = result["overall"]
            if overall != "healthy":
                logger.warning("ヘルスチェック: %s — %s", overall, result)
            else:
                logger.info("ヘルスチェック: healthy")
        except asyncio.CancelledError:
            # シャットダウン時の cancel はループ終了の正規経路。握り潰さず伝播する
            raise
        except Exception:
            logger.exception("ヘルスチェック反復失敗（ループは継続）")
        await asyncio.sleep(interval)


def _push_resource_notify(payload: dict, notify_dir: str, ssh_host: str):
    """リソース最適化通知 payload を sa-ru の notify_dir に SSH push する（§8.14）。

    qu-e と sa-ru は別マシンのため、sa-ru 側の notify_dir に JSON ファイルを置く形で
    通知する。ファイル名は衝突回避のため uuid とし、同期的に実行されるので呼び出し側で
    別スレッドへ逃がす想定（check=True で失敗時は例外を投げ、呼び出し側が記録する）。
    """
    # 受信側ディレクトリを作ってから標準入力で JSON を流し込む（1 コマンドで mkdir + 書き込み）
    remote = f"{notify_dir}/{uuid.uuid4().hex}.json"
    subprocess.run(
        ["ssh", ssh_host, f"mkdir -p {notify_dir} && cat > {remote}"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, timeout=10, check=True,
    )


async def resource_notify_loop(optimizer: ResourceOptimizer, thresholds: dict,
                               notify_dir: str, ssh_host: str, interval: int):
    """interval 秒間隔で推奨 heavy 並行数を算出し、前回値から変化したら sa-ru へ SSH push（§8.14）。

    送信トリガは「推奨並行数が現行値から変化したとき」（メモリ使用率しきい値の跨ぎ）。
    SSH push 失敗時はログ記録のみで継続する（次周期で再送される）。
    推奨値の算出自体の例外（しきい値キー欠落・psutil 一時失敗等）でもループを止めない
    （§4.2「常駐ループの堅牢化」。従来は push のみ guard され算出の例外でループが沈黙死した）。
    """
    last_sent = None
    while True:
        try:
            payload = await asyncio.to_thread(
                optimizer.notify_payload,
                thresholds["memory_warning"], thresholds["memory_critical"],
            )
            recommended = payload["recommended_heavy_instances"]
            if recommended != last_sent:
                try:
                    await asyncio.to_thread(_push_resource_notify, payload, notify_dir, ssh_host)
                    last_sent = recommended
                    logger.info("リソース最適化通知 push: %s", payload)
                except Exception:
                    logger.exception("リソース最適化通知 push 失敗: %s", payload)
        except asyncio.CancelledError:
            # シャットダウン時の cancel はループ終了の正規経路。握り潰さず伝播する
            raise
        except Exception:
            logger.exception("リソース最適化通知 反復失敗（ループは継続）")
        await asyncio.sleep(interval)


async def daily_rotation_loop(log_dir: str, retention_days: int):
    """日次で retention 超過の jsonl を削除（A1 §4）。"""
    while True:
        # 翌日 00:00 までスリープ
        now = datetime.datetime.now()
        tomorrow = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        await asyncio.sleep((tomorrow - now).total_seconds())
        try:
            rotate_jsonl(log_dir, retention_days)
        except Exception:
            logger.exception("daily rotation 失敗")


async def main():
    """qu-e daemon エントリポイント。

    起動シーケンス:
      1. config/qu-e.yaml ロード
      2. QueReviewer / HealthChecker 構築
      3. 起動時 retention rotation（A1 §4）
      4. file_audit Observer 起動（§8.12）
      5. task_context 受信 Observer 起動（§8.13）
      6. SIGTERM/SIGINT ハンドラ登録
      7. health_check_loop / daily_rotation_loop / resource_notify_loop を asyncio タスクとして起動
      8. stop_event で停止待機
    """
    with open("config/qu-e.yaml") as f:
        config = yaml.safe_load(f)

    reviewer = QueReviewer(
        model=config["qu-e"]["model"],
        ollama_host=config["qu-e"]["ollama_url"],
        prompts_dir=config["qu-e"]["prompts_dir"],
        # Tier2（review_cli の別プロセス）と同一パスを指し推論を跨プロセス直列化する（§4.2）。
        # ロックパス・審査 LLM タイムアウトとも qu-e.yaml が唯一の源（コード既定値なし）
        inference_lock=config["qu-e"]["inference_lock"],
        review_timeout_sec=config["qu-e"]["review_timeout_sec"],
    )

    thresholds = config["health_check"]["thresholds"]
    mac_mini_host = config["file_audit"]["mac_mini_host"]
    checker = HealthChecker(thresholds, mac_mini_host=mac_mini_host)
    interval = config["health_check"]["interval_sec"]

    # リソース最適化（§8.14）— 推奨 heavy 並行数を sa-ru へ通知する optimizer
    res_cfg = config["resource_optimization"]
    optimizer = ResourceOptimizer(res_cfg)
    res_notify_dir = res_cfg["o_moi_notify_dir"]
    res_notify_interval = res_cfg["notify_interval_sec"]

    # task_context 受信用のメモリ dict（task_id → context）
    task_context_store: dict[str, dict] = {}

    # 起動時 retention rotation（A1 §4）。log_dir は本来 start_audit() の
    # FileAuditHandler.__init__ が作成するが、呼び出し順が rotate_jsonl → start_audit
    # のため初回起動時（ディレクトリ未作成）は os.listdir が FileNotFoundError になる
    # （実機検証で再現）。rotation はディレクトリ存在が前提の処理なので、ここで先に
    # 作成してから呼ぶ。
    log_dir = config["file_audit"]["log_dir"]
    retention_days = config["file_audit"]["retention_days"]
    os.makedirs(log_dir, exist_ok=True)
    try:
        rotate_jsonl(log_dir, retention_days)
    except Exception:
        logger.exception("起動時 rotation 失敗")

    loop = asyncio.get_running_loop()

    # ファイル監査開始（FileAuditHandler は task_context_store を参照）
    audit_observer, audit_handler = start_audit(config, reviewer, task_context_store, loop)

    # 実開発リポジトリの動的監視（§8.12）— task_context の workspace で登録・解除する
    watch_manager = DynamicWatchManager(
        audit_observer, audit_handler,
        static_roots=config["file_audit"]["watch_paths"],
        commit_gate=config["file_audit"]["commit_gate"])

    # task_context 受信（§8.13）— watchdog で即時反映
    ctx_dir = config["task_context"]["dir"]
    os.makedirs(ctx_dir, exist_ok=True)
    ctx_handler = TaskContextHandler(task_context_store, watch_manager)
    ctx_observer = Observer()
    ctx_observer.schedule(ctx_handler, ctx_dir, recursive=False)
    ctx_observer.start()

    # 起動時初期スキャン（§8.13）: qu-e 停止中に push された既存 task_context を読み込む。
    # 取りこぼすと実行中タスクの変更が匿名（status=none）と誤判定されアラートが濫発する。
    # Observer 起動「後」に走査することでスキャンとイベントの隙間を無くす（同一ファイルを
    # 両経路で読んでも load_file は上書き（冪等）なので二重読みは無害）。
    for name in sorted(os.listdir(ctx_dir)):
        if name.endswith(".json"):
            ctx_handler.load_file(os.path.join(ctx_dir, name))

    stop_event = asyncio.Event()

    def shutdown():
        """SIGTERM/SIGINT 受信時、両 Observer を停止して stop_event を立てる。"""
        logger.info("シャットダウンシグナル受信")
        audit_observer.stop()
        ctx_observer.stop()
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, shutdown)
    loop.add_signal_handler(signal.SIGINT, shutdown)

    logger.info("qu-e 起動")

    health_task = asyncio.create_task(health_check_loop(checker, interval))
    rotation_task = asyncio.create_task(daily_rotation_loop(log_dir, retention_days))
    resource_task = asyncio.create_task(
        resource_notify_loop(optimizer, thresholds, res_notify_dir, mac_mini_host, res_notify_interval)
    )

    await stop_event.wait()
    health_task.cancel()
    rotation_task.cancel()
    resource_task.cancel()
    audit_observer.join()
    ctx_observer.join()
    logger.info("qu-e 停止")


if __name__ == "__main__":
    asyncio.run(main())
