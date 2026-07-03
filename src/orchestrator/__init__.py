"""sa-ru オーケストレーション本体 — タスクキュー監視、分解、連鎖実行。

構築手順書: docs/procedures/05-orchestrator.md Step 8（パイプライン統合）
関連: 設計書 §1.3 / §2.2 / §8.4 / §10
"""

import sys
sys.path.insert(0, "/opt/taka-ma/ya-ta")
# approval-pipeline はハイフン dir でパッケージ import 不可のため sys.path 経由で bare import する
sys.path.insert(0, "/opt/taka-ma/sa-ru/approval-pipeline")

import asyncio
import datetime
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from ai_gateway.decomposer import TaskDecomposer
from ai_gateway.classifier import TaskClassifier
from ai_gateway.risk_classifier import RiskClassifier
from orchestrator.process_manager import RemoteProcessManager
from orchestrator.slack_notifier import SlackNotifier
from orchestrator.pty_wrapper import WorkerPtyWrapper
from orchestrator.concurrency import DynamicConcurrencyLimiter
from orchestrator.conversation import ConversationManager
from orchestrator.resource_monitor import ResourceMonitor
from orchestrator.file_queue import FileQueue


def _select_method(model_conf: dict, use_case: str = "default") -> str:
    """モデルの methods 配列と用途から呼び出し経路を選択する。

    use_case:
      - "default":      通常の振り分け。methods に pty があれば pty、無ければ subprocess
      - "cross_review": 並行投入用。subprocess 優先（対話不要）
      - "multimodal":   マルチモーダル単発。subprocess 優先

    旧 method (単数) フィールドにも後方互換で対応する。
    """
    methods = model_conf.get("methods")
    if methods is None:
        legacy = model_conf.get("method")
        methods = [legacy] if legacy else []

    if use_case in ("cross_review", "multimodal") and "subprocess" in methods:
        return "subprocess"
    if "pty" in methods:
        return "pty"
    if "subprocess" in methods:
        return "subprocess"
    return "pty"

logger = logging.getLogger("sa-ru.orchestrator")

TASK_DIR = "/opt/taka-ma/data/tasks"
# 承認ファイルのディレクトリは Tier3Handler（approval-pipeline/tier3_handler.py）が
# `TAKA_MA_APPROVAL_DIR` で一元管理する。ここでは持たない（旧・未使用定数を撤去）。
POLL_INTERVAL = 5  # 秒


class FileAuditHandler(FileSystemEventHandler):
    """qu-e からの file_audit アラート（json）を watchdog で即時検知し、
    Slack に Approve/Reject ボタン付きで転送する（§8.12）。

    A1 §2「即時通知」を満たすためポーリングではなくイベント駆動。
    処理済みアラートは `{alert_dir}/done/` に退避して履歴を残す。
    """

    def __init__(self, alert_dir: str, slack_notifier: SlackNotifier,
                 process_manager: RemoteProcessManager):
        """監視対象 alert_dir と Slack 転送先を受け取る。

        Args:
            alert_dir: qu-e が SSH push する file_audit アラート json の置き場（ここを監視する）。
            slack_notifier: アラートを Approve/Reject ボタン付きで Slack へ送る通知器。
            process_manager: 将来の操作委譲用に保持する（現状アラート転送では未使用）。
        """
        self.alert_dir = alert_dir
        # 転送済みアラートの退避先（履歴保持・再転送防止）。起動時に用意しておく
        self.done_dir = f"{alert_dir}/done"
        self.slack = slack_notifier
        self.pm = process_manager
        os.makedirs(self.done_dir, exist_ok=True)

    def on_created(self, event):
        """qu-e が ssh で `cat > {alert_dir}/{audit_log_id}.json` で書き込んだ瞬間に発火。"""
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        try:
            with open(event.src_path) as fp:
                alert = json.load(fp)
            self.slack.send_file_audit_alert(alert)
            shutil.move(event.src_path, f"{self.done_dir}/{Path(event.src_path).name}")
        except Exception:
            logger.exception("file_audit アラート処理失敗: %s", event.src_path)


class ResourceNotifyHandler(FileSystemEventHandler):
    """qu-e からのリソース最適化通知（json）を watchdog で即時検知し、
    heavy 並行数上限（`max_heavy_instances`）を動的更新する（§8.14）。

    qu-e の `HealthChecker` / `ResourceOptimizer` がメモリ使用率しきい値を跨いだとき、
    推奨並行数を SSH push する。逼迫時は新規 heavy 起動を抑制（OOM 回避）、
    余裕時は上限まで許可（throughput 最大化）。処理済み通知は `{notify_dir}/done/` に退避。
    """

    def __init__(self, notify_dir: str, limiter: DynamicConcurrencyLimiter,
                 loop: asyncio.AbstractEventLoop):
        """監視対象 notify_dir と更新対象リミッタ・イベントループを受け取る。

        Args:
            notify_dir: qu-e がリソース最適化通知 json を SSH push する置き場（ここを監視する）。
            limiter: heavy 並行数上限を保持する動的リミッタ。通知の推奨値で set_limit する。
            loop: watchdog のワーカースレッドから set_limit（コルーチン）を委譲する先のループ。
        """
        self.notify_dir = notify_dir
        # 処理済み通知の退避先（再適用防止）。起動時に用意しておく
        self.done_dir = f"{notify_dir}/done"
        self.limiter = limiter
        self.loop = loop
        os.makedirs(self.done_dir, exist_ok=True)

    def on_created(self, event):
        """qu-e が ssh で `cat > {notify_dir}/{id}.json` で書き込んだ瞬間に発火。"""
        if event.is_directory or not event.src_path.endswith(".json"):
            return
        try:
            with open(event.src_path) as fp:
                notify = json.load(fp)
            recommended = int(notify["recommended_heavy_instances"])
            # set_limit はコルーチン。watchdog のスレッドから event loop へ委譲する。
            asyncio.run_coroutine_threadsafe(
                self.limiter.set_limit(recommended), self.loop
            )
            logger.info(
                "リソース最適化通知: max_heavy_instances=%d（memory=%s%%, level=%s）",
                recommended, notify.get("memory_usage"), notify.get("level"),
            )
            shutil.move(event.src_path, f"{self.done_dir}/{Path(event.src_path).name}")
        except Exception:
            logger.exception("リソース最適化通知処理失敗: %s", event.src_path)


class Orchestrator:
    """sa-ru の中核。タスク受付から分解・連鎖実行・承認・通知までの常駐ループ群を束ねる。

    run() で dispatcher（タスク監視→分解→キュー投入）、light/heavy ワーカー、会話・着手確認・
    制御の各受信ループ、リソース監視を asyncio.gather で並行起動する。file_audit アラートと
    リソース最適化通知は別スレッドの watchdog Observer で受ける。各ループは _supervise で包み、
    1 つが落ちても全体を巻き添えにせず再起動する（自己修復）。設計書 §1.3 / §2.2 / §8.4 / §10。
    """

    def __init__(self, config):
        """ya-ta.yaml + sa-ru.yaml のマージ済み config から各コンポーネントを組み立てる。

        モデル系（decomposer / classifier / routing）と SSH・各種キュー dir・承認パイプライン・
        会話/制御/着手確認の受信経路・heavy 動的リミッタ・リソース監視を構築・配線する。dir や
        運用値は config を唯一の源にし（writer 側 u-zu と SSOT を保つ）、SSH 値は欠落時に即落とす。
        """
        self.config = config
        self.decomposer = TaskDecomposer(config)
        self.classifier = TaskClassifier(config)
        self.risk_classifier = RiskClassifier(config)

        # worker / qu-e は MBP 上にあり SSH で叩く（設計の Mac mini=司令塔 / MBP=実行ハブ 分担）。
        # その SSH 先ホスト・タイムアウトは sa-ru.yaml の ssh ブロックで一元管理し、SSH を行う全箇所
        # （process_mgr と承認パイプラインの Tier2→qu-e）へ同じ値を渡す＝供給元を 1 つに保つ。
        # 運用値はコードに既定を置かず必須とし、欠落時は ssh ブロックを指して即落とす（host/timeout で
        # 厳格度を揃え、設定漏れの診断位置をぶらさない）。
        ssh_conf = config["ssh"]
        mbp_host = ssh_conf["mbp_host"]
        ssh_timeout = ssh_conf["timeout_sec"]
        self.process_mgr = RemoteProcessManager(ssh_host=mbp_host, ssh_timeout=ssh_timeout)
        self.slack = SlackNotifier()

        # 承認パイプライン（worker の y/n 介入。ya-ta=in-process / qu-e=SSH、§8.8〜§8.9）
        from main import ApprovalPipeline
        self.approval_pipeline = ApprovalPipeline(config, slack_notifier=self.slack, ssh_host=mbp_host)

        # カテゴリ別キュー（FIFO、上限付き）
        self.queue_light = asyncio.Queue(maxsize=100)
        self.queue_heavy = asyncio.Queue(maxsize=10)

        # heavy の同時実行上限（動的リミッタで制御）。
        # 起動時は ya-ta.yaml の max_heavy_instances をブートストラップ値とし、
        # 実行時は qu-e のリソース最適化通知（§8.14）で動的に増減する。
        self.heavy_limiter = DynamicConcurrencyLimiter(
            config["concurrency"]["max_heavy_instances"]
        )

        # 会話フロントエンド。u-zu からの発話を脳 LLM で会話・要約し、人間の着手確認を
        # 得てから確定タスク（status=init）を TASK_DIR に生成する。生成後は既存 dispatcher が拾う。
        # タスクキューの dir は config を唯一の源にする（u-zu の writer task_queue.py と同じキー。
        # 他キューと流儀を揃え、定数直書きで writer と乖離する SSOT ギャップを作らない）。
        self.task_dir = config.get("task_queue", {}).get("dir", TASK_DIR)
        self.conversation = ConversationManager(config, self.slack, task_dir=self.task_dir)
        # 会話/着手確認の dir は config を唯一の源にする（exec_confirm と同じ流儀。定数の二重定義を避ける）
        self.conversation_dir = config["conversation"]["dir"]
        self.conversation_poll = config["conversation"].get("poll_interval_sec", 2)
        self.exec_confirm_dir = config["exec_confirm"]["dir"]
        self.exec_confirm_poll = config["exec_confirm"].get("poll_interval_sec", 2)
        self.exec_confirm_timeout = config["exec_confirm"].get("timeout_sec", 300)

        # 制御コマンド受信（§8.10c）。u-zu が controls/ に書く制御命令（手動 ollama 停止等）を
        # 監視し対応操作へ委譲する。dir は config を唯一の源にする（他キューと同じ流儀・二重定義を避ける）。
        self.control_dir = config["control"]["dir"]
        self.control_poll = config["control"].get("poll_interval_sec", 2)

        # 各待受の取り回し（列挙・パース・壊れファイル隔離・done/ 退避）を共有 FileQueue に集約する。
        # ループ固有の判断（ready とする status・処理後の扱い）は各ループ側に残す。
        # 待受方式は現状の poll を踏襲。
        self.task_q = FileQueue(self.task_dir, poll_interval=POLL_INTERVAL)
        self.conversation_q = FileQueue(self.conversation_dir, poll_interval=self.conversation_poll)
        self.control_q = FileQueue(self.control_dir, poll_interval=self.control_poll)
        self.exec_confirm_q = FileQueue(self.exec_confirm_dir, poll_interval=self.exec_confirm_poll)

        # レンダリングモード自動切替（§7.1）。Blender 検知時に MBP の ollama を停止し
        # GPU/メモリを解放、終了検知で通常モードへ戻す。blender_detection: false なら無効化（None）。
        # SSH 先ホスト・タイムアウトは注入する process_mgr が保持する値を共有する（供給元を 1 つに保つ）。
        rm_conf = config["resource_management"]
        self.resource_monitor = (
            ResourceMonitor(
                check_interval=rm_conf["check_interval_sec"],
                process_mgr=self.process_mgr,  # ollama 停止の委譲・SSH 先/timeout の供給元（SSOT）
            )
            if rm_conf["blender_detection"]
            else None
        )

    async def run(self):
        """dispatcher + 2ワーカーを並行起動。watchdog Observer は別スレッドで起動（§8.12）。"""
        alert_dir = self.config["file_audit"]["alert_dir"]
        os.makedirs(alert_dir, exist_ok=True)
        self.file_audit_handler = FileAuditHandler(alert_dir, self.slack, self.process_mgr)
        self.file_audit_observer = Observer()
        self.file_audit_observer.schedule(self.file_audit_handler, alert_dir, recursive=False)
        self.file_audit_observer.start()

        # リソース最適化通知の受信（§8.14）— qu-e が SSH push する json を watchdog で監視
        loop = asyncio.get_running_loop()
        notify_dir = self.config["resource_optimization"]["notify_dir"]
        os.makedirs(notify_dir, exist_ok=True)
        self.resource_handler = ResourceNotifyHandler(notify_dir, self.heavy_limiter, loop)
        self.resource_observer = Observer()
        self.resource_observer.schedule(self.resource_handler, notify_dir, recursive=False)
        self.resource_observer.start()
        # 常駐コルーチン群。各ループは _supervise で包み、1 つの未捕捉例外が gather 経由で全体を
        # 落とさないようにする（異常終了したループだけ再起動＝自己修復）。
        # resource_monitor は blender_detection 有効時のみ加える（§7.1）。
        coros = [
            self._supervise(self._dispatcher, "dispatcher"),
            self._supervise(self._worker_light, "worker_light"),
            self._supervise(self._worker_heavy, "worker_heavy"),
            self._supervise(self._conversation_loop, "conversation_loop"),    # 会話受信
            self._supervise(self._exec_confirmation_loop, "exec_confirmation_loop"),  # 着手確認
            self._supervise(self._control_loop, "control_loop"),             # 制御命令
        ]
        if self.resource_monitor is not None:
            coros.append(self._supervise(self.resource_monitor.watch, "resource_monitor"))  # §7.1
        try:
            await asyncio.gather(*coros)
        finally:
            self.file_audit_observer.stop()
            self.file_audit_observer.join()
            self.resource_observer.stop()
            self.resource_observer.join()

    async def _supervise(self, make_coro, name: str):
        """常駐ループを監督し、未捕捉例外で死んでも他ループを巻き添えにせず再起動する（自己修復）。

        run() は asyncio.gather でループ群を束ねるため、1 つが例外を送出すると gather 全体が停止し
        daemon が落ちる。各ループをこのラッパーで包み、例外はログして短い待機後に再起動する。
        CancelledError（正常停止要求）は伝播させる。
        """
        while True:
            try:
                await make_coro()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("常駐ループ %s が異常終了。再起動します", name)
                await asyncio.sleep(1)

    # ── dispatcher: タスクファイル監視 → 分解 → キュー投入 ──

    async def _dispatcher(self):
        """タスクディレクトリを監視し、分解・分類してカテゴリ別キューに投入"""
        last_cleanup_date = None
        while True:
            today = datetime.date.today()
            if today != last_cleanup_date:
                await self._daily_cleanup()
                last_cleanup_date = today

            # 未処理（status=init）を 1 件取得し、即座に accepted へ予約する（共有 FileQueue 経由。
            # updated_at は claim が予約時に刻む。壊れた task ファイルは failed/ へ隔離され止まらない）。
            picked = self.task_q.claim("init", reserve_status="accepted")
            if not picked:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            task_file, task = picked
            # 1 タスクの分解/受付失敗で dispatcher を殺さない。例外を吸収して当該タスクを failed に
            # 落とし（in_progress のまま放置すると claim('init') で再取得されず恒久ロストになる）、
            # ユーザーへ通知してループは継続する。
            try:
                self._update_status(task_file, "in_progress")

                # DeepSeek-R1 でタスクを分解（設計書 §8.4, §10.2）
                subtasks = await asyncio.to_thread(
                    self.decomposer.decompose, task["command"]
                )

                # /exam_gw ドライラン: 判定結果のみ返却し、実行しない（設計書 §2.2）
                if task.get("dry_run"):
                    self.slack.notify(
                        self._format_exam_result(subtasks),
                        task.get("channel_id"),
                        team_id=task.get("team_id"),
                    )
                    self._update_status(task_file, "completed")
                    continue

                self.slack.notify(
                    f"タスク受付: {len(subtasks)}件のサブタスクに分解",
                    task.get("channel_id"),
                    team_id=task.get("team_id"),
                )

                # 連鎖実行を非同期タスクとして起動（dispatcher はブロックしない）
                asyncio.create_task(
                    self._execute_chain(task_file, task, subtasks)
                )
            except Exception as e:
                logger.exception("タスクの分解/受付に失敗: %s", task_file)
                try:
                    self._update_status(task_file, "failed", result=str(e))
                except Exception:
                    logger.exception("failed への更新に失敗: %s", task_file)
                try:
                    self.slack.notify(
                        f"タスクの受付に失敗しました: {e}",
                        task.get("channel_id"),
                        team_id=task.get("team_id"),
                    )
                except Exception:
                    logger.exception("受付失敗通知の送信に失敗: %s", task_file)

    # NOTE: タスク分解は TaskDecomposer (ai_gateway/decomposer.py) が担う。
    # _dispatcher() から self.decomposer.decompose() を呼び出す。
    # 分解結果: [{"step": 1, "command": "...", "category": "heavy", "depends_on": []}, ...]

    # ── 会話ループ: u-zu の発話を脳 LLM で会話・要約（§8.3 (A)） ──

    async def _conversation_loop(self):
        """会話キュー（init）を監視し、発話を ConversationManager に渡す。

        取得時に processing へ予約し（共有 FileQueue）、処理済みは done/ へ退避する（履歴・再処理防止）。
        確定タスクの生成はここではなく着手確認後（_exec_confirmation_loop）に行う。quarantine_on_error=False
        は、予約済みのため再取得されず、退避失敗時も processing のまま残せばよいことによる。
        """
        await self.conversation_q.run(
            self._handle_conversation_message,
            ready_status="init", reserve_status="processing", quarantine_on_error=False,
        )

    async def _handle_conversation_message(self, msg_file: str, msg: dict):
        """会話メッセージ 1 件を処理する。脳 LLM 呼び出しは同期ブロックのため to_thread で実行する。

        失敗は握りつぶさずユーザーへ返す（無言ドロップ防止）。例外をここで吸収するため、呼び出し元の
        run() は常に done/ へ退避する（現行挙動どおり「処理を試みたら done」）。通知自体の失敗は無視。
        """
        try:
            await asyncio.to_thread(self.conversation.handle_message, msg)
        except Exception:
            logger.exception("会話メッセージ処理失敗: %s", msg_file)
            try:
                self.slack.notify(
                    "すみません、処理に失敗しました。もう一度お願いします。",
                    msg.get("channel_id"),
                    team_id=msg.get("team_id"),
                    thread_ts=msg.get("thread_ts"),
                )
            except Exception:
                logger.exception("会話失敗通知の送信に失敗")

    # ── 制御ループ: u-zu の制御命令を実行（手動 ollama 停止、§8.10c） ──

    async def _control_loop(self):
        """制御コマンドキュー（controls/）を監視し、命令を実行して結果を Slack へ返す。

        u-zu は別プロセスなので停止本体 process_mgr.stop_ollama()（SSOT）を直接呼べない。
        u-zu が controls/ に書いた命令をここで拾い、対応操作へ委譲する（経路 Slack→u-zu→sa-ru）。
        SSH を伴う停止は同期ブロックのため to_thread で別スレッド実行する（他ループと同様）。
        処理済みは done/ に退避（再処理防止）。stop_ollama() は §7.1 どおり再起動せず、次の推論で
        ollama が自動再ロードする。
        """
        # status=pending を取得する。予約書換はしない（reserve_status 未指定）: 単一消費者なので予約
        # マークは不要で、あえて pending のまま実行することで、実行と done/ 退避の間でクラッシュしても
        # 次回起動で再実行される（取りこぼし防止）。stop_ollama は冪等なので再実行は安全。
        # 処理失敗時は quarantine_on_error=True: pending のまま残すと毎ポーリングで再実行され Slack 通知
        # ストームになるため failed/ へ隔離してループは継続する（gather 経由の全体停止も防ぐ）。
        await self.control_q.run(
            self._handle_control_record,
            ready_status="pending", quarantine_on_error=True,
        )

    async def _handle_control_record(self, ctl_file: str, ctl: dict):
        """制御命令 1 件を実行する。失敗は run() へ送出し failed/ 隔離に委ねる。"""
        await self._handle_control(ctl)

    async def _handle_control(self, ctl: dict):
        """制御命令 1 件を実行し、結果を発行元 Slack（同 channel/thread）へ返す。

        命令文字列は u-zu(control_store.py) と同じ規約値を使う（grep で両側一致を確認）。
        未知の命令は黙って捨てず Slack に明示する（u-zu 改修漏れ・契約ずれを表面化させる）。
        """
        command = ctl.get("command")
        if command == "stop_ollama":
            # 停止本体は SSOT。例外は内部で握られ、成否は dict で返る。返り値に基づき
            # 成功/該当なし/失敗を区別して報告する（偽の「停止しました」を出さない）。
            result = await asyncio.to_thread(self.process_mgr.stop_ollama)
            if not result["ok"]:
                msg = (f":x: ollama 停止に失敗しました（{result['reason']}）。"
                       "SSH 接続と MBP の ollama を確認してください")
            elif result["stopped"]:
                msg = (":white_check_mark: MBP の ollama モデルを停止しました: "
                       f"{', '.join(result['stopped'])}（次の推論で自動再ロード）")
            else:
                msg = ":information_source: 稼働中の ollama モデルはありませんでした（停止不要）"
        else:
            logger.warning("未知の制御命令を受信: %s", command)
            msg = f":x: 未知の制御命令です: `{command}`"
        try:
            self.slack.notify(
                msg, ctl.get("channel_id"),
                team_id=ctl.get("team_id"),
                thread_ts=ctl.get("thread_ts"),
            )
        except Exception:
            logger.exception("制御命令の結果通知に失敗: %s", command)

    # ── 着手確認ループ: 確認の決着を検知して確定タスクを生成（§8.3 (B)） ──

    async def _exec_confirmation_loop(self):
        """着手確認レコードをポーリングし、決着（confirmed / rejected / timeout）を処理する。

        - confirmed: ConversationManager.create_exec_task で確定タスク（status=init）を生成
                     → 既存 dispatcher が拾う（以降は現行フロー無改変）
        - rejected:  実行せず会話継続を促す
        - pending が timeout_sec を超過: 自動 timeout（§8.10 の 5 分タイムアウトと同方針）
        処理済みレコードは done/ に退避する。
        """
        while True:
            # 全件走査（pending の timeout 判定と confirmed/rejected の決着を同時に見るため pick-one では
            # なく iter_records を使う）。共有 FileQueue 経由のため、壊れたレコードは failed/ へ隔離される
            # （従来この経路だけ隔離せず continue していたドリフトを解消）。
            for path, record in self.exec_confirm_q.iter_records():
                status = record.get("status")
                if status == "pending":
                    if self._is_confirm_expired(record):
                        self._finalize_confirm(path, record, "timeout")
                    continue
                if status in ("confirmed", "rejected"):
                    self._finalize_confirm(path, record, status)
            await asyncio.sleep(self.exec_confirm_poll)

    def _is_confirm_expired(self, record: dict) -> bool:
        """pending の着手確認が timeout_sec を超過したか判定する。"""
        try:
            created = datetime.datetime.fromisoformat(record["created_at"])
        except (KeyError, ValueError):
            return False
        now = datetime.datetime.now(created.tzinfo)
        return (now - created).total_seconds() > self.exec_confirm_timeout

    def _finalize_confirm(self, path: str, record: dict, outcome: str):
        """着手確認を決着させる。先に done/ へ退避してから決着アクションを行う。

        退避を先に行う理由: (a) アクション失敗や退避失敗が次スキャンでの再決着＝確定タスクの二重生成を
        招かない（先に拾えなくする）、(b) 退避できないレコード 1 件がループを巻き添えに殺さない
        （退避失敗は failed/ 隔離に倒して return）。アクション失敗は無言にせず発行元 Slack へ通知する
        （confirmed の握り潰し防止）。退避自体が失敗してアクション未実行のまま return した場合は、
        レコードは failed/ に残り再決着されない。
        """
        try:
            self.exec_confirm_q.mark_done(path)
        except Exception:
            logger.exception("着手確認レコードの退避に失敗、隔離: %s", path)
            self.exec_confirm_q.quarantine(path)
            return
        try:
            if outcome == "confirmed":
                self.conversation.create_exec_task(record)
            elif outcome == "rejected":
                self.conversation.notify_rejected(record)
            elif outcome == "timeout":
                self.conversation.notify_timeout(record)
        except Exception:
            logger.exception("着手確認の決着処理失敗: %s (%s)", path, outcome)
            try:
                self.slack.notify(
                    ":x: 着手確認の処理に失敗しました。お手数ですがもう一度お願いします。",
                    record.get("channel_id"),
                    team_id=record.get("team_id"),
                    thread_ts=record.get("thread_ts"),
                )
            except Exception:
                logger.exception("着手確認の失敗通知の送信に失敗: %s", path)

    async def _execute_chain(self, task_file: str, task: dict, subtasks: list[dict]):
        """サブタスクを依存関係に基づき連鎖実行する。
        依存のないサブタスクは並行でキューに投入し、依存のあるものは前の完了を待つ。
        """
        channel = task.get("channel_id")
        team_id = task.get("team_id")   # 応答先ワークスペース（複数WS運用時のトークン選択用）
        results = {}       # step番号 → 実行結果
        futures = {}       # step番号 → asyncio.Future（完了通知用）
        subtask_map = {s["step"]: s for s in subtasks}

        # 各サブタスクの完了を通知する Future を準備
        for s in subtasks:
            futures[s["step"]] = asyncio.get_event_loop().create_future()

        try:
            pending_tasks = []
            for subtask in subtasks:
                t = asyncio.create_task(
                    self._execute_subtask_in_chain(
                        task, subtask, results, futures, channel,
                    )
                )
                pending_tasks.append(t)

            # 全サブタスクの完了を待つ（独立ブランチは失敗ブランチの影響を受けない）
            await asyncio.gather(*pending_tasks, return_exceptions=True)

            # 全サブタスク成功か判定
            failed_steps = [s["step"] for s in subtasks if s["step"] not in results]
            if not failed_steps:
                final_result = results[subtasks[-1]["step"]]
                self._update_status(task_file, "completed", result=final_result)
                self.slack.notify(f"タスク完了:\n```{final_result[:500]}```", channel, team_id=team_id)
            else:
                self._update_status(task_file, "failed")
                self._notify_failure(task, subtasks, results, failed_steps, channel, team_id)

        except Exception as e:
            self._update_status(task_file, "failed", result=str(e))
            self.slack.notify(f"タスク失敗: {e}", channel, team_id=team_id)

    def _notify_failure(self, task, subtasks, results, failed_steps, channel, team_id=None):
        """失敗時の詳細通知（設計書 §10.3）"""
        lines = ["⚠ タスク失敗", "", f"【元の指示】", task["command"], "", "【サブタスク結果】"]
        for s in subtasks:
            step = s["step"]
            if step in results:
                lines.append(f"  Step {step}: {s['command']} ({s['category']}) → ✅ 成功")
            elif step in failed_steps:
                lines.append(f"  Step {step}: {s['command']} ({s['category']}) → ❌ 失敗")
            else:
                lines.append(f"  Step {step}: {s['command']} ({s['category']}) → ⏭ スキップ")
        self.slack.notify("\n".join(lines), channel, team_id=team_id)

    async def _execute_subtask_in_chain(self, task: dict, subtask: dict,
                                         results: dict, futures: dict,
                                         channel: str):
        """単一サブタスクを実行する。依存がある場合は先に完了を待つ。"""
        step = subtask["step"]
        command = subtask["command"]
        category = subtask["category"]  # DeepSeek-R1 が分解時に判定済み
        depends_on = subtask.get("depends_on", [])

        # 依存するサブタスクの完了を待つ（複数依存対応）
        dep_results = []
        for dep in depends_on:
            if dep in futures:
                try:
                    await futures[dep]
                    dep_results.append(f"Step {dep}: {results[dep]}")
                except Exception:
                    # 依存先が失敗 → cascading skip
                    futures[step].set_exception(
                        RuntimeError(f"依存先 Step {dep} が失敗したためスキップ")
                    )
                    return

        # 依存ステップの結果を入力に組み込む
        if dep_results:
            context = "\n".join(dep_results)
            command = f"前のステップの結果:\n{context}\n\n上記を踏まえて: {command}"

        self.slack.notify(f"  サブタスク {step}: {category}", channel, team_id=task.get("team_id"))

        # キューに投入し、ワーカーに実行させる
        result_future = asyncio.get_event_loop().create_future()
        queue_item = {
            **task,
            "_command": command,
            "_category": category,
            "_step": step,
            "_result_future": result_future,
        }

        await self._enqueue(queue_item)

        # ワーカーの実行完了を待つ
        output = await result_future
        results[step] = output
        futures[step].set_result(output)

        self.slack.notify(f"  サブタスク {step}/{len(futures)} 完了", channel, team_id=task.get("team_id"))

    async def _enqueue(self, item: dict):
        """カテゴリに応じたキューにタスクを投入（満杯時は空きを待つ）"""
        category = item["_category"]
        if category == "light":
            await self.queue_light.put(item)
        else:
            await self.queue_heavy.put(item)

    # ── ワーカー: カテゴリ別にキューから取り出して実行 ──

    async def _worker_light(self):
        """light サブタスクを取り出し次第、上限なしで並行起動する（軽量ゆえ絞らない）。"""
        running = []
        while True:
            item = await self.queue_light.get()
            t = asyncio.create_task(self._execute_worker_task(item))
            running.append(t)
            # 完了済みタスク参照を捨てて running リストの無限肥大を防ぐ（保持は GC 抑止のため）
            running = [t for t in running if not t.done()]

    async def _worker_heavy(self):
        """heavy サブタスクを最大 max_heavy_instances 並行で処理（上限は §8.14 で動的変動）。"""
        running = []
        while True:
            item = await self.queue_heavy.get()
            # キューから取り出した後に枠を確保する。確保できるまでここで待つ＝同時起動数を上限に抑える
            await self.heavy_limiter.acquire()
            t = asyncio.create_task(self._execute_heavy_with_release(item))
            running.append(t)
            # 完了済みタスク参照を捨てて running リストの無限肥大を防ぐ
            running = [t for t in running if not t.done()]

    async def _execute_heavy_with_release(self, item):
        """heavy 実行後に並行枠を解放する"""
        try:
            await self._execute_worker_task(item)
        finally:
            await self.heavy_limiter.release()

    async def _execute_worker_task(self, item: dict):
        """ワーカーがキューから受け取ったサブタスクを実行し、結果を Future にセットする。

        分岐:
          - `_model` が 2 つ以上のリスト → cross-review（_execute_cross_review）
          - `_model` が単一文字列または 1 要素リスト → 明示指定実行（フォールバックなし）
          - `_model` 未指定 → `category_defaults[category]` 配列でフォールバック実行

        フォールバック動作（設計書 §2.2）:
          - ユーザーが `:モデル名` で明示指定した場合: そのモデルのみで実行（障害時もフォールバックしない）
          - 指定なしの場合: `routing.category_defaults[category]` 配列を先頭から試行。
            `fallback.max_fallback_attempts` で fallback の試行回数（先頭は含まない）を制限。
            例: `0` = fallback なし（先頭のみ）、`1` = 先頭 + 1 fallback。
            未指定なら配列全要素を試行（無制限）。[0] 障害 → [1] へ。全候補失敗で例外。
          - light 全候補失敗時のみ heavy に昇格して再投入。
        """
        command = item["_command"]
        category = item["_category"]
        step = item["_step"]
        result_future = item["_result_future"]
        channel = item.get("channel_id")
        team_id = item.get("team_id")

        # cross-review 分岐: 2 つ以上のモデル指定で並行投入
        user_specified = item.get("_model")
        if isinstance(user_specified, list) and len(user_specified) >= 2:
            await self._execute_cross_review(item, user_specified)
            return

        # list 1 要素は str に揃える
        if isinstance(user_specified, list):
            user_specified = user_specified[0] if user_specified else None

        # モデル候補リストを構築
        if user_specified:
            # ユーザー明示指定 → フォールバックしない（指定モデル尊重）
            candidates = [user_specified]
        else:
            defaults = self.config["routing"]["category_defaults"].get(category, [])
            max_fallback_attempts = self.config.get("fallback", {}).get("max_fallback_attempts")
            # max_fallback_attempts は fallback の試行回数（先頭は含まない）。
            # 未指定なら配列全要素。N 指定なら先頭 + fallback N 件（合計 N+1 件）
            candidates = defaults if max_fallback_attempts is None else defaults[:max_fallback_attempts + 1]

        last_error = None
        for idx, model_name in enumerate(candidates):
            is_fallback = idx > 0
            try:
                model_conf = self.config["models"].get(model_name, {})
                method = _select_method(model_conf, use_case="default")

                if method == "subprocess":
                    # subprocess 単発実行（ollama / 単発 API）
                    output = await asyncio.to_thread(
                        self.process_mgr.run_model_subprocess, model_name, model_conf, command
                    )
                else:  # pty — 対話型 worker CLI 全般（Claude Code / Gemini CLI / 将来の Codex 等）
                    model_flag = model_conf.get("model_flag", "")
                    cli_command = model_conf.get("command", "claude")
                    instance_id = f"{item['task_id']}-step{step}"
                    workspace = self._workspace_for(item["task_id"])
                    output = await self._run_worker_pty(instance_id, cli_command, command, channel, model_flag, workspace, team_id=team_id, task_id=item["task_id"])

                if is_fallback:
                    self.slack.notify(
                        f"  {candidates[0]} 障害 → {model_name} で実行（fallback）", channel, team_id=team_id
                    )
                result_future.set_result(output)
                return

            except Exception as e:
                last_error = e
                self.slack.notify(f"  {model_name} 障害: {e}", channel, team_id=team_id)
                continue

        # 全候補失敗
        if category == "light":
            # light 全失敗 → heavy に昇格して再投入
            item["_category"] = "heavy"
            self.slack.notify(f"  light 全失敗。heavy に昇格: {last_error}", channel, team_id=team_id)
            await self._enqueue(item)
        else:
            # heavy 全失敗 → Future に例外をセット（_execute_chain で捕捉、User へ failed 通知）
            result_future.set_exception(last_error)

    async def _execute_cross_review(self, item: dict, models: list[str]):
        """複数モデルへ並行投入し、結果を ya-ta（DeepSeek-R1 32B）で知的統合する（設計書 §2.2）。

        - 各モデルは明示指定扱いのためフォールバックしない
        - 各モデルは heavy 枠（self.heavy_limiter）を個別取得（pty 方式のみ）
        - 部分成功許容: 1 つでも成功すれば成功分を統合して返す
        - 全モデル失敗で failed
        """
        command = item["_command"]
        step = item["_step"]
        result_future = item["_result_future"]
        channel = item.get("channel_id")
        team_id = item.get("team_id")

        async def _run_one(model_name: str) -> tuple[str, str | Exception]:
            """1 モデルで cross-review を実行し、(モデル名, 出力 or 例外) を返す。

            実行方式（subprocess / pty）はモデル設定から選ぶ。例外は送出せず戻り値に
            畳み込むことで、複数モデル並行時に 1 つの失敗が他を巻き込まないようにする。
            """
            try:
                model_conf = self.config["models"].get(model_name, {})
                method = _select_method(model_conf, use_case="cross_review")
                if method == "subprocess":
                    output = await asyncio.to_thread(
                        self.process_mgr.run_model_subprocess, model_name, model_conf, command
                    )
                else:  # pty — heavy 枠を個別取得
                    model_flag = model_conf.get("model_flag", "")
                    cli_command = model_conf.get("command", "claude")
                    instance_id = f"{item['task_id']}-step{step}-{model_name}"
                    workspace = self._workspace_for(item["task_id"])
                    async with self.heavy_limiter:
                        output = await self._run_worker_pty(instance_id, cli_command, command, channel, model_flag, workspace, team_id=team_id, task_id=item["task_id"])
                return (model_name, output)
            except Exception as e:
                return (model_name, e)

        results = await asyncio.gather(*[_run_one(m) for m in models])
        successes = [(m, r) for m, r in results if not isinstance(r, Exception)]
        failures = [(m, r) for m, r in results if isinstance(r, Exception)]

        for m, e in failures:
            self.slack.notify(f"  cross-review: {m} 失敗（結果から除外）: {e}", channel, team_id=team_id)

        if not successes:
            result_future.set_exception(
                RuntimeError(f"cross-review: 全モデル失敗 — {[m for m, _ in failures]}")
            )
            return

        # ya-ta（DeepSeek-R1 32B）で知的統合
        integrated = await asyncio.to_thread(self._integrate_cross_review, command, successes)
        result_future.set_result(integrated)

    def _integrate_cross_review(self, command: str, results: list[tuple[str, str]]) -> str:
        """各モデルの結果を ya-ta（DeepSeek-R1 32B）で知的統合する。
        Mac mini 上の ollama 経由で DeepSeek-R1 32B（ya-ta と同モデル）に投入。
        """
        sections = "\n\n".join(f"### {m}\n{r}" for m, r in results)
        prompt = (
            "以下は同じタスクに対する複数 AI モデルの回答です。"
            "それぞれの回答の長所を踏まえ、合意できる点と相違点を整理し、"
            "最終的な統合回答をまとめてください。\n\n"
            f"## 元タスク\n{command}\n\n"
            f"## 各モデルの回答\n{sections}\n\n"
            "## 統合回答（あなたが作成）"
        )
        result = subprocess.run(
            ["ollama", "run", "deepseek-r1:32b"],  # ya-ta と同モデル
            input=prompt,
            capture_output=True, text=True, timeout=180,
        )
        return result.stdout

    async def _run_worker_pty(self, instance_id: str, cli_command: str, command: str, channel: str, model_flag: str = "", workspace: str | None = None, team_id: str | None = None, task_id: str = "") -> str:
        """対話型 worker CLI を PTY 経由で実行する汎用ラッパー呼び出し。

        Claude Code / Gemini CLI / Codex 等を共通の WorkerPtyWrapper で扱う。
        cli_command で起動コマンド名（claude / gemini 等）を指定する。
        workspace を渡すとタスク専用作業ディレクトリで起動する（qu-e の path→task_id 帰属の前提）。

        駆動ループ（§8.5 / 08-approval-pipeline）:
          worker 起動 → タスク投入 → stdout を逐次読取 → y/n プロンプト検出時は
          ApprovalPipeline.process() で承認/拒否（Tier1 自動 / Tier2 qu-e / Tier3 人間）→
          worker 完了（EOF）まで継続 → 蓄積した stdout を最終出力として返す。

        pexpect は同期ブロックのため _drive() を to_thread で別スレッド実行する。承認は
        async（Tier3 が Slack 応答を await）なので、run_coroutine_threadsafe で event loop に委譲し
        結果を待つ（pipeline 内で wrapper.approve()/deny() が呼ばれる）。
        """
        loop = asyncio.get_running_loop()
        wrapper = WorkerPtyWrapper(instance_id, command=cli_command, model_flag=model_flag, cwd=workspace)

        def _drive() -> str:
            """PTY を起動してタスクを流し、承認プロンプトを捌きながら出力を集めて返す。

            pexpect でブロッキングに読むため別スレッド（to_thread）で回す前提の同期関数。
            y/n プロンプトを検出したら approve/deny を裏で判定し、結果を wrapper に書き戻す。
            """
            import pexpect
            from interceptor import detect_prompt
            wrapper.start()
            wrapper.send_task(command)
            chunks: list[str] = []
            context_buf: list[str] = []
            patterns = [r"\[y/n\]", r"\(yes/no\)", r"Allow\?", pexpect.EOF, pexpect.TIMEOUT]
            while True:
                idx = wrapper.child.expect(patterns)
                before = wrapper.child.before or ""
                chunks.append(before)
                context_buf.extend(before.splitlines())
                if idx in (0, 1, 2):           # y/n プロンプト検出
                    matched = wrapper.child.after or ""
                    prompt = detect_prompt(matched, context_buf)
                    if prompt is None:
                        continue
                    asyncio.run_coroutine_threadsafe(
                        self.approval_pipeline.process(
                            prompt, wrapper, instance_id,
                            team_id=team_id, channel=channel, task_id=task_id,
                        ), loop
                    ).result()
                elif idx == 3:                 # EOF — worker 完了
                    break
                else:                          # TIMEOUT
                    raise RuntimeError(f"worker PTY timeout: {instance_id}")
            return "".join(chunks)

        try:
            return await asyncio.to_thread(_drive)
        finally:
            wrapper.close()

    # ── /exam_gw ドライラン結果フォーマット ──

    def _format_exam_result(self, subtasks: list[dict]) -> str:
        """ドライラン結果を Slack 通知用テキストに整形"""
        lines = ["ya-ta 検証結果（実行なし）\n"]
        for s in subtasks:
            model = s.get("model")
            if model:
                model_display = f"{model}（ユーザー指定。フォールバックなし）"
                primary = model
            else:
                # category_defaults 配列から解決（[0] がデフォルト）
                defaults = self.config["routing"]["category_defaults"].get(s["category"], [])
                max_fallback_attempts = self.config.get("fallback", {}).get("max_fallback_attempts")
                candidates = defaults if max_fallback_attempts is None else defaults[:max_fallback_attempts + 1]
                if candidates:
                    primary = candidates[0]
                    if len(candidates) > 1:
                        fallback_display = " → ".join(candidates[1:])
                        model_display = f"null → {primary}（デフォルト、fallback: {fallback_display}）"
                    else:
                        model_display = f"null → {primary}（デフォルト、fallback なし）"
                else:
                    primary = None
                    model_display = "null（候補なし）"
            model_conf = self.config["models"].get(primary, {}) if primary else {}
            methods = model_conf.get("methods") or ([model_conf.get("method")] if model_conf.get("method") else [])
            selected_method = _select_method(model_conf) if model_conf else "unknown"
            lines.append(
                f"Step {s['step']}: {s['command']}\n"
                f"  category: {s['category']}\n"
                f"  model: {model_display}\n"
                f"  methods: {methods} → selected: {selected_method}\n"
                f"  depends_on: {s.get('depends_on', [])}\n"
                f"  confidence: {s.get('confidence', 'N/A')}\n"
            )
        return "\n".join(lines)

    # ── アーカイブローテート ──

    async def _daily_cleanup(self):
        """タスクアーカイブ（done/）の古いディレクトリを削除。判定ログは学習データのため永続保持。"""
        retention = self.config["cleanup"]["retention_days"]
        threshold = datetime.date.today() - datetime.timedelta(days=retention)

        done_dir = f"{self.task_dir}/done"
        if os.path.exists(done_dir):
            for name in os.listdir(done_dir):
                try:
                    if datetime.date.fromisoformat(name) < threshold:
                        shutil.rmtree(os.path.join(done_dir, name))
                except ValueError:
                    pass

    # ── ユーティリティ ──

    def _update_status(self, path: str, status: str, result: str = None):
        """タスクファイルの status を更新する。completed/failed はアーカイブ。
        in_progress / completed / failed 遷移時に qu-e へタスクコンテキストを push する（§8.13）。
        """
        with open(path) as f:
            task = json.load(f)
        task["status"] = status
        task["updated_at"] = datetime.datetime.now().isoformat()
        if result:
            task["result"] = result
        with open(path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        # §8.13 タスクコンテキスト共有(qu-e へ SSH push)
        if status in ("in_progress", "completed", "failed"):
            self._push_task_context(task)

        # completed/failed のファイルを done/{日付}/ に移動（ディレクトリ走査の肥大化防止）
        if status in ("completed", "failed"):
            today = datetime.date.today().isoformat()
            done_dir = f"{self.task_dir}/done/{today}"
            os.makedirs(done_dir, exist_ok=True)
            shutil.move(path, os.path.join(done_dir, os.path.basename(path)))

    def _workspace_for(self, task_id: str) -> str:
        """タスク専用の作業ディレクトリ（MBP 上）。

        各タスクは `{workspace_base}/{task_id}` で実行され、qu-e はこの接頭辞で
        file_audit の変更パスを task_id に帰属させる（§8.13）。
        """
        base = self.config["task_context"].get("workspace_base", "/opt/taka-ma/work")
        return f"{base}/{task_id}"

    def _push_task_context(self, task: dict):
        """タスクコンテキストを qu-e に SSH push する（§8.13）。

        in_progress / completed / failed の各遷移で同じ payload 形式で push し、
        受信側（qu-e）が status を見て保持・整理する。
        SSH push 失敗時はログ記録のみで継続（task 処理は止めない）。
        """
        remote_dir = self.config["task_context"]["remote_dir"]
        task_id = task["task_id"]
        payload = json.dumps({
            "task_id": task_id,
            "command": task["command"],
            "channel_id": task.get("channel_id", ""),
            "team_id": task.get("team_id", ""),          # §8.3: file_audit アラートの応答先WS特定用
            "thread_ts": task.get("thread_ts"),          # §8.12: 実行中タスクへの file_audit アラートを同一スレッドへ Thread 返信させる
            "status": task["status"],
            "workspace": self._workspace_for(task_id),   # §8.13: パス→task_id 帰属用
        }, ensure_ascii=False)
        try:
            self.process_mgr.run_ssh_command(
                f"mkdir -p {remote_dir} && cat > {remote_dir}/{task_id}.json",
                stdin_text=payload,
            )
        except Exception:
            logger.exception("task_context push 失敗: task_id=%s", task_id)


# 起動エントリは orchestrator/__main__.py（`python -m orchestrator`）に置く。
# パッケージ実行（-m）では __init__.py の __name__ は "orchestrator" になり、
# ここに __main__ ブロックを書いても起動しないため、__main__.py へ分離している。
