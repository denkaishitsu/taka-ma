"""sa-ru オーケストレーション本体 — タスクキュー監視、分解、連鎖実行。

構築手順書: docs/procedures/05-orchestrator.md Step 8（パイプライン統合）
関連: 設計書 §1.3 / §2.2 / §8.4 / §10
"""

import os
import sys
sys.path.insert(0, "/opt/taka-ma/ya-ta")
# approval-pipeline はハイフン dir でパッケージ import 不可のため sys.path 経由で bare import する
sys.path.insert(0, "/opt/taka-ma/sa-ru/approval-pipeline")
# 開発機（未配備）向けフォールバック。配備先の絶対パスは上で先頭に入れてあるため、
# 本番では常にそちらが先に当たり解決先は変わらない。末尾に足すのはリポジトリ上の実体
# src/approval-pipeline で、これが無いと開発機の pytest が interceptor を解決できず落ちる。
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "approval-pipeline"))

import asyncio
import datetime
import glob
import json
import logging
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from ai_gateway.decomposer import TaskDecomposer
from ai_gateway.classifier import TaskClassifier
from ai_gateway.llm import GenerationProgress, run_ollama
from ai_gateway.plan_corrector import PlanCorrector
from ai_gateway.risk_classifier import RiskClassifier
from orchestrator.process_manager import RemoteProcessManager
from orchestrator.slack_notifier import SlackNotifier
from orchestrator.pty_wrapper import WorkerPtyWrapper
from orchestrator.headless_runner import WorkerHeadlessRunner, build_hook_settings
from orchestrator.preflight import AuthPreflight, PreflightFailure
from orchestrator import contract as contract_rules
from orchestrator import intent_store
from orchestrator.concurrency import DynamicConcurrencyLimiter
from orchestrator.conversation import ConversationManager
from orchestrator.grounding import GroundingReport, GroundingVerifier
from orchestrator.plan import PlanService, effective_deps
from orchestrator.resource_monitor import ResourceMonitor
from orchestrator.file_queue import FileQueue, atomic_write_json

# Claude Code の対話モードは worker 完了時に EOF しない（tmux セッション内に常駐し続ける）ため、
# 「EOF＝完了」だけではタスク完了を検知できず、実際は完了しているのに PTY タイムアウトで
# failed 扱いになる欠陥を実機検証で確認・是正。当初は "for shortcuts"（入力待ちフッター）や
# "tokens" を含む生成中ステータスの文言をアンカーに完了判定を試みたが、生成中インジケータの
# 絵文字・文言は実機観測のたびに異なり（"↓ N tokens" のときも "✢N tokens" のときもある）、
# 文言ベースの検知は本質的に脆いことを2回の実機検証で確認した。Claude Code の非対話モード
# （`claude -p --output-format stream-json`）が permission_denials 等の構造化 JSON で完了を
# 返すことを一次ソース確認済みだが、そちらへの本格移行は承認パイプライン全体の再設計を伴う
# 別タスク（#80）とし、当面は文言に依存しない「タスク送信後、一定の起動猶予を過ぎてから、
# 一定時間まったく新規出力が来ない」という無音時間ベースの完了判定を暫定策として用いる。
# 既知の限界（隠さない）: 実行中のツール呼び出し（例: 遅い Bash コマンド）が
# _IDLE_QUIET_SEC を超えて無出力になった場合、誤って完了扱いする可能性が残る
# （#80 の構造化プロトコルへの移行で解消予定）。
_TASK_STARTUP_GRACE_SEC = 20  # タスク送信後、この秒数が経つまでは無音でも完了とみなさない
_IDLE_QUIET_SEC = 20          # 起動猶予後、この秒数以上新規出力が無ければ完了とみなす


# ── 承認保留（§3.3 (4) / §8.10）──

# 承認保留に落ちたタスクの status。completed でも failed でもない第 3 の終端で、人間の決着を
# 待つ状態を表す。dispatcher の claim("init") にも起動時の予約回収（accepted/in_progress）にも
# 拾われず、_approval_hold_loop だけが決着を見て init へ戻す。
STATUS_PENDING_APPROVAL = "pending_approval"

# 承認レコード（{approval_dir}/{request_id}.json）の status 契約値。承認パイプライン
# （tier3_handler.py）・u-zu（approval_store.py）と**同じ文字列**を使う規約で、3 者は別ツリー
# 配備のため import 共有できない（`grep STATUS_APPROVAL_` で三者一致を確認する）。
STATUS_APPROVAL_PENDING = "pending"
STATUS_APPROVAL_APPROVED = "approved"
STATUS_APPROVAL_REJECTED = "rejected"

# 承認レコードのファイル名に用いる request_id の受理形式（uuid 相当：英数とハイフンのみ）。
# タスクファイル由来の値をそのままパス連結するため、パス区切り・親参照を弾いて承認
# ディレクトリ外への参照を防ぐ（u-zu の `/taka-ma-approve` と同じ規律・§8.10）。
_APPROVAL_ID_RE = re.compile(r"[A-Za-z0-9-]+")

# 中止命令（§8.10d）で停止したタスクの result に刻む理由。終端 status は failed を再利用する
# （§8.10 の却下中止と同じ規律。専用 status を新設すると sa-ru/u-zu/qu-e 3 者への契約追加になる）。
_CANCELLED_BY_CONTROL = "中止命令により停止"

# 中止報告の 1 行に載せる指示文の最大長（一覧性優先。全文はタスクファイルが正本）
_CANCEL_REPORT_MAX_CHARS = 60


def _cancel_display(text: str) -> str:
    """中止報告（§8.10d）の一覧行向けに指示文/要約を 1 行へ丸める。全文はタスクファイルが正本。"""
    line = " ".join(text.split())
    if len(line) > _CANCEL_REPORT_MAX_CHARS:
        line = line[:_CANCEL_REPORT_MAX_CHARS] + "…"
    return line or "（指示文なし）"


class ApprovalHold(Exception):
    """サブタスクが承認保留で未了のまま畳まれたことを表す内部シグナル（設計書 §3.3 (4)）。

    保留は worker の障害ではないため、昇格ラダー（`_execute_worker_task` の候補ループ）へは
    決して流さない。次段モデルへ昇格させると「承認を迂回した実行」になりうるためで、
    `_run_candidate` は保留を例外ではなく戻り値 `("hold", request_id)` で返し、この例外は
    昇格判断を終えた後の「サブタスク完了通知 future」の解決にのみ使う（後続の cascading skip を
    従来経路のまま働かせ、タスク単位で畳むため）。
    """

    def __init__(self, request_id: str):
        super().__init__(f"承認保留により中断（承認 ID: {request_id}）")
        self.request_id = request_id


def _select_method(model_conf: dict, use_case: str = "default") -> str:
    """モデルの methods 配列と用途から呼び出し経路を選択する。

    use_case:
      - "default":      通常の振り分け。headless > pty > subprocess の優先で選ぶ
      - "cross_review": 並行投入用。subprocess 優先（対話不要）
      - "multimodal":   マルチモーダル単発。subprocess 優先

    headless は Claude Code 専用の非対話経路（claude -p + stream-json + PreToolUse フック）。
    interactive(pty) は agy 対話等の汎用対話 CLI 用。旧 method (単数) にも後方互換で対応する。
    """
    methods = model_conf.get("methods")
    if methods is None:
        legacy = model_conf.get("method")
        methods = [legacy] if legacy else []

    # cross_review / multimodal は対話不要のため、subprocess を持つなら最優先で単発実行する。
    if use_case in ("cross_review", "multimodal") and "subprocess" in methods:
        return "subprocess"
    # 通常経路は headless（構造化・確定的）を最優先、次に interactive(pty)、最後に subprocess。
    if "headless" in methods:
        return "headless"
    if "pty" in methods:
        return "pty"
    if "subprocess" in methods:
        return "subprocess"
    return "pty"


# ── execution × depth × confidence → モデル写像 / 昇格ラダー ──

# worker 出力の自己申告昇格マーカー。worker が「このタスクは自分の手に余る」と判断した際に
# 出力へ埋め込む（設計書 §2.2「昇格の引き金 (a)」）。行頭一致で検出する。
ESCALATE_MARKER = "ESCALATE:"


def _escalate_reason(output: str) -> str | None:
    """worker 出力に ESCALATE 自己申告があれば理由文字列を返す（無ければ None）。

    いずれかの行が `ESCALATE:` で始まる場合に昇格の引き金とみなす（設計書 §2.2 (a)）。
    理由はマーカー以降のテキスト。output が文字列でない場合は None。
    """
    if not isinstance(output, str):
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(ESCALATE_MARKER):
            return stripped[len(ESCALATE_MARKER):].strip() or "(理由未記載)"
    return None


def _axis_label(subtask: dict) -> str:
    """サブタスクの execution × depth を人間向け 1 行ラベルにする（通知・ドライラン用）。

    depth 省略（None）は execution だけを示す。旧 category 表示の置換。
    """
    execution = subtask.get("execution", "agent")
    depth = subtask.get("depth")
    return f"{execution}/{depth}" if depth else execution


def _resolve_model(routing: dict, execution: str, depth, confidence) -> str | None:
    """写像テーブル（ya-ta.yaml routing.matrix）で primary モデル名を解決する（設計書 §2.2）。

    - confidence >= routing.confidence_threshold なら high 側、未満なら low 側を引く
    - inline は depth 不問（matrix.inline.{high,low}）
    - agent は depth（shallow/deep）で分岐。省略・未知の depth は unspecified を用いる
    未解決（matrix 不備）のときは None を返す。呼び出し側が候補なしとして failed に倒す。
    """
    matrix = routing.get("matrix", {})
    threshold = routing.get("confidence_threshold", 0.8)
    # confidence 欠損は high 扱い（分類側で 1.0 正規化済みだが多重防御）
    band = "high" if (confidence is None or confidence >= threshold) else "low"

    if execution == "inline":
        return matrix.get("inline", {}).get(band)
    # agent: depth で下位表を選ぶ。shallow/deep 以外（None 含む）は unspecified
    depth_key = depth if depth in ("shallow", "deep") else "unspecified"
    return matrix.get("agent", {}).get(depth_key, {}).get(band)


def _escalation_chain(routing: dict, primary: str | None, max_steps=None) -> list[str]:
    """primary を起点に、昇格ラダー（routing.escalation.ladder）で試行順の候補列を作る。

    - primary がラダー上にあれば primary 以降を、ラダー外（例: gemma）なら primary + ラダー全体を返す
    - primary が None（写像未解決）ならラダー全体を返す
    - max_steps（fallback.max_fallback_attempts）が与えられれば primary を含めて max_steps+1 件に制限
    先頭が実行対象、以降が段階昇格先（設計書 §2.2「昇格ラダー」）。重複は順序を保って除去する。
    """
    ladder = routing.get("escalation", {}).get("ladder", [])
    if primary is None:
        chain = list(ladder)
    elif primary in ladder:
        chain = ladder[ladder.index(primary):]
    else:
        # ラダー外の primary（gemma 等）: まず primary、失敗したらラダー先頭から
        chain = [primary] + list(ladder)
    # 順序保持の重複除去（primary がラダー先頭と一致する等の二重を潰す）
    seen = set()
    deduped = []
    for m in chain:
        if m not in seen:
            seen.add(m)
            deduped.append(m)
    if max_steps is not None:
        deduped = deduped[:max_steps + 1]
    return deduped


logger = logging.getLogger("sa-ru.orchestrator")

# タスクキューの dir / ポーリング間隔は sa-ru.yaml の task_queue ブロックを唯一の源にする
# （旧 TASK_DIR / POLL_INTERVAL 定数は yaml と二重定義だったため撤去）。
# 承認ファイルのディレクトリは書き手（Tier3Handler・u-zu の approval_store）が環境変数
# `TAKA_MA_APPROVAL_DIR` で一元管理する。sa-ru は #132 で「保留レコードを読む」読み手に
# なったため、同じ env を同じ優先順で解決し（未設定なら sa-ru.yaml の approval.dir を既定に
# 使う）、書き手と読み手が別のディレクトリを見る事故を構造的に防ぐ。ここで yaml だけを見ると、
# env で配備先を変えた瞬間に保留が永久に検知されなくなる（タスクが pending_approval のまま滞留する）。


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
                recommended, notify.get("memory_usage"), notify.get("level"))
            shutil.move(event.src_path, f"{self.done_dir}/{Path(event.src_path).name}")
        except Exception:
            logger.exception("リソース最適化通知処理失敗: %s", event.src_path)


class Orchestrator:
    """sa-ru の中核。タスク受付から分解・連鎖実行・承認・通知までの常駐ループ群を束ねる。

    run で dispatcher（タスク監視→分解→キュー投入）、light/heavy ワーカー、会話・着手確認・
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
        # worker 起動前の認証プリフライト（SSH / git remote / Anthropic の事前検査と原因切り分け）。
        # SSH 先は process_mgr と同じ mbp_host（供給元を 1 つに保つ）。運用値は sa-ru.yaml の
        # preflight ブロックが唯一の源（コード側に既定値なし。欠落は KeyError で即落とす）
        self.preflight = AuthPreflight(ssh_host=mbp_host, conf=config["preflight"])
        self.slack = SlackNotifier()

        # LLM 処理待ちのハートビート通知間隔（§10.8）。sa-ru.yaml の heartbeat.interval_sec を
        # 唯一の源とし、コードに既定値を置かない（欠落は ssh ブロックと同様に起動時に即落とす）
        self._heartbeat_interval = config["heartbeat"]["interval_sec"]

        # 承認パイプライン（worker の y/n 介入。ya-ta=in-process / qu-e=SSH、§8.8〜§8.9）。
        # 実体は approval-pipeline パッケージ（08 で /opt/taka-ma/sa-ru/approval-pipeline へ配備、
        # 設計上「sa-ru の一部」）に存在する。設計 §08「パイプラインは y/n 検出時に起動」に従い、
        # 初回 y/n 検出時に遅延生成する（approval_pipeline プロパティ）。__init__ で eager import
        # すると「08 は 05 sa-ru 稼働を前提／05 は 08 配備を前提」の循環になり sa-ru が単体起動できない。
        self._mbp_host = mbp_host
        self._approval_pipeline = None

        # 承認保留の決着監視（§8.10）。承認レコードの置き場・監視間隔・再投入回数の上限は
        # sa-ru.yaml の approval ブロックを唯一の源にする（Tier3 ハンドラ・u-zu と同じ dir を
        # 指すことでファイルベース cross-process が成立する。コード側に既定値を置かない）。
        approval_conf = config["approval"]
        self.approval_dir = os.environ.get("TAKA_MA_APPROVAL_DIR", approval_conf["dir"])
        self.approval_poll = approval_conf["poll_interval_sec"]
        self.max_reinject = approval_conf["max_reinject"]

        # カテゴリ別キュー（FIFO、上限付き）
        # execution 軸でレーン分離。inline=無制限、agent=heavy_limiter 制限
        self.queue_inline = asyncio.Queue(maxsize=100)
        self.queue_agent = asyncio.Queue(maxsize=10)

        # heavy の同時実行上限（動的リミッタで制御）。
        # 起動時は ya-ta.yaml の max_heavy_instances をブートストラップ値とし、
        # 実行時は qu-e のリソース最適化通知（§8.14）で動的に増減する。
        self.heavy_limiter = DynamicConcurrencyLimiter(
            config["concurrency"]["max_heavy_instances"]
        )

        # 会話フロントエンド。u-zu からの発話を脳 LLM で会話・要約し、人間の着手確認を
        # 得てから確定タスク（status=init）を task_queue.dir に生成する。生成後は既存 dispatcher が拾う。
        # タスクキューの dir / ポーリング間隔は config を唯一の源にする（u-zu の writer
        # task_queue.py と同じキー。コードに既定値を置かず、欠落は起動時に即落とす）。
        self.task_dir = config["task_queue"]["dir"]
        self.task_poll = config["task_queue"]["poll_interval_sec"]
        # 計画確認ゲート（§8.10b）が使う計画サービス。分解と自然言語訂正は ya-ta、モデル写像は
        # 実行側と同じ _plan_execution を注入する（プレビュー専用の写像を持たない・§10.2.1）
        self.plan_service = PlanService(
            self.decomposer, PlanCorrector(config),
            self._plan_execution, config.get("models", {}).keys())
        self.conversation = ConversationManager(config, self.slack, task_dir=self.task_dir,
                                                 classifier=self.classifier,
                                                 plan_service=self.plan_service,
                                                 process_mgr=self.process_mgr,
                                                 canceller=self.request_cancel)

        # 会話⇄実行の受け渡し契約（§8.10f）。`contract:` ブロック未構成なら従来動作（段階導入）。
        # intents_dir = 依頼の寿命（goal_status）台帳／ task_deny_dir = 禁止型拘束のタスク別
        # deny 規則（decide デーモンが読む。両者とも Mac mini ローカル）
        contract_conf = config.get("contract") or {}
        self.intents_dir = contract_conf.get("intents_dir")
        self.task_deny_dir = contract_conf.get("task_deny_dir")
        # task_id → worker が実行した Bash コマンド列（headless の stream-json から採取。
        # 逐語命令の遵守照合 §8.10f に使い、タスク終端で必ず破棄する）
        self._executed_commands: dict[str, list[str]] = {}

        # 停止命令の実行台帳（§8.10d）。task_id → {"chain": 連鎖実行の asyncio.Task,
        # "workers": worker の asyncio.Task 集合}。dispatcher が連鎖起動時に登録し、完了時に
        # done_callback で自動削除する。中止はこの台帳を引いて cancel する。
        self._running_tasks: dict[str, dict] = {}
        # 中止済み task_id の集合。キューに滞留中（worker 未取得）のサブタスクを worker 取得時に
        # スキップさせ、cancel の隙間からの遅延実行を防ぐ（§8.10d）。プロセス再起動でクリアされる
        # 揮発情報で、件数はタスク数オーダーのため保持し続けても肥大しない。
        self._cancelled_tasks: set[str] = set()
        # request_cancel（会話スレッドからの同期呼び出し）が cancel コルーチンを委譲する先の
        # イベントループ。run() の起動時に捕捉する。
        self._loop: asyncio.AbstractEventLoop | None = None
        # 会話/着手確認の dir・ポーリング間隔は config を唯一の源にする（コード既定値なし・二重定義を避ける）
        self.conversation_dir = config["conversation"]["dir"]
        self.conversation_poll = config["conversation"]["poll_interval_sec"]
        self.exec_confirm_dir = config["exec_confirm"]["dir"]
        self.exec_confirm_poll = config["exec_confirm"]["poll_interval_sec"]

        # 制御コマンド受信（§8.10c）。u-zu が controls/ に書く制御命令（手動 ollama 停止等）を
        # 監視し対応操作へ委譲する。dir・間隔は config を唯一の源にする（他キューと同じ流儀・二重定義を避ける）。
        self.control_dir = config["control"]["dir"]
        self.control_poll = config["control"]["poll_interval_sec"]

        # 各待受の取り回し（列挙・パース・壊れファイル隔離・done/ 退避）を共有 FileQueue に集約する。
        # ループ固有の判断（ready とする status・処理後の扱い）は各ループ側に残す。
        # 待受方式は現状の poll を踏襲。
        self.task_q = FileQueue(self.task_dir, poll_interval=self.task_poll)
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
        # 停止命令（§8.10d）の cancel 委譲先ループを捕捉する。request_cancel は会話スレッド
        # （to_thread）から呼ばれるため、asyncio タスクの cancel はこのループへ委譲する必要がある。
        self._loop = asyncio.get_running_loop()
        # 起動時の予約回収（reserve-then-crash 回復・§8.3）。前プロセスが accepted/in_progress の
        # まま落ちたタスクは claim('init') に拾われず恒久滞留するため、init へ戻して再処理させる。
        # 真の起動点（run）で 1 回だけ実施する（_dispatcher に置くと _supervise 再起動時に実行中の
        # in_progress タスクまで init へ戻し二重実行を招く）。
        # pending_approval は回収対象に**含めない**。人間の決着待ちであって落ちた予約ではなく、
        # init へ戻すと承認前に未了分が走り出す（承認の迂回）。決着は _approval_hold_loop が見る。
        reclaimed = self.task_q.reclaim({"accepted", "in_progress"}, "init")
        if reclaimed:
            logger.warning(
                "起動時の予約回収: accepted/in_progress の %d 件を init へ戻して再処理する", reclaimed)
        # 会話キューにも同じ回収を行う（§8.3 と同じ reserve-then-crash 対策）。processing の
        # まま前プロセスが落ちた発話は claim("init") に拾われず無言で滞留する（2026-08-24
        # E2E で実測: 処理中の再起動で発話が processing のまま取り残された）。会話処理も
        # at-least-once（クラッシュ位置によっては返信が重複しうるが、無言ドロップより回復を優先）
        reclaimed_msgs = self.conversation_q.reclaim({"processing"}, "init")
        if reclaimed_msgs:
            logger.warning(
                "起動時の会話回収: processing の %d 件を init へ戻して再処理する", reclaimed_msgs)

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
            self._supervise(self._worker_inline, "worker_inline"),
            self._supervise(self._worker_agent, "worker_agent"),
            self._supervise(self._conversation_loop, "conversation_loop"),    # 会話受信
            self._supervise(self._exec_confirmation_loop, "exec_confirmation_loop"),  # 着手確認
            self._supervise(self._control_loop, "control_loop"),             # 制御命令
            self._supervise(self._approval_hold_loop, "approval_hold_loop"), # 承認保留の決着（§8.10）
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

        run は asyncio.gather でループ群を束ねるため、1 つが例外を送出すると gather 全体が停止し
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
                await asyncio.sleep(self.task_poll)
                continue

            task_file, task = picked
            # 1 タスクの分解/受付失敗で dispatcher を殺さない。例外を吸収して当該タスクを failed に
            # 落とし（in_progress のまま放置すると claim('init') で再取得されず恒久ロストになる）、
            # ユーザーへ通知してループは継続する。
            try:
                await self._update_status(task_file, "in_progress")

                # 会話由来のタスクは計画確認ゲート（§8.10b）で分解済み＝承認された凍結プランを
                # そのまま実行する。ここで再分解すると人間が承認した計画と実際に走る計画がズレ、
                # 訂正した depth / model の上書きも失われる（設計書 §10.2「凍結プランの実行」）。
                frozen_plan = task.get("_plan")
                if frozen_plan:
                    subtasks = frozen_plan
                elif task.get("directive"):
                    # 逐語命令は分解しない（§10.2 / §8.10f。原文を command とする 1 件の
                    # プラン。ya-ta の言い換えを通すと変換のたびに劣化する）。会話由来は
                    # 通常 _plan で来るため、これは会話を経ない directive タスクの保険
                    subtasks = [{"step": 1, "command": task["directive"],
                                 "execution": "agent", "depth": None,
                                 "confidence": 1.0, "depends_on": []}]
                else:
                    # ya-ta モデルでタスクを分解（設計書 §8.4, §10.2）。分解 LLM の無応答区間は
                    # ハートビートで進捗を同スレッドへ返す（§10.8）
                    subtasks = await self._run_with_heartbeat(
                        "タスク分解", self.decomposer.decompose, task["command"],
                        channel=task.get("channel_id"),
                        team_id=task.get("team_id"),
                        thread_ts=task.get("thread_ts"),
                    )

                # /exam_gw ドライラン: 判定結果のみ返却し、実行しない（設計書 §2.2）
                if task.get("dry_run"):
                    await self._notify(
                        self._format_exam_result(subtasks),
                        task.get("channel_id"),
                        team_id=task.get("team_id"),
                        thread_ts=task.get("thread_ts"))
                    await self._update_status(task_file, "completed")
                    continue

                # 分解中に中止命令（§8.10d）が届いたタスクは受付通知も連鎖起動もしない
                # （タスクファイルは中止側が failed へ終端済み。通知すると「中止しました」の後に
                # 「タスク受付」が届く転倒になり、起動すると _update_status が移動済みパスを
                # 開いて落ちるだけの死に連鎖になる）。
                if task["task_id"] in self._cancelled_tasks:
                    continue

                accepted_msg = (f"タスク受付: 承認済みの計画 {len(subtasks)} 件で実行します"
                                if frozen_plan else
                                f"タスク受付: {len(subtasks)}件のサブタスクに分解")
                await self._notify(
                    accepted_msg,
                    task.get("channel_id"),
                    team_id=task.get("team_id"),
                    thread_ts=task.get("thread_ts"))

                # 禁止型拘束をタスク別 deny 規則として登録する（§8.10f。decide デーモンが
                # ツール実行前に機械ブロックする。終端時に _update_status が掃除する）
                self._write_task_deny(task)

                # 連鎖実行を非同期タスクとして起動（dispatcher はブロックしない）。
                # 起動と同時に実行台帳へ登録し、中止命令（§8.10d）が cancel できるようにする。
                chain = asyncio.create_task(
                    self._execute_chain(task_file, task, subtasks)
                )
                self._track_chain(task["task_id"], chain)
            except Exception as e:
                logger.exception("タスクの分解/受付に失敗: %s", task_file)
                try:
                    await self._update_status(task_file, "failed", result=str(e))
                except Exception:
                    logger.exception("failed への更新に失敗: %s", task_file)
                try:
                    await self._notify(
                        f"タスクの受付に失敗しました: {e}",
                        task.get("channel_id"),
                        team_id=task.get("team_id"),
                        thread_ts=task.get("thread_ts"))
                except Exception:
                    logger.exception("受付失敗通知の送信に失敗: %s", task_file)

    # NOTE: タスク分解は TaskDecomposer (ai_gateway/decomposer.py) が担う。
    # _dispatcher から self.decomposer.decompose を呼び出す。
    # 分解結果: [{"step": 1, "command": "...", "execution": "agent", "depth": "deep",
    #             "confidence": 0.9, "depends_on": []}, ...]（execution × depth 2軸）

    # ── 会話ループ: u-zu の発話を脳 LLM で会話・要約（§8.3 (A)） ──

    async def _conversation_loop(self):
        """会話キュー（init）を監視し、発話を ConversationManager に渡す。

        取得時に processing へ予約し（共有 FileQueue）、処理済みは done/ へ退避する（履歴・再処理防止）。
        確定タスクの生成はここではなく着手確認後（_exec_confirmation_loop）に行う。quarantine_on_error=False
        は、予約済みのため再取得されず、退避失敗時も processing のまま残せばよいことによる。
        """
        await self.conversation_q.run(
            self._handle_conversation_message,
            ready_status="init", reserve_status="processing", quarantine_on_error=False)

    async def _handle_conversation_message(self, msg_file: str, msg: dict):
        """会話メッセージ 1 件を処理する。脳 LLM 呼び出しは同期ブロックのため to_thread で実行する。

        失敗は握りつぶさずユーザーへ返す（無言ドロップ防止）。例外をここで吸収するため、呼び出し元の
        run は常に done/ へ退避する（現行挙動どおり「処理を試みたら done」）。通知自体の失敗は無視。
        """
        try:
            # 脳 LLM の無応答区間はハートビートで進捗を同スレッドへ返す（§10.8）
            await self._run_with_heartbeat(
                "会話応答の生成", self.conversation.handle_message, msg,
                channel=msg.get("channel_id"),
                team_id=msg.get("team_id"),
                thread_ts=msg.get("thread_ts"),
            )
        except Exception:
            logger.exception("会話メッセージ処理失敗: %s", msg_file)
            try:
                await self._notify(
                    "すみません、処理に失敗しました。もう一度お願いします。",
                    msg.get("channel_id"),
                    team_id=msg.get("team_id"),
                    thread_ts=msg.get("thread_ts"))
            except Exception:
                logger.exception("会話失敗通知の送信に失敗")

    # ── 制御ループ: u-zu の制御命令を実行（手動 ollama 停止、§8.10c） ──

    async def _control_loop(self):
        """制御コマンドキュー（controls/）を監視し、命令を実行して結果を Slack へ返す。

        u-zu は別プロセスなので停止本体 process_mgr.stop_ollama（SSOT）を直接呼べない。
        u-zu が controls/ に書いた命令をここで拾い、対応操作へ委譲する（経路 Slack→u-zu→sa-ru）。
        SSH を伴う停止は同期ブロックのため to_thread で別スレッド実行する（他ループと同様）。
        処理済みは done/ に退避（再処理防止）。stop_ollama は §7.1 どおり再起動せず、次の推論で
        ollama が自動再ロードする。
        """
        # status=pending を取得する。予約書換はしない（reserve_status 未指定）: 単一消費者なので予約
        # マークは不要で、あえて pending のまま実行することで、実行と done/ 退避の間でクラッシュしても
        # 次回起動で再実行される（取りこぼし防止）。stop_ollama は冪等なので再実行は安全。
        # 処理失敗時は quarantine_on_error=True: pending のまま残すと毎ポーリングで再実行され Slack 通知
        # ストームになるため failed/ へ隔離してループは継続する（gather 経由の全体停止も防ぐ）。
        await self.control_q.run(
            self._handle_control_record,
            ready_status="pending", quarantine_on_error=True)

    async def _handle_control_record(self, ctl_file: str, ctl: dict):
        """制御命令 1 件を実行する。失敗は run へ送出し failed/ 隔離に委ねる。"""
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
            await self._notify(
                msg, ctl.get("channel_id"),
                team_id=ctl.get("team_id"),
                thread_ts=ctl.get("thread_ts"))
        except Exception:
            logger.exception("制御命令の結果通知に失敗: %s", command)

    # ── 停止命令の即時実行（§8.10d）: 実行台帳と cancel 本体 ──

    def _track_chain(self, task_id: str, chain: asyncio.Task):
        """連鎖実行タスクを実行台帳へ登録する。完了時に台帳から自動削除する（§8.10d）。"""
        entry = self._running_tasks.setdefault(task_id, {"chain": None, "workers": set()})
        entry["chain"] = chain
        chain.add_done_callback(lambda _t: self._running_tasks.pop(task_id, None))

    def _track_worker(self, task_id: str, worker: asyncio.Task):
        """worker タスクを実行台帳の該当エントリへ登録する（§8.10d）。

        エントリ不在（台帳未登録のタスク・連鎖終端後の遅延投入）は登録しない — その worker は
        cancel 対象から漏れるが、中止済み task_id 集合のガード（_execute_worker_task 冒頭）が
        実行自体を止めるため、中止の実効性は失われない。
        """
        entry = self._running_tasks.get(task_id)
        if entry is None:
            return
        entry["workers"].add(worker)
        worker.add_done_callback(lambda _t: entry["workers"].discard(_t))

    def _cancel_running(self, task_id: str):
        """実行台帳の該当エントリ（worker 群 → 連鎖）を cancel する（台帳に無ければ何もしない）。

        遠隔プロセスの回収は実行アダプタごとの既存資源回収経路に乗る（§8.10d）: headless は
        CancelledError でローカル ssh を kill し `-tt` の SIGHUP がリモート `claude -p` へ伝播、
        pty は finally の wrapper.close（tmux kill-session）。subprocess（ollama 単発）は
        別スレッド同期実行のため途中打ち切りできないが、結果は破棄され後続ステップは走らない。
        """
        entry = self._running_tasks.pop(task_id, None)
        if entry is None:
            return
        for worker in list(entry["workers"]):
            worker.cancel()
        chain = entry.get("chain")
        if chain is not None:
            chain.cancel()

    def request_cancel(self, msg: dict) -> dict:
        """停止命令の実行本体（ConversationManager へ注入する canceller・§8.10d）。

        会話処理は to_thread の別スレッドで走るため同期関数とし、asyncio タスクの cancel を
        含む本体（_cancel_conversation_targets）はイベントループへ委譲して結果を待つ。
        タイムアウト 60 秒は qu-e への SSH push（_update_status 内・接続タイムアウトあり）を
        含んでも決着に十分な上限で、超過時は例外が会話側へ返り原因明示で報告される。
        """
        if self._loop is None:
            raise RuntimeError("イベントループ未起動のため停止命令を実行できません")
        future = asyncio.run_coroutine_threadsafe(
            self._cancel_conversation_targets(msg), self._loop)
        return future.result(timeout=60)

    async def _cancel_conversation_targets(self, msg: dict) -> dict:
        """発話と同一会話面（team_id + channel_id 一致）の計画・タスクを全て停止する（§8.10d）。

        戻り値は報告用 {"confirms": [...], "running": [...], "queued": [...], "held": [...]}
        （各要素は表示用の 1 行文字列）。規則が決定的なため対象の曖昧さは生じず、確認往復は
        行わない。終端 status は failed を再利用し result に中止命令による停止と刻む
        （§8.10 の却下中止と同じ規律。アーカイブ・qu-e への終了通知・起動時予約回収の非対象と
        いう終端の契約を既存経路で満たすため）。
        """
        # 会話面の照合は None と ""（レコード側の既定値）を同一視して正規化する。素の比較だと
        # msg 側の欠落（None）とレコード側の既定 "" が不一致になり、停止対象を取りこぼす
        team_id = msg.get("team_id") or ""
        channel_id = msg.get("channel_id") or ""
        report = {"confirms": [], "running": [], "queued": [], "held": []}

        # (a) 提示中の計画（exec-confirm pending）→ cancelled に書換えて done/ へ退避。
        # 以後の着手ボタン押下はレコード不在として u-zu 側で安全に無視される
        # （resolve_exec_confirm は pending 以外・不在で False を返す既存契約）。
        for path, record in self.exec_confirm_q.iter_records():
            if record.get("status") != "pending":
                continue
            if ((record.get("team_id") or "") != team_id
                    or (record.get("channel_id") or "") != channel_id):
                continue
            record["status"] = "cancelled"
            record["decided_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            record["decided_by"] = msg.get("user_id", "")
            atomic_write_json(path, record)
            self.exec_confirm_q.mark_done(path)
            report["confirms"].append(_cancel_display(record.get("summary") or ""))

        # (b) 未着手 (c) 実行中 (d) 承認保留のタスクを停止する
        for path, task in self.task_q.iter_records():
            status = task.get("status")
            if status not in ("init", "accepted", "in_progress", STATUS_PENDING_APPROVAL):
                continue
            if ((task.get("team_id") or "") != team_id
                    or (task.get("channel_id") or "") != channel_id):
                continue
            task_id = task.get("task_id", "")
            # 先に中止済み集合へ入れ、dispatcher の連鎖起動・worker のキュー取得を塞いでから
            # 実行中の asyncio タスク群を cancel する（cancel の隙間からの遅延実行を防ぐ）
            self._cancelled_tasks.add(task_id)
            self._cancel_running(task_id)
            if status == STATUS_PENDING_APPROVAL:
                # 保留の拠り所（承認レコード）を先に退避する（§8.10 _resolve_hold と同順。
                # 残すと決着待ちの孤児レコードが承認ディレクトリに滞留する）
                await asyncio.to_thread(
                    self._archive_held_records, task_id, task.get("held_approval_id") or "")
            try:
                await self._update_status(path, "failed", result=_CANCELLED_BY_CONTROL)
            except FileNotFoundError:
                continue  # 走査と停止の間に完了/失敗で終端済み（アーカイブ移動済み）→ 停止対象外
            label = _cancel_display(task.get("command") or "")
            if status == "in_progress":
                report["running"].append(label)
            elif status == STATUS_PENDING_APPROVAL:
                report["held"].append(label)
            else:
                report["queued"].append(label)

        logger.info(
            "中止命令を実行: team=%s channel=%s 計画=%d 実行中=%d 未着手=%d 承認待ち=%d",
            team_id, channel_id, len(report["confirms"]), len(report["running"]),
            len(report["queued"]), len(report["held"]))
        return report

    # ── 着手確認ループ: 確認の決着を検知して確定タスクを生成（§8.3 (B)） ──

    async def _exec_confirmation_loop(self):
        """着手確認レコードをポーリングし、決着（confirmed / rejected）を処理する。

        - confirmed: ConversationManager.create_exec_task で確定タスク（status=init）を生成
                     → 既存 dispatcher が拾う（以降は現行フロー無改変）
        - rejected:  実行せず会話継続を促す
        pending は人間がボタンで決着させるまで待ち続ける（§8.10b。自動 timeout で締め直しを
        強いない。§8.10 承認と違い同期で待つ worker プロセスが無いため期限が不要）。
        処理済みレコードは done/ に退避する。
        """
        while True:
            # 全件走査（pending を残したまま confirmed/rejected だけ決着させるため pick-one では
            # なく iter_records を使う）。共有 FileQueue 経由のため、壊れたレコードは failed/ へ隔離される
            # （従来この経路だけ隔離せず continue していたドリフトを解消）。
            for path, record in self.exec_confirm_q.iter_records():
                status = record.get("status")
                if status in ("confirmed", "rejected"):
                    await self._finalize_confirm(path, record, status)
            await asyncio.sleep(self.exec_confirm_poll)

    @property
    def approval_pipeline(self):
        """承認パイプラインを初回 y/n 検出時に遅延生成する（設計 §08）。

        approval-pipeline は sa-ru 配下（/opt/taka-ma/sa-ru/approval-pipeline、08 で配備）に
        あり PYTHONPATH 経由で import する。未配備の段階では sa-ru は y/n を検出しない限り本経路に
        入らないため、遅延生成にすることで approval-pipeline 不在でも sa-ru 単体で起動できる。
        """
        if self._approval_pipeline is None:
            from main import ApprovalPipeline
            self._approval_pipeline = ApprovalPipeline(
                self.config, slack_notifier=self.slack, ssh_host=self._mbp_host)
        return self._approval_pipeline

    async def _finalize_confirm(self, path: str, record: dict, outcome: str):
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
        except Exception:
            logger.exception("着手確認の決着処理失敗: %s (%s)", path, outcome)
            try:
                await self._notify(
                    ":x: 着手確認の処理に失敗しました。お手数ですがもう一度お願いします。",
                    record.get("channel_id"),
                    team_id=record.get("team_id"),
                    thread_ts=record.get("thread_ts"))
            except Exception:
                logger.exception("着手確認の失敗通知の送信に失敗: %s", path)

    # ── 承認保留ループ: 保留タスクの決着を検知して再投入 / 中止（§8.10） ──

    async def _approval_hold_loop(self):
        """保留中（status=pending_approval）のタスクを走査し、承認の決着を反映する。

        人間の決着に期限は無いため、ここは「待ち続ける」ことが正常動作である。保留はタスク
        ファイルと承認レコードだけで自己完結しており（メモリ上に待機状態を持たない）、sa-ru を
        再起動してもこのループが拾い直す。1 件の決着処理失敗でループを殺さないよう例外は
        飲み込み、次周回で再試行する（決着はディスク上に残っているため取りこぼさない）。
        """
        while True:
            for path, task in self.task_q.iter_records():
                if task.get("status") != STATUS_PENDING_APPROVAL:
                    continue
                try:
                    await self._resolve_hold(path, task)
                except Exception:
                    logger.exception("承認保留の決着処理に失敗（次周回で再試行）: %s", path)
            await asyncio.sleep(self.approval_poll)

    async def _resolve_hold(self, path: str, task: dict):
        """保留タスク 1 件について承認レコードを見て、再投入 / 中止を決める（§8.10）。

        - pending のまま       → 何もしない（人間の決着待ち。期限なし）
        - approved             → 承認レコードを done/ へ退避し、タスクを init へ戻して再投入
        - rejected / その他    → タスクを failed で中止
        - レコード不在         → 追跡不能。failed で中止する（保留の拠り所を失っており、放置
                                 すると誰にも決着させられないタスクが永久に残る）

        再投入前に承認レコードを退避するのは、保留を解消してからでないと次の worker が
        「保留中」と判定され（`_held_approval`）、走った途端にまた畳まれるため。
        """
        channel = task.get("channel_id")
        team_id = task.get("team_id")
        thread_ts = task.get("thread_ts")
        task_id = task.get("task_id", "")
        request_id = task.get("held_approval_id") or ""
        record = await asyncio.to_thread(self._read_approval_record, request_id)
        status = (record or {}).get("status")

        if record is not None and status == STATUS_APPROVAL_PENDING:
            return  # 決着待ち（保留の正常状態）

        if status == STATUS_APPROVAL_APPROVED:
            await asyncio.to_thread(self._archive_held_records, task_id, request_id)
            # 再投入回数の上限（§8.10）。承認 → 再投入 → 同じ操作で再び保留、が延々繰り返す
            # 場合に打ち切る。上限は「収束しなかった」ことの表明であり、承認そのものの否定ではない。
            # 数値として解釈できない値（外部からの破損・手編集）は「収束したか判断できない」ため
            # 上限超過と同じ扱いにする。0 に倒すと上限を素通りして循環が止まらなくなり、例外を
            # 上へ投げると保留ループが同じタスクで毎周回失敗し続けてログを埋める。
            try:
                reinject_count = int(task.get("_reinject_count") or 0) + 1
            except (TypeError, ValueError):
                logger.warning("_reinject_count を解釈できません（上限超過として扱う）: %s", path)
                reinject_count = self.max_reinject + 1
            if reinject_count > self.max_reinject:
                result_path = await self._update_status(
                    path, "failed",
                    result=f"再投入が上限（max_reinject={self.max_reinject}）を超えました")
                await self._notify(
                    f"承認と再投入が {self.max_reinject} 回を超えても収束しなかったため中止しました。"
                    f"\n結果ファイル: {result_path}",
                    channel, team_id=team_id, thread_ts=thread_ts)
                return
            # init へ戻すと dispatcher の claim("init") が拾い、凍結プラン（_plan）のうち
            # completed_steps に無い step だけが実行される。held_approval_id は次の保留で
            # 上書きされるまで残ると誤検知の元になるため、ここで消す。
            await self._update_status(path, "init", extra={
                "_reinject_count": reinject_count,
                "held_approval_id": "",
            })
            await self._notify("承認を確認しました。未了分から再開します。", channel,
                               team_id=team_id, thread_ts=thread_ts)
            return

        # rejected / レコード不在 / 想定外 status → 中止
        if record is None:
            reason = f"承認レコードが見つかりません (ID: {request_id})"
            message = f"承認待ちの記録を追跡できないため中止しました（ID: {request_id}）。"
        elif status == STATUS_APPROVAL_REJECTED:
            reason = f"人間承認が却下されました (ID: {request_id})"
            message = "却下により中止しました。"
        else:
            reason = f"承認レコードが想定外の status です: {status} (ID: {request_id})"
            message = f"承認の状態を解釈できないため中止しました（status={status}）。"
        await asyncio.to_thread(self._archive_held_records, task_id, request_id)
        # 却下は機械可読の原因コードで記録する（§8.10f。却下された操作の自動再試行を
        # 反復停止ゲートが塞ぐ材料になる）
        cause = "approval_rejected" if status == STATUS_APPROVAL_REJECTED else None
        result_path = await self._update_status(
            path, "failed", result=reason,
            extra=({"failure_cause": cause} if cause else None))
        if cause:
            await self._record_outcome(task, cause)
        await self._notify(f"{message}\n結果ファイル: {result_path}", channel,
                           team_id=team_id, thread_ts=thread_ts)

    def _read_approval_record(self, request_id: str) -> dict | None:
        """承認レコードを読む。不在・破損・不正な request_id は None。

        request_id はタスクファイル由来だが、そのままファイル名に連結するため形式を検証する
        （u-zu の `/taka-ma-approve` と同じく、パス区切り・親参照による承認ディレクトリ外への
        参照を防ぐ・§8.10「request_id の検証」）。
        """
        if not _APPROVAL_ID_RE.fullmatch(request_id or ""):
            return None
        try:
            with open(os.path.join(self.approval_dir, f"{request_id}.json")) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _archive_held_records(self, task_id: str, request_id: str):
        """当該タスクの保留を全て解消する（決着したレコード＋取り残しの保留レコード）。

        通常は保留レコードは 1 タスクに 1 件（Tier3Handler が冪等化する）だが、同一タスクの
        複数サブタスクが**同じ猶予ウィンドウ内で並行して** Tier3 に到達すると、どちらもまだ
        held_at を見つけられず 2 件作られうる。決着した 1 件だけを退避すると、残り 1 件が
        pending のまま `_held_approval` に拾われ、再投入した途端にまた畳まれる（人間が両方の
        ボタンを押すまでタスクが進まない）。

        未決着のまま退避するレコードがあっても承認を飛ばすことにはならない。そのツール実行は
        ブロック済みで、再投入後に同じ操作へ到達すれば Tier3 が改めて立つ（§8.10「同じ操作を
        人間に二度聞くことはあり得る」）。退避先は Tier3Handler._finalize と同じ done/。
        """
        for rid in [request_id] + self._held_request_ids(task_id):
            if not _APPROVAL_ID_RE.fullmatch(rid or ""):
                continue
            src = os.path.join(self.approval_dir, f"{rid}.json")
            done_dir = os.path.join(self.approval_dir, "done")
            try:
                os.makedirs(done_dir, exist_ok=True)
                os.replace(src, os.path.join(done_dir, f"{rid}.json"))
            except FileNotFoundError:
                pass  # 既に退避済み／不在

    def _held_request_ids(self, task_id: str) -> list[str]:
        """当該タスクの保留レコード（status=pending かつ held_at 有り）の request_id を全て返す。"""
        if not task_id:
            return []
        found = []
        for path in sorted(glob.glob(os.path.join(self.approval_dir, "*.json"))):
            try:
                with open(path) as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if (record.get("task_id") == task_id
                    and record.get("status") == STATUS_APPROVAL_PENDING
                    and record.get("held_at")):
                found.append(record.get("request_id")
                             or os.path.basename(path)[:-len(".json")])
        return found

    def _validate_subtask_graph(self, subtasks: list[dict]) -> str | None:
        """サブタスク分解の依存グラフを検証し、不正なら理由文字列を、健全なら None を返す（設計書 §10.3）。

        検出項目:
          - 重複 step: step をキーに futures/results を張るため、重複すると後勝ちで
            サブタスクが 1 件静かに消える（誤った完了判定・二重 set_result 例外の原因）。
          - 自己依存: step が自身に依存すると自分の futures を await して即デッドロック。
          - 循環依存: step 群が相互に依存すると互いの futures を永久 await してデッドロック。
        存在しない step への依存（dangling）は実行時に無視される（_execute_subtask_in_chain の
        `if dep not in futures: continue`）ため、ここでは循環判定の辺からも除外する。除外の判定は
        plan.effective_deps に一本化する（検証・プレビューの wave 分割・実行の 3 者で依存グラフの
        解釈を揃える。ズレると「見せた段構成と実行順が違う」事故になる・設計書 §10.2.1）。
        """
        steps = [s["step"] for s in subtasks]
        seen: set = set()
        dups: set = set()
        for st in steps:
            if st in seen:
                dups.add(st)
            seen.add(st)
        if dups:
            return f"重複した step 番号があります: {sorted(dups)}"

        step_set = set(steps)
        graph: dict = {}
        for s in subtasks:
            st = s["step"]
            deps = s.get("depends_on", []) or []
            if st in deps:
                return f"Step {st} が自分自身に依存しています"
            # 存在する step への辺のみ張る（dangling は実行時無視と揃える）
            graph[st] = effective_deps(s, step_set)

        # DFS 3色塗りで循環検出（GRAY 到達＝後退辺＝循環）
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {st: WHITE for st in step_set}

        def _has_cycle(u) -> bool:
            color[u] = GRAY
            for v in graph.get(u, []):
                if color[v] == GRAY:
                    return True
                if color[v] == WHITE and _has_cycle(v):
                    return True
            color[u] = BLACK
            return False

        for st in step_set:
            if color[st] == WHITE and _has_cycle(st):
                return f"depends_on に循環があります（Step {st} を含む閉路）"
        return None

    async def _execute_chain(self, task_file: str, task: dict, subtasks: list[dict]):
        """サブタスクを依存関係に基づき連鎖実行する。
        依存のないサブタスクは並行でキューに投入し、依存のあるものは前の完了を待つ。
        """
        channel = task.get("channel_id")
        team_id = task.get("team_id")   # 応答先ワークスペース（複数WS運用時のトークン選択用）
        thread_ts = task.get("thread_ts")  # 実行中通知を会話スレッドへ返す（設計書 §8.3）

        # 実行前にサブタスクグラフを検証する（設計書 §10.3「実行前検証」）。重複 step は
        # futures/results が step をキーにするため後勝ちで静かに 1 件消え、循環・自己依存は
        # 互いの futures を永久 await してデッドロックする。いずれも実行前に failed で弾き、理由を返す。
        graph_error = self._validate_subtask_graph(subtasks)
        if graph_error:
            await self._update_status(task_file, "failed", result=graph_error)
            await self._notify(
                f"タスク失敗: サブタスク分解が不正です — {graph_error}", channel,
                team_id=team_id, thread_ts=thread_ts)
            return

        results = {}       # step番号 → 実行結果
        futures = {}       # step番号 → asyncio.Future（完了通知用）
        subtask_map = {s["step"]: s for s in subtasks}

        # 保留からの再投入分（§8.10）。前回までに終わった step の出力はタスクファイルに
        # 永続化されており、再実行せずそのまま結果として引き継ぐ（キーは JSON 都合で文字列）。
        completed_steps = task.get("completed_steps") or {}

        # 各サブタスクの完了を通知する Future を準備
        for s in subtasks:
            futures[s["step"]] = asyncio.get_event_loop().create_future()

        try:
            pending_tasks = []
            for subtask in subtasks:
                t = asyncio.create_task(
                    self._execute_subtask_in_chain(
                        task, subtask, results, futures, channel, completed_steps)
                )
                pending_tasks.append(t)

            # 全サブタスクの完了を待つ（独立ブランチは失敗ブランチの影響を受けない）
            await asyncio.gather(*pending_tasks, return_exceptions=True)

            # 承認保留は失敗より先に判定する（§3.3 (4)「保留を成功としても失敗としても記録しない」）。
            # 保留された step は results に入らないため、順序を誤ると「未了＝失敗」と読み替えられ、
            # 人間が承認する前にタスクが failed で捨てられる。
            held = self._first_hold(futures)
            if held:
                await self._hold_task(task_file, task, subtasks, results, held,
                                      channel, team_id, thread_ts)
                return

            # 全サブタスク成功か判定
            failed_steps = [s["step"] for s in subtasks if s["step"] not in results]
            if not failed_steps:
                final_result = results[subtasks[-1]["step"]]
                # 完了検証（§8.9 / §8.10f）。承認された完了条件（acceptance）があればそれが
                # 主判定 — 実世界に対する検査の全 PASS が「完了」の必要条件。無ければ従来の
                # 主張検出（worker 出力の push/コミット主張の裏取り）へ落ちる（補助・§8.10f）。
                # SSH の同期実行を含むため to_thread でループから切り離す（§10.7）。
                all_outputs = "\n".join(str(v) for v in results.values())
                if task.get("acceptance"):
                    grounding = await asyncio.to_thread(self._ground_acceptance, task)
                else:
                    grounding = await asyncio.to_thread(
                        self._ground_result, task, all_outputs)
                # 遵守照合（§8.10f）: 逐語命令タスクは「命令されたコマンド ⇄ 実行された
                # コマンド」の機械照合を判定に含める。不一致は完了にしない
                if task.get("directive"):
                    executed = self._executed().pop(task["task_id"], [])
                    comp_ok, comp_text = contract_rules.compare_directive(
                        task["directive"], executed)
                    grounding.text += "\n\n" + comp_text
                    if not comp_ok:
                        grounding.ok = False
                        grounding.note = ((grounding.note + "・") if grounding.note else "") \
                            + "命令どおりのコマンドが実行されていない"
                        grounding.cause = grounding.cause or "directive_not_followed"
                        grounding.summary = f"（未達: {grounding.note}）"
                # 証跡は正本（結果ファイル）にも残す（§8.9。Slack 表示と独立に全文へ到達できる）。
                # 未達なら機械可読の失敗原因コードも正本へ刻む（§8.10f 反復停止の材料）
                result_record = f"{final_result}\n\n{grounding.text}"
                cause = None if grounding.ok else (grounding.cause or "grounding_unverified")
                result_path = await self._update_status(
                    task_file, "completed", result=result_record,
                    extra=({"failure_cause": cause} if cause else None))
                # 完了/未完了の文言は検証結果から機械導出する（worker 出力は「worker の報告」
                # として区別して送る）。結果は切り詰めず分割送信し、正本パスを必ず併記する（§8.9）
                if grounding.ok:
                    header = f"タスク完了（結果ファイル: {result_path}）:"
                else:
                    header = (f"⚠ タスク未完了: {grounding.note}"
                              f"（結果ファイル: {result_path}）。worker の報告:")
                await self._notify_chunked(header, final_result,
                                           channel, team_id=team_id, thread_ts=thread_ts)
                await self._notify_chunked("実測確認（sa-ru がコマンド実行で裏取り）:",
                                           grounding.text,
                                           channel, team_id=team_id, thread_ts=thread_ts)
                # 完了結果を発生元の会話セッションへ還流する（§8.9「会話への還流」）。
                # グラウンディング判定を先頭に併記し、worker の自己申告を会話脳が事実として
                # 引き継がないようにする。ファイル I/O とロック取得を含むため to_thread で
                # ループから切り離す（§10.7）
                await asyncio.to_thread(
                    self.conversation.append_task_result, task,
                    f"{grounding.summary}\n{final_result}", result_path,
                    grounding.workspace)
                # 依頼の寿命の更新（§8.10f）: 当該 goal の決着・失敗原因の会話記録・
                # 同一会話の open 目標の再検査。SSH を含むため to_thread（§10.7）
                await asyncio.to_thread(self._finalize_goal, task, grounding)
            else:
                cause = self._chain_failure_cause(futures)
                result_path = await self._update_status(
                    task_file, "failed",
                    extra=({"failure_cause": cause} if cause else None))
                await self._notify_failure(task, subtasks, results, failed_steps, channel,
                                           team_id, thread_ts, result_path=result_path)
                await self._record_outcome(task, cause or "worker_error")

        except Exception as e:
            result_path = await self._update_status(
                task_file, "failed", result=str(e),
                extra={"failure_cause": self._failure_cause_from_error(e)})
            await self._notify(f"タスク失敗: {e}\n結果ファイル: {result_path}",
                               channel, team_id=team_id, thread_ts=thread_ts)
            await self._record_outcome(task, self._failure_cause_from_error(e))
        finally:
            # 遵守照合用の実行コマンド記録はタスク終端で必ず破棄する（肥大防止・§8.10f）
            self._executed().pop(task.get("task_id", ""), None)

    def _ground_result(self, task: dict, worker_outputs: str) -> GroundingReport:
        """完了報告のグラウンディング検証を実行する（同期・to_thread で呼ぶこと。§8.9）。

        検証自体の想定外の失敗で完了通知を止めない（チェーンを failed に落とさない）。
        ただし fail-open にはせず、「検証を実行できなかった」ことを ok=False の実測結果と
        して返す（push 主張が未検証のまま「完了」と報告される事故＝虚偽完了そのものを防ぐ）。
        """
        workspace = None
        try:
            workspace = self._resolve_workspace(task)
            verifier = GroundingVerifier(self.process_mgr.run_ssh_probe)
            report = verifier.verify(workspace, worker_outputs)
            report.workspace = workspace
            return report
        except Exception as e:
            logger.exception("グラウンディング検証で想定外の失敗: task_id=%s", task.get("task_id"))
            note = f"実測確認を実行できませんでした（{type(e).__name__}: {e}）"
            return GroundingReport(
                ok=False, note=note,
                text=f"【実測確認】workspace: {workspace or '（未解決）'}\n{note}",
                summary=f"（実測確認の結果、未完了: {note}）",
                workspace=workspace)

    def _executed(self) -> dict:
        """実行コマンド記録（task_id → Bash コマンド列・遵守照合 §8.10f）の遅延アクセサ。

        テストは __new__ で __init__ を跳ばして個体を作るため、属性の直接参照は
        AttributeError になる。__dict__.setdefault で常に存在を保証する。
        """
        return self.__dict__.setdefault("_executed_commands", {})

    async def _record_outcome(self, task: dict, cause: str | None):
        """失敗原因コードを会話へ記録する（§8.10f）。ファイル I/O を含むため to_thread。

        テストの偽 ConversationManager は record_task_outcome を持たないことがあるため
        存在確認して呼ぶ（無ければ記録なしで続行。実個体では常に存在する）。
        """
        record = getattr(getattr(self, "conversation", None), "record_task_outcome", None)
        if record is None:
            return
        await asyncio.to_thread(record, task, cause)

    def _ground_acceptance(self, task: dict) -> GroundingReport:
        """承認された完了条件（§8.10f acceptance）を実世界に対して検査する（同期・to_thread で呼ぶ）。

        検証自体の想定外の失敗で完了通知は止めないが fail-open にはしない（_ground_result と
        同じ規律）: 検査を実行できなかったことを ok=False・原因コード付きで返す。
        """
        workspace = None
        try:
            workspace = self._resolve_workspace(task)
            verifier = GroundingVerifier(self.process_mgr.run_ssh_probe)
            report = verifier.verify_acceptance(workspace, task.get("acceptance") or [])
            report.workspace = workspace
            return report
        except Exception as e:
            logger.exception("完了条件の検査で想定外の失敗: task_id=%s", task.get("task_id"))
            note = f"完了条件の検査を実行できませんでした（{type(e).__name__}: {e}）"
            return GroundingReport(
                ok=False, note=note,
                text=f"【完了条件の検査】workspace: {workspace or '（未解決）'}\n{note}",
                summary=f"（完了条件の検査の結果、未達: {note}）",
                workspace=workspace, cause="grounding_unverified")

    def _finalize_goal(self, task: dict, grounding: GroundingReport):
        """依頼の寿命の更新（§8.10f・同期・to_thread で呼ぶ）。

        (a) 失敗原因コードを会話セッションへ記録（反復停止判定の材料）、(b) 当該 intent の
        goal_status を検査結果で更新（PASS のときだけ achieved。宣言では閉じない）、
        (c) 同一会話の open 目標を世界に対して再検査し、満たされたものだけ閉じる（後続
        タスクによる達成の機械検出）。intent 側の失敗は実行結果へ波及させない。
        """
        cause = None if grounding.ok else (grounding.cause or "grounding_unverified")
        record = getattr(getattr(self, "conversation", None), "record_task_outcome", None)
        if record is not None:
            try:
                record(task, cause)
            except Exception:
                logger.exception("失敗原因コードの会話記録に失敗: task_id=%s", task.get("task_id"))
        intents_dir = getattr(self, "intents_dir", None)
        if not intents_dir:
            return
        task_id = task.get("task_id", "")
        try:
            if task.get("acceptance"):
                intent_store.set_goal_status(
                    intents_dir, task_id,
                    intent_store.GOAL_ACHIEVED if grounding.ok else intent_store.GOAL_OPEN)
            for record in intent_store.list_open(intents_dir,
                                                 task.get("conversation_id")):
                if record.get("task_id") == task_id:
                    continue
                verifier = GroundingVerifier(self.process_mgr.run_ssh_probe)
                report = verifier.verify_acceptance(
                    record["workspace"], record["acceptance"])
                if report.ok:
                    intent_store.set_goal_status(
                        intents_dir, record["task_id"], intent_store.GOAL_ACHIEVED)
                    # イベントループ外（to_thread）のため直接送る（§10.7 の watchdog と同じ扱い）
                    self.slack.notify(
                        "過去の未達依頼が現在は完了条件を満たしています（達成として閉じます）: "
                        f"{(record.get('summary') or '')[:60]}",
                        task.get("channel_id"), team_id=task.get("team_id"),
                        thread_ts=task.get("thread_ts"))
        except Exception:
            logger.exception("goal 更新・再検査に失敗（実行結果へは波及させない）: %s", task_id)

    @staticmethod
    def _failure_cause_from_error(exc: BaseException) -> str:
        """例外から機械可読の失敗原因コードを導出する（§8.10f。LLM を使わない）。"""
        if isinstance(exc, PreflightFailure):
            return {"ssh": "ssh_unreachable", "git": "git_remote_unreachable",
                    "anthropic": "anthropic_auth"}.get(exc.kind, "worker_error")
        if "timeout" in str(exc).lower():
            return "worker_timeout"
        return "worker_error"

    def _chain_failure_cause(self, futures: dict) -> str | None:
        """失敗した連鎖の代表原因コードを、サブタスク future の例外から導出する（§8.10f）。"""
        for fut in futures.values():
            if not fut.done() or fut.cancelled():
                continue
            exc = fut.exception()
            if exc is None or isinstance(exc, ApprovalHold):
                continue
            return self._failure_cause_from_error(exc)
        return None

    def _write_task_deny(self, task: dict):
        """禁止型拘束（§8.10f constraints forbid=true）をタスク別 deny 規則として登録する。

        decide デーモン（同じ Mac mini 上）が各ツール判定の前にこのファイルを読み、
        パターン一致の Bash コマンドを Tier 判定に入る前に deny する。書込失敗は実行を
        止めない（deny は追加の防壁であり、既存の Tier1/2/3 判定は常に効いている）。
        """
        deny_dir = getattr(self, "task_deny_dir", None)
        if not deny_dir:
            return
        task_id = task.get("task_id", "")
        if not _APPROVAL_ID_RE.fullmatch(task_id):
            return
        forbids = [c for c in (task.get("constraints") or []) if c.get("forbid")]
        patterns = [p for c in forbids for p in (c.get("patterns") or [])]
        # 環境改変コマンドの既定 deny への許可経路（§8.10f 事前予防）: 契約 directive
        # （人が着手確認で承認した逐語命令）に含まれる環境改変コマンドだけを allow_env に
        # 刻む。decide デーモンは allow_env に無い環境改変（git init 等）を既定 deny する
        allow_env = contract_rules.env_mutation_allows(task.get("directive"))
        if not patterns and not allow_env:
            return
        try:
            os.makedirs(deny_dir, exist_ok=True)
            atomic_write_json(
                os.path.join(deny_dir, f"{task_id}.json"),
                {"task_id": task_id, "patterns": patterns,
                 "sources": [c.get("text", "") for c in forbids],
                 "allow_env": allow_env})
            logger.info("タスク別 deny 規則を登録: task_id=%s patterns=%d allow_env=%d",
                        task_id, len(patterns), len(allow_env))
        except OSError:
            logger.exception("タスク別 deny 規則の書込失敗（Tier 判定は継続有効）: %s", task_id)

    @staticmethod
    def _first_hold(futures: dict) -> str | None:
        """いずれかのサブタスクが承認保留で畳まれていれば、その承認 ID を返す（無ければ None）。

        保留は「タスク単位で畳む」（§10.4）ため、1 件でも保留があればタスク全体が保留になる。
        exception() の取得は future の「例外が未回収」警告の解消も兼ねる。
        """
        for fut in futures.values():
            if not fut.done() or fut.cancelled():
                continue
            exc = fut.exception()
            if isinstance(exc, ApprovalHold):
                return exc.request_id
        return None

    async def _hold_task(self, task_file: str, task: dict, subtasks: list[dict],
                         results: dict, request_id: str, channel, team_id, thread_ts):
        """承認保留でタスクを畳み、決着後に未了分から再投入できる状態をディスクに残す（§8.10）。

        永続化するのは (a) 済んだサブタスクの出力 `completed_steps`、(b) 待っている承認の
        `held_approval_id`、(c) 凍結プラン `_plan` の 3 つ。従来 (a) は `_execute_chain` の
        メモリ上にしか無く、sa-ru を再起動すると失われた。(c) を書くのは、再投入時に再分解が
        走ると step 番号が変わり `completed_steps` との対応が崩れる（済んだはずの作業が
        やり直しになる、あるいは別の作業として実行される）ため。

        Slack への保留通知は承認パイプライン（Tier3Handler）が既に送っているため、ここでは
        重ねて送らない（同じ事象を 2 通で知らせない）。
        """
        completed_steps = dict(task.get("completed_steps") or {})
        # 今回終わった分を積み増す（再投入をまたいで累積する。キーは JSON 都合で文字列）
        completed_steps.update({str(step): output for step, output in results.items()})
        await self._update_status(task_file, STATUS_PENDING_APPROVAL, extra={
            "completed_steps": completed_steps,
            "held_approval_id": request_id,
            "_plan": subtasks,
        })
        logger.info("承認保留でタスクを畳みました: task_id=%s 承認ID=%s 済み step=%s",
                    task.get("task_id"), request_id, sorted(completed_steps))

    async def _notify(self, text, channel=None, *, team_id=None, thread_ts=None):
        """Slack 送信をイベントループから切り離す薄いラッパー（§10.7）。

        SlackNotifier.notify は slack-sdk の同期 HTTP 送信で、失敗・遅延時に応答まで
        スレッドを占有する。常駐ループ上で直接呼ぶと 1 通の遅い送信で全ループが凍結するため、
        別スレッド（to_thread）へ逃がして await する。イベントループ外（watchdog スレッド等）は
        従来どおり self.slack.notify を直接呼んでよい。
        """
        await asyncio.to_thread(
            self.slack.notify, text, channel, team_id=team_id, thread_ts=thread_ts)

    async def _run_with_heartbeat(self, label, func, *args,
                                  channel=None, team_id=None, thread_ts=None):
        """同期 LLM 呼び出しを別スレッドで実行しつつ、完了まで一定間隔で進捗を返す（§10.8）。

        func には GenerationProgress を progress キーワードで渡す（func 側が run_ollama へ
        引き回し、生成スレッドが受信チャンクごとにトークン数を刻む）。通知間隔ごとに
        「何の処理が・何秒経過・生成トークン数」を発話と同じスレッドへ返す。「あと何秒」は
        原理的に算出できないため報告しない。ハートビート送信の失敗は本処理に影響させない
        （ログのみ・次周期で再試行）。本処理の完了・例外で即座に打ち切る（完了後に
        「処理中」を届けない）。func の戻り値・例外はそのまま呼び出し元へ返す。
        """
        progress = GenerationProgress()
        start = time.monotonic()
        work = asyncio.ensure_future(asyncio.to_thread(func, *args, progress=progress))
        while True:
            done, _ = await asyncio.wait({work}, timeout=self._heartbeat_interval)
            if done:
                return work.result()  # 例外もここで元のまま再送出される
            elapsed = int(time.monotonic() - start)
            try:
                await self._notify(
                    f"⏳ {label}を処理中です（{elapsed}秒経過・生成 {progress.tokens} トークン）",
                    channel, team_id=team_id, thread_ts=thread_ts)
            except Exception:
                logger.exception("ハートビート通知の送信に失敗（本処理は継続）: %s", label)

    # Slack 1 メッセージに収める本文の上限（Slack の text 上限 4,000 字より余裕を持つ）
    NOTIFY_CHUNK_CHARS = 3500
    # 分割送信の最大メッセージ数。超過分は送らないが、全文は併記した結果ファイルが正本の
    # ため情報は失われない（切り詰め廃止の趣旨は「全文へ到達できる経路を常に残す」§8.9）
    NOTIFY_MAX_CHUNKS = 8

    async def _notify_chunked(self, header: str, body: str, channel=None, *,
                              team_id=None, thread_ts=None):
        """長文結果を切り詰めず分割送信する（§8.9「完了通知の内容規律」）。

        1 通目は header（結果ファイルパス併記済み）+ 本文先頭。以降は本文の続きを
        NOTIFY_CHUNK_CHARS ごとに送る。NOTIFY_MAX_CHUNKS を超える極端な長文は
        打ち切りを明示し、全文は結果ファイルへ誘導する。
        """
        chunks = [body[i:i + self.NOTIFY_CHUNK_CHARS]
                  for i in range(0, len(body), self.NOTIFY_CHUNK_CHARS)] or [""]
        truncated = len(chunks) > self.NOTIFY_MAX_CHUNKS
        chunks = chunks[:self.NOTIFY_MAX_CHUNKS]
        for i, chunk in enumerate(chunks):
            prefix = header + "\n" if i == 0 else f"（続き {i + 1}/{len(chunks)}）\n"
            await self._notify(f"{prefix}```{chunk}```", channel,
                               team_id=team_id, thread_ts=thread_ts)
        if truncated:
            await self._notify(
                "（本文が長いため以降の送信を省略しました。全文は上記の結果ファイルを参照してください）",
                channel, team_id=team_id, thread_ts=thread_ts)

    async def _notify_failure(self, task, subtasks, results, failed_steps, channel,
                              team_id=None, thread_ts=None, result_path=None):
        """失敗時の詳細通知（設計書 §10.3。結果ファイルパスを併記する §8.9）"""
        lines = ["⚠ タスク失敗", "", f"【元の指示】", task["command"], "", "【サブタスク結果】"]
        for s in subtasks:
            step = s["step"]
            axis = _axis_label(s)  # 旧 category に替わる execution/depth 表示
            if step in results:
                lines.append(f"  Step {step}: {s['command']} ({axis}) → ✅ 成功")
            elif step in failed_steps:
                lines.append(f"  Step {step}: {s['command']} ({axis}) → ❌ 失敗")
            else:
                lines.append(f"  Step {step}: {s['command']} ({axis}) → ⏭ スキップ")
        if result_path:
            lines += ["", f"結果ファイル: {result_path}"]
        await self._notify("\n".join(lines), channel, team_id=team_id, thread_ts=thread_ts)

    async def _execute_subtask_in_chain(self, task: dict, subtask: dict,
                                         results: dict, futures: dict,
                                         channel: str, completed_steps: dict | None = None):
        """単一サブタスクを実行する。依存がある場合は先に完了を待つ。

        このサブタスクの完了通知 futures[step] は、成功・依存先失敗・ワーカー例外・投入失敗の
        いずれの経路でも必ず解決する（設計書 §10.3「Future 解決の不変条件」）。未解決のまま
        抜けると、この step に依存する後続サブタスクの `await futures[dep]` が永久ブロックし、
        _execute_chain の gather も戻らず（タスクは in_progress のまま恒久ハング）になるため、
        全経路を try で囲い、例外時は futures[step] へ伝播させて cascading skip を機能させる。

        completed_steps（保留からの再投入時のみ非空）にこの step があれば、worker を起動せず
        その出力を結果として即座に引き渡す（§8.10「既に済んだサブタスクを再実行しない」）。
        """
        step = subtask["step"]
        command = subtask["command"]
        # ya-ta が分解時に判定した生の 2 軸。モデル写像・昇格は worker 側で行う
        execution = subtask.get("execution", "agent")
        depth = subtask.get("depth")
        confidence = subtask.get("confidence")
        # サブタスクに :モデル名 が付いていれば明示指定として尊重（写像・昇格をバイパス）
        model = subtask.get("model")
        depends_on = subtask.get("depends_on", [])

        try:
            # 保留前に完了済みの step は再実行しない（§8.10）。依存待ちより先に置くのは、
            # 済んだ step の依存先もまた済んでいるはずで、待つ意味が無いため（依存先が保留で
            # 未了なら、この step も未了のはずで completed_steps には入らない）。
            done_output = (completed_steps or {}).get(str(step))
            if done_output is not None:
                results[step] = done_output
                futures[step].set_result(done_output)
                await self._notify(
                    f"  サブタスク {step}: 承認前に完了済みのためスキップ", channel,
                    team_id=task.get("team_id"), thread_ts=task.get("thread_ts"))
                return

            # 依存するサブタスクの完了を待つ（複数依存対応）
            dep_results = []
            for dep in depends_on:
                if dep not in futures:
                    continue
                try:
                    await futures[dep]
                except Exception as dep_err:
                    # 依存先が失敗 → cascading skip（下の except で futures[step] へ伝播）
                    raise RuntimeError(
                        f"依存先 Step {dep} が失敗したためスキップ") from dep_err
                dep_results.append(f"Step {dep}: {results[dep]}")

            # 依存ステップの結果を入力に組み込む
            if dep_results:
                context = "\n".join(dep_results)
                command = f"前のステップの結果:\n{context}\n\n上記を踏まえて: {command}"

            # 逐語命令（§8.10f directive）は定型の逐語実行指示で包む（言い換え・別手順の禁止）。
            # 分解されない 1 件プランのため dep_results は常に空で、原文が無傷で届く
            if task.get("directive"):
                command = contract_rules.directive_command(task["directive"])
            # 拘束条件（§8.10f 配布規則）: 全 step 共通の境界として短い定型を前置する。
            # 禁止型は decide デーモンの deny 規則としても機械強制される（_write_task_deny）
            constraints_block = contract_rules.build_constraints_block(
                task.get("constraints") or [])
            if constraints_block:
                command = constraints_block + command

            await self._notify(f"  サブタスク {step}: {_axis_label(subtask)}", channel,
                              team_id=task.get("team_id"), thread_ts=task.get("thread_ts"))

            # キューに投入し、ワーカーに実行させる
            result_future = asyncio.get_event_loop().create_future()
            # subtask の :モデル名 を _model に載せる。未指定なら task 側の _model（あれば）を残す
            user_model = model if model is not None else task.get("_model")
            # 写像テーブルで実行計画（レーン・候補列・明示指定か）を決める。
            # model_override は計画確認でユーザーが上書きしたモデル（§10.2.1。昇格は止めない）
            override_model = subtask.get("model_override")
            lane, candidates, user_specified = self._plan_execution(
                execution, depth, confidence, user_model, override_model)
            queue_item = {
                **task,
                "_command": command,
                "_execution": execution,
                "_depth": depth,
                "_confidence": confidence,
                # 上書きがあれば _model も単一モデルへ倒す。_execute_worker_task は _model が
                # 2 モデル以上のリストなら cross-review へ分岐するため、ここで残すと計画確認の
                # 上書きが実行時に無視され、提示した計画と違うモデル群が走る（§10.2.1 違反）
                "_model": override_model or user_model,
                "_lane": lane,                    # inline / agent（レーン＝解決モデルの method 由来）
                "_candidates": candidates,        # 実行順のモデル候補列（先頭が primary、以降は昇格先）
                "_user_specified": user_specified,  # 明示指定なら昇格・レーン跨ぎ再投入をしない
                "_step": step,
                "_result_future": result_future,
            }

            await self._enqueue(queue_item)

            # ワーカーの実行完了を待つ（ワーカーが例外をセットしたらここで送出される）
            output = await result_future
            results[step] = output
            futures[step].set_result(output)

            await self._notify(f"  サブタスク {step}/{len(futures)} 完了", channel,
                              team_id=task.get("team_id"), thread_ts=task.get("thread_ts"))
        except Exception as e:
            # 成功通知の失敗（set_result 後）はここに来るが futures[step] は解決済みなので
            # 二重解決しない。未解決のときのみ例外を伝播して後続の永久 await を防ぐ。
            if not futures[step].done():
                futures[step].set_exception(e)

    async def _enqueue(self, item: dict):
        """レーンに応じたキューにタスクを投入（満杯時は空きを待つ）。

        レーンは「解決モデルの method」で決まる（`_plan_execution` が算出し `_lane` に格納）。
        subprocess モデル（gemma）＝inline レーン（無制限）、headless/pty モデル（haiku/sonnet/
        opus/gemini）＝agent レーン（heavy_limiter 制限）。inline の gemma が失敗して昇格ラダー
        （headless）へ移る際は `_lane` を agent へ書き換えて再投入し、必ず limiter 配下で走らせる。
        """
        if item.get("_lane") == "inline":
            await self.queue_inline.put(item)
        else:
            await self.queue_agent.put(item)

    # ── ワーカー: execution レーン別にキューから取り出して実行 ──

    async def _worker_inline(self):
        """inline サブタスクを取り出し次第、上限なしで並行起動する（純生成ゆえ絞らない）。"""
        running = []
        while True:
            item = await self.queue_inline.get()
            t = asyncio.create_task(self._execute_worker_task(item))
            self._track_worker(item.get("task_id", ""), t)  # 中止命令の cancel 対象へ登録（§8.10d）
            running.append(t)
            # 完了済みタスク参照を捨てて running リストの無限肥大を防ぐ（保持は GC 抑止のため）
            running = [t for t in running if not t.done()]

    async def _worker_agent(self):
        """agent サブタスクを最大 max_heavy_instances 並行で処理（上限は §8.14 で動的変動）。"""
        running = []
        while True:
            item = await self.queue_agent.get()
            # キューから取り出した後に枠を確保する。確保できるまでここで待つ＝同時起動数を上限に抑える
            await self.heavy_limiter.acquire()
            t = asyncio.create_task(self._execute_heavy_with_release(item))
            self._track_worker(item.get("task_id", ""), t)  # 中止命令の cancel 対象へ登録（§8.10d）
            running.append(t)
            # 完了済みタスク参照を捨てて running リストの無限肥大を防ぐ
            running = [t for t in running if not t.done()]

    async def _execute_heavy_with_release(self, item):
        """heavy 実行後に並行枠を解放する"""
        try:
            await self._execute_worker_task(item)
        finally:
            await self.heavy_limiter.release()

    def _plan_execution(self, execution, depth, confidence, user_model, override_model=None):
        """写像テーブルからレーン・モデル候補列・明示指定フラグを決める（設計書 §2.2）。

        Returns: (lane, candidates, user_specified)
          - override_model（計画確認での上書き・§10.2.1）→ それを primary に昇格ラダーを張る。
            `:モデル名` の明示指定と違い昇格を止めない（人間確認は上流フィルタであって
            昇格の代替ではない）。より新しい意思表示のため user_model より優先する
          - user_model が 2 モデル以上のリスト → cross-review。lane=agent、candidates=そのリスト
          - user_model が単一（str / 1 要素リスト）→ 明示指定。candidates=[その 1 件]、昇格しない
          - 指定なし → primary = matrix 解決、candidates = 昇格ラダー（fallback.max_fallback_attempts で段数制限）
        レーンは candidates 先頭モデルの method で決める（subprocess＝inline / それ以外＝agent）。
        """
        models = self.config["models"]
        routing = self.config["routing"]

        # cross-review（2 モデル以上の明示指定）。計画確認で上書きされていれば単一モデルへ倒す
        if isinstance(user_model, list) and len(user_model) >= 2 and not override_model:
            return "agent", list(user_model), True

        # 1 要素リストは str に揃える
        if isinstance(user_model, list):
            user_model = user_model[0] if user_model else None

        if user_model and not override_model:
            candidates = [user_model]
            user_specified = True
        else:
            # 計画確認の上書きがあればそれを primary に、無ければ写像テーブルで解決する
            primary = override_model or _resolve_model(routing, execution, depth, confidence)
            # ya-ta.yaml の fallback: がコメントのみだと YAML 上 None になり得るため or {} で潰す
            # （実機検証で AttributeError を確認・2026-07-04 と同根）。max は昇格の最大段数。
            max_steps = (self.config.get("fallback") or {}).get("max_fallback_attempts")
            candidates = _escalation_chain(routing, primary, max_steps)
            user_specified = False

        # レーン = 先頭候補モデルの method（subprocess=inline レーン / headless・pty=agent レーン）
        first = candidates[0] if candidates else None
        method = _select_method(models.get(first, {})) if first else "headless"
        lane = "inline" if method == "subprocess" else "agent"
        return lane, candidates, user_specified

    async def _run_candidate(self, item, model_name, command, step,
                             channel, team_id, thread_ts) -> tuple[str, str]:
        """単一モデル候補を method に応じた実行アダプタで走らせ、`(status, payload)` を返す。

        - `("ok", 出力文字列)`: 通常完了
        - `("hold", 承認ID)`: 実行中に承認保留が起きた（§3.3 (4)）

        **保留を例外で返さない**のが要点（§8.10「昇格ラダーを回さない」）。例外にすると
        呼び出し元の昇格ラダーが「モデル障害」と誤認して次段モデルで同じ作業を再実行し、
        別の手順で承認を迂回しうる。保留は障害ではないため戻り値で表現する。

        保留の判定に worker の終了状態は使えない。承認をブロックされた worker は「指示どおり
        終わった」＝正常終了で返る（実機確認済み）ため、根拠は承認レコード（当該タスクに
        status=pending かつ held_at を持つもの）に一本化する。worker が例外で終わった場合も
        先に保留を確認する — 承認ブロック後に worker 側で別のエラーが起きても、実体は保留で
        あり failed に倒すと人間の承認が届かなくなるため、保留を例外より優先する。

        instance_id に model_name を含めるため、昇格で複数モデルを順に走らせても
        workspace / tmux セッション名が衝突しない（Layer3 review 由来の是正を踏襲）。
        """
        model_conf = self.config["models"].get(model_name, {})
        method = _select_method(model_conf, use_case="default")
        task_id = item["task_id"]
        try:
            if method == "subprocess":
                # subprocess 単発実行（ollama / 単発 API）
                output = await asyncio.to_thread(
                    self.process_mgr.run_model_subprocess, model_name, model_conf, command)
            else:
                # headless（Claude Code）/ pty（agy 対話等の汎用対話 CLI）
                model_flag = model_conf.get("model_flag", "")
                cli_command = model_conf.get("command", "claude")
                instance_id = f"{task_id}-step{step}-{model_name}"
                workspace = self._resolve_workspace(item)
                if method == "headless":
                    output = await self._run_worker_headless(
                        instance_id, cli_command, command, model_flag, workspace,
                        channel=channel, team_id=team_id, task_id=task_id, thread_ts=thread_ts)
                else:
                    output = await self._run_worker_pty(
                        instance_id, cli_command, command, channel, model_flag, workspace,
                        team_id=team_id, task_id=task_id, thread_ts=thread_ts)
        except Exception:
            held = await asyncio.to_thread(self._held_approval, task_id)
            if held:
                return ("hold", held)
            raise
        held = await asyncio.to_thread(self._held_approval, task_id)
        if held:
            return ("hold", held)
        return ("ok", output)

    def _held_approval(self, task_id: str) -> str | None:
        """当該タスクに未決着の保留レコードがあれば承認 ID を返す（無ければ None）。

        保留レコードの条件は「status=pending かつ held_at を持つ」（§8.10）。承認パイプライン
        （Tier3Handler）が別プロセスで書き、sa-ru はここで読むだけの片方向共有で、両者は
        `approval.dir` という 1 つの設定値で結ばれる。done/ 配下は決着済みのため走査しない。
        走査失敗（ディレクトリ不在・破損レコード）は保留なしとして扱う — 保留を見落とせば
        タスクは通常の完了/失敗判定に落ちるだけで、承認レコード自体は生き続ける。

        走査そのものは `_held_request_ids` に一本化する（検知と退避が別々の述語で保留を
        数えると、片方だけが拾うレコードが取り残される）。
        """
        held = self._held_request_ids(task_id)
        return held[0] if held else None

    async def _execute_worker_task(self, item: dict):
        """ワーカーがキューから受け取ったサブタスクを実行し、結果を Future にセットする。

        （写像テーブル + 昇格ラダー）:
          - cross-review（`_model` が 2 モデル以上）→ _execute_cross_review
          - inline レーン（`_lane == "inline"`）→ 先頭候補（gemma）のみ実行。失敗 / ESCALATE 申告時は
            残る昇格ラダー（headless モデル）を agent レーンへ再投入し、必ず heavy_limiter 配下で走らせる
          - agent レーン → `_candidates`（primary → 昇格先…）を順に試行（heavy_limiter は
            _execute_heavy_with_release が 1 スロット保持。逐次昇格はその 1 枠で足りる）
          - 明示指定（`_user_specified`）は昇格・レーン跨ぎ再投入をしない（指定モデル尊重）
        昇格の引き金は「例外・タイムアウト」と「worker 出力の ESCALATE: 自己申告」の 2 つ（設計書 §2.2）。

        承認保留（`_run_candidate` が `("hold", 承認ID)` を返す）はこのどちらでもない。昇格せず
        再投入もせず、ApprovalHold を future にセットして即座に返る（§8.10）。ここで昇格すると
        次段モデルが別の手順で同じ操作に到達し、人間の承認を待たずに実行しうる。
        """
        command = item["_command"]
        step = item["_step"]
        result_future = item["_result_future"]
        channel = item.get("channel_id")
        team_id = item.get("team_id")
        thread_ts = item.get("thread_ts")

        # 中止済みタスク（§8.10d）のサブタスクは実行しない。cancel 発行とキュー滞留の隙間
        # （worker 未取得の投入済み item・昇格の agent レーン再投入）からの遅延実行を塞ぐ。
        # future は cancel で解決する（連鎖側も cancel 済みのため待ち手は居ない）。
        if item.get("task_id") in self._cancelled_tasks:
            if not result_future.done():
                result_future.cancel()
            return

        # cross-review 分岐: 2 つ以上のモデル指定で並行投入
        raw_model = item.get("_model")
        if isinstance(raw_model, list) and len(raw_model) >= 2:
            await self._execute_cross_review(item, raw_model)
            return

        lane = item.get("_lane", "agent")
        candidates = item.get("_candidates") or []
        user_specified = item.get("_user_specified", False)

        # 候補皆無（matrix 不備等）は恒久ハング回避のため意味のある例外で即解決する
        if not candidates:
            result_future.set_exception(RuntimeError(
                f"実行可能なモデル候補がありません（execution={item.get('_execution')}, "
                f"depth={item.get('_depth')}, confidence={item.get('_confidence')}）"))
            return

        # ── inline レーン: 先頭候補（gemma）のみ。失敗/ESCALATE は agent レーンへ昇格再投入 ──
        if lane == "inline":
            model_name = candidates[0]
            try:
                status, payload = await self._run_candidate(
                    item, model_name, command, step, channel, team_id, thread_ts)
                if status == "hold":
                    # 承認保留。昇格も再投入もせず、このサブタスクを未了のまま畳む（§8.10）
                    result_future.set_exception(ApprovalHold(payload))
                    return
                output = payload
                reason = _escalate_reason(output)
                if reason and not user_specified and len(candidates) > 1:
                    await self._notify(
                        f"  {model_name} が ESCALATE 申告（{reason}）→ agent レーンへ昇格", channel,
                        team_id=team_id, thread_ts=thread_ts)
                    await self._escalate_to_agent_lane(item, candidates[1:])
                    return
                result_future.set_result(output)
            except Exception as e:
                if user_specified or len(candidates) <= 1:
                    result_future.set_exception(e)
                    return
                await self._notify(
                    f"  {model_name} 障害 → agent レーンへ昇格: {e}", channel,
                    team_id=team_id, thread_ts=thread_ts)
                await self._escalate_to_agent_lane(item, candidates[1:])
            return

        # ── agent レーン: 昇格ラダーをインラインで順に試行（limiter は呼び出し側が保持） ──
        last_error = None
        for idx, model_name in enumerate(candidates):
            is_escalation = idx > 0
            try:
                if is_escalation:
                    await self._notify(
                        f"  {candidates[idx - 1]} → {model_name} へ昇格実行", channel,
                        team_id=team_id, thread_ts=thread_ts)
                status, payload = await self._run_candidate(
                    item, model_name, command, step, channel, team_id, thread_ts)
                if status == "hold":
                    # 承認保留。次段モデルへ昇格させると承認を迂回した実行になりうるため、
                    # ここで候補ループを打ち切る（§8.10「昇格ラダーを回さない」）。
                    result_future.set_exception(ApprovalHold(payload))
                    return
                output = payload
                reason = _escalate_reason(output)
                if reason and not user_specified and idx < len(candidates) - 1:
                    # 上位段が残っていれば昇格継続（この出力は採用しない）
                    await self._notify(
                        f"  {model_name} が ESCALATE 申告（{reason}）→ 次段へ昇格", channel,
                        team_id=team_id, thread_ts=thread_ts)
                    last_error = RuntimeError(f"{model_name} ESCALATE: {reason}")
                    continue
                if reason and (user_specified or idx == len(candidates) - 1):
                    # 明示指定 or 最上位段での ESCALATE は昇格先が無い → failed に倒す
                    result_future.set_exception(RuntimeError(
                        f"最上位モデル {model_name} でも解決不可（ESCALATE: {reason}）"))
                    return
                result_future.set_result(output)
                return
            except Exception as e:
                last_error = e
                await self._notify(
                    f"  {model_name} 障害/難所: {e}", channel, team_id=team_id, thread_ts=thread_ts)
                if user_specified:
                    break  # 明示指定は昇格しない（指定モデル尊重）
                continue

        # 全段失敗 → Future に例外をセット（_execute_chain で捕捉、User へ failed 通知）
        if last_error is None:
            last_error = RuntimeError("実行可能なモデル候補がありません")
        result_future.set_exception(last_error)

    async def _escalate_to_agent_lane(self, item: dict, remaining: list[str]):
        """inline レーンの失敗/ESCALATE を受け、残る昇格ラダーを agent レーンへ再投入する。

        `_lane` を agent へ書き換えて heavy_limiter 配下で走らせる（inline レーンで headless を
        起動すると limiter を素通りして MBP 資源を無制限に消費するのを防ぐ）。同一 item を
        使い回すが instance_id は model_name 込みのため tmux セッション名は衝突しない。
        """
        item["_candidates"] = remaining
        item["_lane"] = "agent"
        item["_execution"] = "agent"
        await self._enqueue(item)

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
        thread_ts = item.get("thread_ts")

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
                else:  # headless（Claude）/ pty（汎用対話 CLI）— いずれも heavy 枠を個別取得
                    model_flag = model_conf.get("model_flag", "")
                    cli_command = model_conf.get("command", "claude")
                    instance_id = f"{item['task_id']}-step{step}-{model_name}"
                    workspace = self._resolve_workspace(item)
                    async with self.heavy_limiter:
                        if method == "headless":
                            output = await self._run_worker_headless(instance_id, cli_command, command, model_flag, workspace, channel=channel, team_id=team_id, task_id=item["task_id"], thread_ts=thread_ts)
                        else:
                            output = await self._run_worker_pty(instance_id, cli_command, command, channel, model_flag, workspace, team_id=team_id, task_id=item["task_id"], thread_ts=thread_ts)
                return (model_name, output)
            except Exception as e:
                return (model_name, e)

        results = await asyncio.gather(*[_run_one(m) for m in models])

        # 承認保留は統合より先に判定する（§8.10）。保留された（＝操作をブロックされた）モデルの
        # 出力を混ぜて統合すると、承認前の中途半端な成果が「完了結果」として記録される。
        held = await asyncio.to_thread(self._held_approval, item["task_id"])
        if held:
            result_future.set_exception(ApprovalHold(held))
            return

        successes = [(m, r) for m, r in results if not isinstance(r, Exception)]
        failures = [(m, r) for m, r in results if isinstance(r, Exception)]

        for m, e in failures:
            await self._notify(f"  cross-review: {m} 失敗（結果から除外）: {e}", channel,
                              team_id=team_id, thread_ts=thread_ts)

        if not successes:
            result_future.set_exception(
                RuntimeError(f"cross-review: 全モデル失敗 — {[m for m, _ in failures]}")
            )
            return

        # ya-ta（DeepSeek-R1 32B）で知的統合。統合が失敗（ollama 非 0 終了・タイムアウト）
        # しても result_future を必ず解決する。未解決のまま抜けると、この step の
        # _execute_subtask_in_chain が `await result_future` で永久ブロックする。
        try:
            integrated = await asyncio.to_thread(
                self._integrate_cross_review, command, successes)
        except Exception as e:
            await self._notify(f"  cross-review 統合に失敗: {e}", channel,
                              team_id=team_id, thread_ts=thread_ts)
            result_future.set_exception(e)
            return
        result_future.set_result(integrated)

    def _integrate_cross_review(self, command: str, results: list[tuple[str, str]]) -> str:
        """各モデルの結果を ya-ta（DeepSeek-R1 32B）で知的統合する。
        Mac mini 上の ollama 経由で DeepSeek-R1 32B（ya-ta と同モデル）に投入。

        ollama 実行の失敗を検出し例外化する（設計書 §2.2 / §10.3）。従来は returncode を無視して
        result.stdout をそのまま返していたため、ollama が非 0 終了（モデル未 pull・OOM 等）
        したとき空文字を「統合成功」として返していた。TimeoutExpired（180s 超）も送出され
        呼び出し元 _execute_cross_review で捕捉されず result_future が未解決になっていた。
        いずれも RuntimeError に畳んで呼び出し元へ返し、future を確実に解決させる。
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
        # ya-ta 分解脳と同モデルで統合する（モデル名の SSOT は ya-ta.yaml。直書きすると
        # モデル入替時に取り残される — 実際に deepseek 直書きが入替 grep から漏れた前科）。
        # HTTP API 経由（§8.4）。OllamaError は RuntimeError 派生のため、呼び出し元の
        # 「統合失敗 → 当該ステップ failed」処理（§2.2）にそのまま乗る。
        return run_ollama(self.config["ya-ta"]["model"], prompt,
                          timeout=self.config["ya-ta"]["llm_timeout_sec"],
                          host=self.config["sa-ru"]["ollama_host"])

    async def _run_worker_pty(self, instance_id: str, cli_command: str, command: str, channel: str, model_flag: str = "", workspace: str | None = None, team_id: str | None = None, task_id: str = "", thread_ts: str | None = None) -> str:
        """対話型 worker CLI を PTY 経由で実行する汎用ラッパー呼び出し。

        Claude Code / Gemini CLI / Codex 等を共通の WorkerPtyWrapper で扱う。
        cli_command で起動コマンド名（claude / gemini 等）を指定する。
        workspace を渡すとタスク専用作業ディレクトリで起動する（qu-e の path→task_id 帰属の前提）。

        駆動ループ（§8.5 / 08-approval-pipeline）:
          worker 起動 → タスク投入 → stdout を逐次読取 → y/n プロンプト検出時は
          ApprovalPipeline.process で承認/拒否（Tier1 自動 / Tier2 qu-e / Tier3 人間）→
          worker 完了（EOF）まで継続 → 蓄積した stdout を最終出力として返す。

        pexpect は同期ブロックのため _drive を to_thread で別スレッド実行する。承認は
        async（Tier3 が Slack 応答を await）なので、run_coroutine_threadsafe で event loop に委譲し
        結果を待つ（pipeline 内で wrapper.approve/deny が呼ばれる）。
        """
        loop = asyncio.get_running_loop()
        wrapper = WorkerPtyWrapper(instance_id, command=cli_command, model_flag=model_flag, cwd=workspace)

        def _drive() -> str:
            """PTY を起動してタスクを流し、承認プロンプトを捌きながら出力を集めて返す。

            pexpect でブロッキングに読むため別スレッド（to_thread）で回す前提の同期関数。
            agy 対話等の汎用対話 CLI が出すレガシー y/n テキストのプロンプトを検出したら
            approve/deny を裏で判定し、結果を wrapper に書き戻す（Claude Code は headless
            アダプタへ移行したため、本経路は Ink メニューを扱わない）。
            """
            import pexpect
            from interceptor import detect_prompt, strip_ansi

            wrapper.start()
            chunks: list[str] = []
            context_buf: list[str] = []
            prompt_patterns = [r"\[y/n\]", r"\(yes/no\)", r"Allow\?"]

            def _consume() -> None:
                """直近でマッチしたレガシー y/n プロンプトを承認パイプライン（Tier1/2/3）へ回す。"""
                before = wrapper.child.before or ""
                # chunks（最終出力）/ context_buf（Tier3 承認リクエストの Context 欄・
                # extract_command の対象）は ANSI を除去してから積む。生のまま積むと
                # Context 欄が制御コードだらけで文字化けし、extract_command の
                # "Run:"/"Write to:" 判定も阻害される（実機検証で確認・是正）。
                chunks.append(strip_ansi(before))
                context_buf.extend(strip_ansi(before).splitlines())
                matched = wrapper.child.after or ""
                combined = before + matched
                prompt = detect_prompt(combined, context_buf)
                if prompt is None:
                    return
                asyncio.run_coroutine_threadsafe(
                    self.approval_pipeline.process(
                        prompt, wrapper, instance_id,
                        team_id=team_id, channel=channel, task_id=task_id, thread_ts=thread_ts), loop
                ).result()

            # 起動直後にタスク投入前の承認プロンプトを出す CLI があるため、先に片付ける。
            # 先にタスクを送ると、プロンプト表示中にタスク文字列が入力へ紛れ込む恐れがある。
            # 出ない CLI は 5 秒でタイムアウトして通常フローへ進む（実害のある待ちにはならない）。
            try:
                idx = wrapper.child.expect(prompt_patterns + [pexpect.EOF], timeout=5)
                if idx == len(prompt_patterns):        # EOF — 起動直後に終了（異常系）
                    chunks.append(strip_ansi(wrapper.child.before or ""))
                    return "".join(chunks)
                _consume()
            except pexpect.exceptions.TIMEOUT:
                pass  # 起動時プロンプトなし。通常フローへ。

            wrapper.send_task(command)
            send_time = time.monotonic()
            patterns = prompt_patterns + [pexpect.EOF, pexpect.TIMEOUT]
            menu_count = len(prompt_patterns)
            eof_idx = menu_count
            while True:
                # 起動猶予（_TASK_STARTUP_GRACE_SEC）を過ぎるまでは pexpect 既定の長い
                # timeout（300 秒、TIMEOUT に到達したら異常とみなす）のまま待つ。猶予を過ぎたら
                # 以降は短い quiet timeout に切り替え、「無音」を完了シグナルとして扱う。
                elapsed = time.monotonic() - send_time
                call_timeout = _IDLE_QUIET_SEC if elapsed >= _TASK_STARTUP_GRACE_SEC else None
                idx = wrapper.child.expect(patterns, timeout=call_timeout)
                if idx < menu_count:                   # プロンプト検出
                    _consume()
                elif idx == eof_idx:                   # EOF — worker 完了（agy 等、非対話 CLI 用）
                    chunks.append(strip_ansi(wrapper.child.before or ""))
                    break
                else:                                  # TIMEOUT
                    chunks.append(strip_ansi(wrapper.child.before or ""))
                    if call_timeout is not None:
                        # 起動猶予後の無音＝対話モードは EOF しないため、これが唯一の完了シグナル
                        # （実機検証で確認・是正）。
                        break
                    raise RuntimeError(f"worker PTY timeout: {instance_id}")
            return "".join(chunks)

        try:
            return await asyncio.to_thread(_drive)
        finally:
            # close は tmux kill-session の SSH（同期ブロッキング）を含むため、イベントループを
            # 凍結させないよう別スレッドで実行する（§10.7）。
            await asyncio.to_thread(wrapper.close)

    async def _run_worker_headless(self, instance_id: str, cli_command: str, command: str,
                                   model_flag: str = "", workspace: str | None = None, *,
                                   channel: str | None = None, team_id: str | None = None,
                                   task_id: str = "", thread_ts: str | None = None) -> str:
        """Claude Code を headless（claude -p + stream-json + PreToolUse フック）で実行する。

        interactive(pty) 経路（_run_worker_pty）の Claude 版置き換え。対話モードを覗き見て y/n を
        送るのではなく、非対話 1 プロセスで実行し、各ツールの承認は PreToolUse フックが同期ゲートする
        （設計 §8.5 headless アダプタ）。

        起動前に PreToolUse フックの settings を生成して MBP の workspace に書き込む。フックは MBP で
        発火し、SSH（ControlMaster 多重化）で Mac mini の decide クライアント → 常駐 decide デーモン
        （中核 decide()）を呼ぶ（設計 Appendix §2.1）。Tier3 承認リクエストの応答先
        （team_id / channel / thread_ts / task_id）はフック stdin に乗らないため、ここで settings の
        フックコマンド引数へ焼き込む。
        """
        # 起動前プリフライト: SSH / git remote / Anthropic の認証・到達性を検査し、不合格なら
        # worker を起動しない。失敗種別と実出力の該当行を Slack へ明示し、SSH と Anthropic の
        # 混同診断を防ぐ。昇格ラダーの再突入（TTL 内キャッシュの再検出）では同じ通知を重ねない。
        # 例外はそのまま送出し、既存の失敗経路（昇格・failed 決着・Slack 通知）に乗せる。
        try:
            await asyncio.to_thread(self.preflight.check, workspace, cli_command)
        except PreflightFailure as pf:
            if not pf.cached:
                await self._notify(pf.report(), channel, team_id=team_id, thread_ts=thread_ts)
            raise
        # headless 運用値（フック timeout・python パス・全体上限）は sa-ru.yaml の headless
        # ブロックを唯一の源にする（コード既定値なし。欠落は KeyError で即落とし診断位置を揃える）
        hcfg = self.config["headless"]
        # フック settings を生成（フックコマンド＝ssh mini decide_client → デーモン UDS、task 文脈を argv へ焼込）。
        settings = build_hook_settings(
            hcfg["mini_host"], hcfg["decide_client"], hcfg["decide_socket"],
            task_id=task_id, team_id=team_id, channel=channel, thread_ts=thread_ts,
            instance_id=instance_id, timeout_sec=hcfg["hook_timeout_sec"],
            python_bin=hcfg["python_bin"])
        # settings を MBP 上の workspace に書き込む（_push_task_context と同じ ssh の cat > 方式）。
        # claude -p --settings がこのファイルを読んでフックを有効化する。
        settings_path = f"{workspace}/.taka-hook-settings.json"
        await asyncio.to_thread(
            self.process_mgr.run_ssh_command,
            f"mkdir -p {workspace} && cat > {settings_path}",
            stdin_text=json.dumps(settings, ensure_ascii=False))
        # SSH 越しに claude -p を起動し、stream-json を解析して最終出力を得る。
        runner = WorkerHeadlessRunner(
            instance_id, command=cli_command, model_flag=model_flag,
            ssh_host=self._mbp_host, cwd=workspace, hook_settings_path=settings_path)
        result = await runner.run(command, timeout=hcfg["run_timeout_sec"])
        # 実行された Bash コマンドを遵守照合（§8.10f）用に記録する。タスク終端
        # （_execute_chain の finally）で必ず破棄されるため肥大しない
        commands = getattr(result, "commands", None)  # 偽 result（テスト）は持たないことがある
        if commands and task_id:
            self._executed().setdefault(task_id, []).extend(commands)
        # 終了コードによる成功判定の機械化（§8.9）。result イベントを受信していても
        # 非 0 終了は成功として扱わず、失敗経路（retry / 昇格ラダー）へ回す。
        if result.returncode not in (0, None):
            raise RuntimeError(
                f"headless worker が非 0 終了 (exit={result.returncode}): {instance_id}: "
                f"{(result.text or '')[:200]}")
        return result.text

    # ── /exam_gw ドライラン結果フォーマット ──

    def _format_exam_result(self, subtasks: list[dict]) -> str:
        """ドライラン結果を Slack 通知用テキストに整形（execution × depth + 写像表示）"""
        lines = ["ya-ta 検証結果（実行なし）\n"]
        for s in subtasks:
            model = s.get("model")
            # 実行計画（レーン・候補列・明示指定か）を実行と同じ写像で解決して表示する
            lane, candidates, user_specified = self._plan_execution(
                s.get("execution", "agent"), s.get("depth"), s.get("confidence"), model)
            primary = candidates[0] if candidates else None
            if user_specified:
                if len(candidates) >= 2:
                    model_display = f"{candidates}（ユーザー指定・cross-review）"
                else:
                    model_display = f"{primary}（ユーザー指定。昇格なし）"
            elif primary is not None:
                if len(candidates) > 1:
                    ladder_display = " → ".join(candidates[1:])
                    model_display = f"{primary}（写像デフォルト、昇格: {ladder_display}）"
                else:
                    model_display = f"{primary}（写像デフォルト、昇格なし）"
            else:
                model_display = "（候補なし — matrix 不備）"
            model_conf = self.config["models"].get(primary, {}) if primary else {}
            methods = model_conf.get("methods") or ([model_conf.get("method")] if model_conf.get("method") else [])
            selected_method = _select_method(model_conf) if model_conf else "unknown"
            lines.append(
                f"Step {s['step']}: {s['command']}\n"
                f"  execution: {s.get('execution', 'N/A')} / depth: {s.get('depth')}\n"
                f"  lane: {lane}\n"
                f"  model: {model_display}\n"
                f"  methods: {methods} → selected: {selected_method}\n"
                f"  depends_on: {s.get('depends_on', [])}\n"
                f"  confidence: {s.get('confidence', 'N/A')}\n"
            )
        return "\n".join(lines)

    # ── アーカイブローテート ──

    async def _daily_cleanup(self):
        """タスクアーカイブ（done/）の古いディレクトリを削除。判定ログは学習データのため永続保持。

        この関数は例外を外へ送出しない（設計書 §10.7）。_dispatcher は日付が変わった最初の周回で本関数を
        呼び、その後に last_cleanup_date を更新する。ここで OSError（listdir/rmtree の権限・I/O
        エラー等）を送出すると last_cleanup_date 更新前に _dispatcher が死に、_supervise が
        再起動しても last_cleanup_date はローカル変数ゆえ None に戻るため、毎起動で同じ cleanup を
        叩いては即死する無限再起動（タスクを 1 件も捌けないライブロック）に陥る。listdir 全体と
        各エントリ削除を try で囲み、失敗はログのみで飲み込んで周回を前進させる。
        """
        retention = self.config["cleanup"]["retention_days"]
        threshold = datetime.date.today() - datetime.timedelta(days=retention)

        done_dir = f"{self.task_dir}/done"
        if not os.path.exists(done_dir):
            return
        try:
            names = os.listdir(done_dir)
        except OSError:
            logger.exception("done アーカイブの走査に失敗（cleanup をスキップ）: %s", done_dir)
            return
        for name in names:
            try:
                if datetime.date.fromisoformat(name) < threshold:
                    shutil.rmtree(os.path.join(done_dir, name))
            except ValueError:
                pass  # 日付形式でない名前（想定外の混入）は対象外
            except OSError:
                # 個々のディレクトリ削除失敗（権限・使用中など）は握りつぶし次へ。
                # ここで送出すると dispatcher ライブロックの原因になる（本 docstring 参照）。
                logger.exception("アーカイブ削除に失敗（スキップ）: %s", name)

    # ── ユーティリティ ──

    async def _update_status(self, path: str, status: str, result: str = None, *,
                             extra: dict | None = None) -> str:
        """タスクファイルの status を更新する。completed/failed はアーカイブ。
        in_progress / completed / failed / pending_approval 遷移時に qu-e へタスクコンテキストを
        push する（§8.13）。

        qu-e への push は SSH（同期・接続タイムアウトまで待つ）を含むため、イベントループを
        凍結させないよう to_thread で別スレッド実行する（§10.7）。ファイル I/O は高速なローカル
        操作のためそのまま行う。

        extra は status と同時に書き込む追加フィールド（承認保留の completed_steps /
        held_approval_id / 凍結プランなど）。読み直した最新のタスクへマージするため、
        別経路の書込と競合しても status 以外の更新を取りこぼさない。

        戻り値はタスクファイルの最終所在（アーカイブ後は done/{日付}/ 配下）。完了・失敗
        通知に「結果の正本ファイルパス」を併記するために使う（§8.9）。
        """
        with open(path) as f:
            task = json.load(f)
        task["status"] = status
        task["updated_at"] = datetime.datetime.now().isoformat()
        if result:
            task["result"] = result
        if extra:
            task.update(extra)
        # 状態遷移の書換も原子書込に統一（§8.3 書込の原子性）。書込中クラッシュで壊れた task が
        # 本パスに残り、次回起動の予約回収スキャンを誤らせるのを防ぐ。
        atomic_write_json(path, task)

        # §8.13 タスクコンテキスト共有(qu-e へ SSH push)。SSH の同期ブロッキングを別スレッドへ逃がす。
        # pending_approval も push する。qu-e は終了系 status（completed/failed）でのみ workspace を
        # 掃除・監視解除するため、保留を「終了」と伝えると再投入前に成果物が消えうる。
        if status in ("in_progress", "completed", "failed", STATUS_PENDING_APPROVAL):
            await asyncio.to_thread(self._push_task_context, task)

        # completed/failed のファイルを done/{日付}/ に移動（ディレクトリ走査の肥大化防止）
        if status in ("completed", "failed"):
            # タスク別 deny 規則（§8.10f）は終端で掃除する（残すと無関係な後続に効かないが
            # ゴミとして蓄積する。掃除失敗は無害なため黙って続行）
            deny_dir = getattr(self, "task_deny_dir", None)
            if deny_dir and _APPROVAL_ID_RE.fullmatch(task.get("task_id") or ""):
                try:
                    os.remove(os.path.join(deny_dir, f"{task['task_id']}.json"))
                except OSError:
                    pass
            today = datetime.date.today().isoformat()
            done_dir = f"{self.task_dir}/done/{today}"
            os.makedirs(done_dir, exist_ok=True)
            dest = os.path.join(done_dir, os.path.basename(path))
            shutil.move(path, dest)
            return dest
        return path

    def _workspace_for(self, task_id: str) -> str:
        """タスク専用の作業ディレクトリ（MBP 上）の既定値。

        各タスクは `{workspace_base}/{task_id}` で実行され、qu-e はこの接頭辞で
        file_audit の変更パスを task_id に帰属させる（§8.13）。
        """
        base = self.config["task_context"].get("workspace_base", "/opt/taka-ma/work")
        return f"{base}/{task_id}"

    def _resolve_workspace(self, task: dict) -> str:
        """タスクの作業ディレクトリ（MBP 上）を解決する（§8.13 workspace の決定）。

        `repo:` で実開発リポジトリが明示指定されていればその絶対パス（会話側で検証済み・
        queue_item = {**task, ...} で伝播）、無ければ既定の `{workspace_base}/{task_id}`。
        """
        return task.get("workspace") or self._workspace_for(task["task_id"])

    def _push_task_context(self, task: dict):
        """タスクコンテキストを qu-e に SSH push する（§8.13）。

        in_progress / completed / failed の各遷移で同じ payload 形式で push し、
        受信側（qu-e）が status を見て保持・整理する。
        in_progress では同一 SSH コマンド内で先に workspace を mkdir し、qu-e が
        この push を受けて動的監視（§8.12）を登録する時点でのディレクトリ存在を
        順序として保証する（新規 clone 運用ではこの空ディレクトリへ worker が clone する）。
        SSH push 失敗時はログ記録のみで継続（task 処理は止めない）。
        """
        remote_dir = self.config["task_context"]["remote_dir"]
        task_id = task["task_id"]
        workspace = self._resolve_workspace(task)
        payload = json.dumps({
            "task_id": task_id,
            "command": task["command"],
            "channel_id": task.get("channel_id", ""),
            "team_id": task.get("team_id", ""),          # §8.3: file_audit アラートの応答先WS特定用
            "thread_ts": task.get("thread_ts"),          # §8.12: 実行中タスクへの file_audit アラートを同一スレッドへ Thread 返信させる
            "status": task["status"],
            "workspace": workspace,                      # §8.13: パス→task_id 帰属・動的監視の登録先
        }, ensure_ascii=False)
        mkdir_ws = (f"mkdir -p {shlex.quote(workspace)} && "
                    if task["status"] == "in_progress" else "")
        try:
            self.process_mgr.run_ssh_command(
                f"{mkdir_ws}mkdir -p {remote_dir} && cat > {remote_dir}/{task_id}.json",
                stdin_text=payload)
        except Exception:
            logger.exception("task_context push 失敗: task_id=%s", task_id)


# 起動エントリは orchestrator/__main__.py（`python -m orchestrator`）に置く。
# パッケージ実行（-m）では __init__.py の __name__ は "orchestrator" になり、
# ここに __main__ ブロックを書いても起動しないため、__main__.py へ分離している。
