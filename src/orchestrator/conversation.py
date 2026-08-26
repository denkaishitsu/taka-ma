"""会話フロントエンド — sa-ru の脳で人間と会話し、意図が固まったら実行へ移譲する。

設計書 §2.1（sa-ru 役割）/ §8.3（u-zu → sa-ru: 会話投入 → 確定要約 → 実行タスク）。

データフロー:
  u-zu が会話キュー（CONVERSATION_DIR）に発話を書く
    → ConversationManager.handle_message が脳 LLM（sa-ru.model）を呼ぶ
      ├─ ready=false → Slack に会話返信（足りない前提を確かめる）
      └─ ready=true  → 構造化要約 + 着手/やり直すボタンを提示し、確認レコードを pending で作成
  人間が「着手」を押す → u-zu が確認レコードを confirmed に更新
    → ConversationManager.create_exec_task が確定タスク（status=init）を生成
      → 既存 dispatcher が拾い、ya-ta 分解 → worker 実行（以降は現行フロー無改変）

「締めワード」は文字列マッチで列挙しない。脳 LLM が各発話を「会話継続 / 今すぐ実行」に分類するため、
言い回し（実行 / やれ / Go / Do it …）に依存しない。`/taka-ma-go`（force_ready）は LLM 判定を待たず
直近会話を要約して締める明示エスケープ。
"""

import datetime
import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from pathlib import Path

from ai_gateway.classifier import InvalidModelError
from ai_gateway.llm import (
    GenerationProgress,
    OllamaConnectionError,
    OllamaTimeoutError,
    extract_json,
    repair_json_escapes,
    run_ollama,
)
from orchestrator import contract as contract_rules
from orchestrator import intent_store
from orchestrator.file_queue import atomic_write_json

logger = logging.getLogger("sa-ru.conversation")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 会話へ還流するタスク結果の最大文字数。会話文脈用の要約であり、全文は結果ファイル
# （併記パス）が正本のため切っても情報は失われない（設計書 §8.9「会話への還流」）。
RESULT_REFLOW_MAX_CHARS = 2000

# `repo:` 明示指定（§8.13 実開発リポジトリ）。`:モデル名` と同じく脳 LLM の要約では
# 消えるため、要約対象の生文から抽出する。直前が非空白（URL の `/repo:tag` 等の埋め込み）
# のものはトークンとして扱わない（誤マッチで無関係な発話を差し戻さないため）。
_REPO_TOKEN_RE = re.compile(r"(?<!\S)repo:(\S+)")
# 受け付ける workspace パス。SSH コマンド文字列・worker の cwd に乗るため、
# 絶対パス・安全文字（英数 . _ - /）のみに制限する（§8.13 repo: パスの検証、fail-closed）。
_SAFE_WORKSPACE_RE = re.compile(r"\A/[A-Za-z0-9._/\-]+\Z")
# 脳 LLM 応答契約 {reply, ready, summary}（§8.3）のキーが生テキストへ混じっているかの検出。
# JSONDecodeError フォールバックに来る出力は定義上パース不能（切断・多重 JSON 等の壊れ形）
# なので json.loads では判定できず、「引用符付き契約キー + コロン」の形で検出する。壊れ形には
# Python dict 風のシングルクォート（'reply':）もあり得るため引用符は "/' の両方を受ける。
# 人向け返信に内部 JSON を 1 断片も見せないため、キー 1 つの出現でも縮退する（安全側・
# #taka-ma/142。実害は 2026-08-10 Slack DM インシデント F2 の生 JSON 漏出）。
_CONTRACT_KEY_RE = re.compile(r"""["'](?:reply|ready|summary)["']\s*:""")
# 自然文のリポジトリ指定（§8.13 / #143）。インシデント発端の `#Repo ~/DevDev/...` のように、
# 人間は `repo:` 記法ではなくマーカー語＋パスで指定する現実がある（2026-08-10 インシデント
# 根本原因 1）。マーカー語（repo / repository / リポジトリ）に区切り（: ： = は を の・空白）を
# 挟んで続く `/...` または `~/...` パスを候補として拾い、`repo:` 記法と同一の検証・展開に通す。
# 直前が英数・`-`・`/` のもの（URL の `.../repo:tag` や `my-repo` 等の埋め込み）は誤検知する
# ためマーカーとして扱わない。
_REPO_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_/\-])#?(?:repo(?:sitory)?|リポジトリ)"
    r"(?:\s*(?:[:：=＝]|は|を|の)\s*|\s+)"
    r"((?:~/|/)[A-Za-z0-9._\-][A-Za-z0-9._/\-]*)",
    re.IGNORECASE)


# probe の許可値（§8.3。LLM が選べるのは種別のみ・応答本文はコードが実レコード/実出力から
# 組み立てる。許可リスト外の値は無効＝LLM 出力を任意動作へ接続しない）
_PROBE_KINDS = ("repo_status", "task_status")

# 「実行系」とみなすタスク status（§8.3 進行状況発言のグラウンディング。orchestrator の
# 遷移 init→accepted→in_progress と承認保留 STATUS_PENDING_APPROVAL に一致させる —
# `grep -n "pending_approval" src/orchestrator/__init__.py src/orchestrator/conversation.py`
# で両側の一致を確認する。__init__ からの import は循環になるため値を重ねて持つ）
_ACTIVE_TASK_STATUSES = {"init", "accepted", "in_progress", "pending_approval"}


class InvalidWorkspaceError(ValueError):
    """`repo:` 指定が検証を通らない（相対パス・危険文字・`..` 等）ときのエラー。"""


class ConversationManager:
    """会話セッションの保持・脳 LLM 呼び出し・要約提示・確定タスク生成を担う。

    セッション履歴はターン追記のたびにディスクへ原子書込で永続化する（設計書 §8.3
    「会話セッション履歴の永続化」）。sa-ru 再起動・TTL 経過後も次の発話時にファイルから
    文脈を回復するため、会話の記憶を失わない。
    """

    def __init__(self, config, slack_notifier, task_dir: str, classifier=None,
                 plan_service=None, process_mgr=None, canceller=None):
        """会話マネージャを構築する。

        Args:
            config: sa-ru の脳モデル・会話タイムアウト・確定タスク/着手確認の出力先を含む設定。
            slack_notifier: 要約や着手確認ボタンを人間へ提示する通知手段。
            task_dir: 会話から確定したタスクを書き出す先（dispatcher が走査して実行に回す）。
            classifier: `:モデル名` 明示指定の抽出に使う TaskClassifier（設計書「ユーザーモデル
                指定」）。脳 LLM の要約はユーザーの生文を言い換えるため `:opus` 等の記法が消える。
                要約対象の生文（`msg["text"]`）から先に抽出し、要約とは別経路でタスクへ伝える
                （parse_model 自体は既存だったが呼び出し元が無く未配線だった。実機検証で
                `:opus` 指定が効かないことを確認・是正）。
            plan_service: 計画プレビューの生成・整形・訂正（orchestrator.plan.PlanService）。
                意図が固まった時点でここで分解まで済ませ、計画を提示して承認を取る
                （設計書 §8.10b 計画確認ゲート / §10.2.1 計画プレビュー契約）。
            process_mgr: 確認系質問への実測応答（§8.3 probe）で読み取り専用コマンドを
                workspace に SSH 実行する手段（RemoteProcessManager）。None なら probe は
                「実行手段なし」の事実を返信する（宣言は返さない）。
            canceller: 停止命令の実行本体（設計書 §8.10d）。Orchestrator.request_cancel を
                注入する（発話 msg を受け、同一会話面の計画/タスクを停止して停止結果 dict を
                返す同期呼び出し。会話処理は to_thread 上のため同期でよい）。None なら
                制御判定を行わない（単体テスト・段階導入用）。
        """
        self.config = config
        self.process_mgr = process_mgr
        # probe の対象 workspace の既定 base（Orchestrator._workspace_for と同じ解決規則）
        self.workspace_base = config.get("task_context", {}).get(
            "workspace_base", "/opt/taka-ma/work")
        # conversation_id → 直近タスクの workspace（確認系質問の実測対象。§8.3 probe）。
        # セッション永続化ファイルにも保存し、再起動をまたいで保持する
        self._last_workspace: dict[str, str] = {}
        self.plan_service = plan_service
        self.model = config["sa-ru"]["model"]              # 脳モデル（sa-ru.yaml が正本）
        # 接続先・会話タイムアウトは config を唯一の源にする（設計書 §8.4。コード既定値なし）
        self.ollama_host = config["sa-ru"]["ollama_host"]
        self.timeout = config["sa-ru"]["converse_timeout_sec"]
        # 会話は応答速度優先で思考を無効化できる（None=モデル既定・設計書 §8.4）。
        # 実測: qwen3.6 の思考 1400 トークン/30 秒 → think=false で 26 トークン/0.9 秒
        self.think = config["sa-ru"].get("llm_think")
        self.slack = slack_notifier
        self.task_dir = task_dir                            # 確定タスクの書き出し先（dispatcher が走査）
        self.classifier = classifier
        self.canceller = canceller                          # 停止命令の実行本体（§8.10d）
        self.confirm_dir = config["exec_confirm"]["dir"]    # 着手確認レコードの dir
        os.makedirs(self.confirm_dir, exist_ok=True)
        # 会話プロンプトは静的なので起動時に 1 度だけ読む（毎ターンの disk I/O を避ける）
        self._prompt_template = (PROMPTS_DIR / "converse.md").read_text()
        # 進行主張の選別プロンプト（§8.3 グラウンディングの安全網。言語理解は LLM が担い、
        # 語列挙の正規表現を使わない — 2026-08-25 E2E FAIL の是正）
        self._progress_claim_template = (PROMPTS_DIR / "progress_claim.md").read_text()
        # ready 再検査の選別プロンプト（§8.3 細部質問の検品。対象・動作が特定できる依頼に
        # 「何を書くか」等の細部を聞き返して止まる取りこぼし — 2026-08-24 E2E 実測 — の安全網）
        self._ready_recheck_template = (PROMPTS_DIR / "ready_recheck.md").read_text()
        # TTL はセッションの「メモリからのアンロード」期限。永続化ファイルは残るため
        # TTL 経過・再起動後も次の発話時に文脈を回復できる（設計書 §8.3 永続化）。
        # sa-ru.yaml を唯一の供給元とする（コード既定値なし。sessions_dir と流儀を揃える）
        self.session_ttl_sec = config["conversation"]["session_ttl_sec"]
        # セッション永続化の保存先（conversation_id 単位の JSON・原子書込）。
        # sa-ru.yaml を唯一の供給元とする（コード側に既定値を置くと供給元が二重になる）
        self.sessions_dir = config["conversation"]["sessions_dir"]
        os.makedirs(self.sessions_dir, exist_ok=True)
        # 脳 LLM ビューの二窓幅（§8.3 (C) head+tail）。永続化ファイルは全履歴を保持し、
        # プロンプトへ載せる分だけ「冒頭 + 直近」に丸める。sa-ru.yaml が唯一の供給元
        self.history_head_turns = config["conversation"]["history_head_turns"]
        self.history_tail_turns = config["conversation"]["history_tail_turns"]
        # worker ホスト（MBP）の HOME 絶対パス（sa-ru.yaml task_context.worker_home が唯一の
        # 供給元）。`~/` 前置きのリポジトリ指定をここで絶対パスへ展開する（§8.13 / #143。
        # sa-ru は MBP 側ホームを自力解決できない）。未設定なら ~ 指定は従来どおり差し戻す
        # （fail-closed。誤ったホームで展開して無関係パスへ書くより安全側）
        self.worker_home = (config.get("task_context") or {}).get("worker_home")
        # conversation_id → [{"role": "user"|"assistant", "text": str}, ...]
        self.sessions: dict[str, list[dict]] = {}
        # conversation_id → 検証済み workspace 絶対パス。repo: / 自然文指定はセッション単位で
        # 持続させる（§8.13 / #143。抽出を ready を発火させた最終発話に限ると「冒頭で指定 →
        # 後の発話で着手」という自然な流れで指定が落ちる。2026-08-14 Wave1-B 検証 2）。
        # セッション永続化ファイルにも保存し、再起動・TTL 経過後も失わない
        self.session_workspace: dict[str, str] = {}
        # conversation_id → 同一会話から直前に生成した確定タスクの task_id（§8.3 (C)）。
        # 次の確定タスクの parent_task_id になる。セッション永続化ファイルにも保存し、
        # 再起動をまたいで親子チェーンを保つ
        self._last_task_id: dict[str, str] = {}
        # 会話⇄実行の受け渡し契約（§8.10f）。`contract:` 設定ブロックの有無で有効化する
        # （段階導入。未構成環境・既存テストでは従来動作のまま）。intents_dir は依頼の寿命
        # （goal_status）を持つ intent レコードの保存先
        contract_conf = config.get("contract") or {}
        self._contract_enabled = bool(contract_conf)
        self.intents_dir = contract_conf.get("intents_dir")
        self._contract_template = (
            (PROMPTS_DIR / "contract.md").read_text() if self._contract_enabled else None)
        # conversation_id → 直近タスクの失敗原因コード列（連続分のみ・§8.10f 反復停止判定）。
        # セッション永続化ファイルにも保存し、再起動をまたいで反復を見失わない
        self._failure_causes: dict[str, list[str]] = {}
        # conversation_id → 最終アクセス時刻（monotonic 秒）。エビクション判定に使う
        self._last_seen: dict[str, float] = {}
        # セッション辞書の排他。会話処理は to_thread（別スレッド）、タスク結果の還流
        # （append_task_result）はイベントループ側スレッドから呼ばれ、同一セッションを
        # 同時に触り得るため（設計書 §8.9「会話への還流」）
        self._sessions_lock = threading.Lock()

    def _causes(self) -> dict:
        """失敗原因コード表（conversation_id → 連続コード列・§8.10f）を返す遅延アクセサ。

        テストは __new__ で __init__ を跳ばして個体を作るため、属性の直接参照は
        AttributeError になる。__dict__.setdefault で常に存在を保証する。
        """
        return self.__dict__.setdefault("_failure_causes", {})

    def _awaiting_map(self) -> dict:
        """返答待ちフラグ表（conversation_id → bool・§8.3 (C) 能動昇格）の遅延アクセサ。

        _causes と同じ理由（__new__ で作られるテスト個体）で __dict__.setdefault を使う。
        """
        return self.__dict__.setdefault("_awaiting_reply", {})

    def _set_awaiting(self, cid: str, awaiting: bool):
        """「taka-ma がユーザーの返答を待っているか」を更新し、セッションと一緒に永続化する。

        §8.3 (C) 返答待ちスレッドの能動昇格。u-zu がセッション永続化ファイルの
        awaiting_reply を読み、true のスレッドではメンション無しの返信を能動投入する。
        会話面へ質問・確認を送るたび true、待ちが解けた遷移（着手・完了還流・中止決着）で
        false にする。ここは書く側の唯一の口（判定の権威は sa-ru の会話状態）。
        """
        if not cid:
            return
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            self._awaiting_map()[cid] = awaiting
            self._persist_session(cid, history)

    # ── セッション永続化（設計書 §8.3「会話セッション履歴の永続化」） ──

    def _session_path(self, cid: str) -> str:
        """conversation_id をファイル名安全な形にして永続化パスを返す。"""
        return os.path.join(self.sessions_dir, re.sub(r"[^0-9A-Za-z._-]", "_", cid) + ".json")

    def _load_or_create_session(self, cid: str) -> list[dict]:
        """メモリ上のセッションを返す。無ければ永続化ファイルから回復し、それも無ければ新規。

        呼び出し側で _sessions_lock を保持していること。
        """
        history = self.sessions.get(cid)
        if history is not None:
            return history
        path = self._session_path(cid)
        history = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                history = data.get("turns", [])
                # 直近タスクの workspace も回復する（§8.3 probe。再起動後の確認系質問に
                # 「workspace 不明」で答えないため。メモリ上の値が新しい可能性があるため上書きしない）
                if data.get("last_workspace") and cid not in self._last_workspace:
                    self._last_workspace[cid] = data["last_workspace"]
                # workspace 指定もセッションの記憶として回復する（§8.13 / #143。
                # 再起動・TTL 経過で「冒頭のリポジトリ指定」を失わない）
                if data.get("workspace"):
                    self.session_workspace[cid] = data["workspace"]
                # 直近確定タスクも回復する（§8.3 (C)。再起動後の確定タスクにも
                # parent_task_id を継がせる。メモリ上の値が新しい可能性があるため上書きしない）
                if data.get("last_task_id") and cid not in self._last_task_id:
                    self._last_task_id[cid] = data["last_task_id"]
                # 失敗原因コード列も回復する（§8.10f 反復停止。再起動で反復を見失わない）
                if data.get("failure_causes") and cid not in self._causes():
                    self._causes()[cid] = list(data["failure_causes"])
                # 返答待ちフラグも回復する（§8.3 (C) 能動昇格。回復しないと次の永続化で
                # 既定 False に巻き戻り、再起動を跨いだ返答待ちが passive へ落ちる）
                if "awaiting_reply" in data and cid not in self._awaiting_map():
                    self._awaiting_map()[cid] = bool(data["awaiting_reply"])
            except (OSError, json.JSONDecodeError, AttributeError):
                # 壊れた永続化ファイルで会話全体を止めない。新規セッションとして進める
                logger.exception("会話セッションの読込失敗（新規で継続）: %s", path)
                history = []
        self.sessions[cid] = history
        return history

    def _persist_session(self, cid: str, history: list[dict]):
        """セッションを原子書込で永続化する。失敗しても会話処理本体は止めない。"""
        try:
            atomic_write_json(self._session_path(cid), {
                "conversation_id": cid,
                "turns": history,
                "last_workspace": self._last_workspace.get(cid),  # §8.3 probe の実測対象
                # 検証済み workspace 指定（§8.13 / #143）。会話ターンと同じ寿命で持続させる
                "workspace": self.session_workspace.get(cid),
                # 同一会話の直近確定タスク（§8.3 (C)。次のタスクの parent_task_id になる）
                "last_task_id": self._last_task_id.get(cid),
                # 直近タスクの失敗原因コード列（連続分・§8.10f 反復停止判定）
                "failure_causes": self._causes().get(cid),
                # taka-ma がユーザーの返答を待っているか（§8.3 (C)。u-zu が読み、true の
                # スレッドではメンション無しの返信を能動投入する）
                "awaiting_reply": bool(self._awaiting_map().get(cid)),
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        except OSError:
            logger.exception("会話セッションの永続化失敗（メモリ上は継続）: %s", cid)

    # ── 会話処理（会話ループから to_thread で呼ばれる：脳 LLM は同期ブロック） ──

    @staticmethod
    def parse_workspace(text: str, worker_home: str | None = None) -> tuple[str, str | None]:
        """生文から `repo:<絶対パス>` を抽出し、(除去後テキスト, workspace|None) を返す。

        `:モデル名` 指定（classifier.parse_model）より先に呼ぶこと。`repo:/path` の
        `:/path` 部分が parse_model の `:(\\S+)` に誤マッチして未登録モデル扱いになるため、
        先に取り除く必要がある。検証（§8.13 repo: パスの検証）に通らない指定は
        InvalidWorkspaceError で着手前に差し戻す（fail-closed）。

        worker_home: worker ホスト（MBP）の HOME 絶対パス。`~/` 前置きはこの値で展開して
        から検証する（§8.13 / #143。人間の指定は `~/DevDev/...` が現実形）。未指定（None）
        なら展開できないため従来どおり差し戻す。
        """
        matches = _REPO_TOKEN_RE.findall(text)
        if not matches:
            return text, None
        if len(set(matches)) > 1:
            raise InvalidWorkspaceError("repo: 指定が複数あります。1 つにしてください")
        workspace = matches[0].rstrip("/")
        if workspace.startswith("~"):
            if worker_home and (workspace == "~" or workspace.startswith("~/")):
                # worker ホスト側 HOME への展開（§8.13。展開後を通常の検証に通す）
                workspace = worker_home.rstrip("/") + workspace[1:]
            else:
                raise InvalidWorkspaceError(
                    "repo: は絶対パスで指定してください（~ は使えません。"
                    "例: repo:/Users/<user>/DevDev/xxx）")
        if not _SAFE_WORKSPACE_RE.match(workspace) or ".." in workspace.split("/"):
            raise InvalidWorkspaceError(
                "repo: のパスが不正です（絶対パス・英数と . _ - / のみ・.. 不可）")
        clean = _REPO_TOKEN_RE.sub("", text).strip()
        return clean, workspace

    @staticmethod
    def find_repo_mention(text: str) -> str | None:
        """自然文のリポジトリ指定（`#Repo ~/path`『リポジトリ: /path』等）のパス候補を返す。

        `repo:` 記法が無いときのフォールバック（§8.13 / #143。インシデント発端メッセージは
        `#Repo ~/DevDev/...` 形式で、記法抽出だけでは workspace に乗らなかった）。候補は
        呼び出し側で `repo:` 記法と同一の検証・~ 展開に通す。複数あれば最後（最新の言及）を採る。
        """
        matches = _REPO_MENTION_RE.findall(text)
        return matches[-1] if matches else None

    def _extract_workspace(self, text: str) -> tuple[str, str | None, str | None]:
        """発話 1 件から workspace 指定を抽出する。(repo: 除去後テキスト, workspace, 案内文言)。

        - `repo:` 記法の不正は InvalidWorkspaceError を送出（明示記法は fail-closed に差し戻す）
        - 自然文指定（find_repo_mention）が検証を通らない場合は例外にせず案内文言を返す
          （ヒューリスティック検出で会話を堰き止めない。repo: 記法での再指定を促す・#143）
        """
        clean, workspace = self.parse_workspace(text, worker_home=self.worker_home)
        if workspace is not None:
            return clean, workspace, None
        raw = self.find_repo_mention(text)
        if raw is None:
            return clean, None, None
        try:
            # 候補を repo: 記法に包み直し、記法指定と同一の検証・~ 展開に通す（規則の二重化防止）
            _, workspace = self.parse_workspace(f"repo:{raw}", worker_home=self.worker_home)
            return clean, workspace, None
        except InvalidWorkspaceError as e:
            return clean, None, (
                f"リポジトリ指定と思われる `{raw}` を workspace に設定できませんでした（{e}）。"
                "`repo:/絶対パス` 記法で指定し直してください。")

    def _history_view(self, history: list[dict]) -> list[dict]:
        """脳 LLM プロンプト用の二窓ビュー（冒頭 + 直近）を返す（§8.3 (C) head+tail）。

        永続化側は全ターン保持のまま、プロンプトへ載せる分だけをここで丸める。冒頭窓を
        必ず含めるのは、スレッドがどれだけ伸びても依頼の前提（冒頭プロンプト）を回答・
        確定要約の入力に残すため（2026-08-10 インシデント F3 の再発防止）。全量投入に
        しないのは、num_ctx 超過分が古い側から暗黙に切り捨てられ、長大スレッドで冒頭が
        消える＝F3 と同型の失敗に戻るため（二窓は上限サイズが決定的）。
        """
        head, tail = self.history_head_turns, self.history_tail_turns
        if len(history) <= head + tail:
            return list(history)
        omitted = len(history) - head - tail
        marker = {"role": "assistant", "text": f"（中略 {omitted} ターン）"}
        return history[:head] + [marker] + history[-tail:]

    def _record_passive_turn(self, msg: dict):
        """非メンション発話（passive）を既存セッションに限り履歴へ追記する（§8.3 (C)）。

        脳 LLM 呼び出し・Slack 返信はしない（bot が呼ばれていない発話に応答しない）。
        セッションが無ければ捨てる（無関係スレッドの発話を収集しない）。
        """
        cid = msg["conversation_id"]
        with self._sessions_lock:
            if cid not in self.sessions and not os.path.exists(self._session_path(cid)):
                return
            self._last_seen[cid] = time.monotonic()
            history = self._load_or_create_session(cid)
            history.append({"role": "user", "text": msg["text"]})
            self._persist_session(cid, history)

    def _evict_idle_sessions(self, now: float):
        """TTL を超えて使われていないセッションをメモリからアンロードする。

        永続化ファイルは削除しない（時間経過で記憶を失わない・設計書 §8.3）。
        """
        stale = [c for c, seen in self._last_seen.items() if now - seen > self.session_ttl_sec]
        for c in stale:
            self.sessions.pop(c, None)
            self._last_seen.pop(c, None)
            # workspace も同時にアンロードする（永続化ファイルに残るため次の発話で回復する）
            self.session_workspace.pop(c, None)

    def handle_message(self, msg: dict, progress: GenerationProgress | None = None):
        """1 件の発話を処理する。会話継続なら返信、意図が固まれば着手確認を提示する。

        progress はハートビート進捗通知（§10.8）へ生成トークン数を届ける共有ホルダー
        （呼び出し元 _run_with_heartbeat が渡す）。
        """
        cid = msg["conversation_id"]
        # 脳 LLM 呼び出し開始のタイミングを可視化する(投稿受信〜着手確認提示の所要時間を
        # 計測できるようにする。実運用フィードバックを受けて追加）。
        logger.info("会話メッセージ処理開始: conversation_id=%s", cid)

        # 非メンション発話（チャンネルスレッドの人どうしの返信）は文脈としての追記のみ
        # （§8.3 (C) passive）。制御判定・訂正解釈・脳 LLM のどれにも掛けない
        if msg.get("passive"):
            self._record_passive_turn(msg)
            return

        # 停止命令（制御コマンド）は訂正解釈・脳 LLM より前に判定し、承認ゲートに掛けず
        # 即時実行する（設計書 §8.10d）。提示中の計画がある状態での「中止」は計画への訂正
        # ではなく破棄命令のため、訂正解釈より先でなければならない。/taka-ma-go は明示の
        # 実行エスケープなので制御判定に掛けない。
        if (not msg.get("force_ready") and self.canceller is not None
                and self.classifier is not None
                and self.classifier.classify_control(msg["text"]) == "cancel"):
            self._handle_cancel(msg)
            return

        # 計画確認中（pending の確認レコードがある）なら、発話をまず「提示済みプランへの訂正」
        # として解釈する（設計書 §8.3 訂正経路 / §10.2.1）。訂正と解釈できなければ通常の会話へ
        # 落とす（人間がプランを捨てて話を続ける経路を塞がない）。/taka-ma-go は締め直しの
        # 明示エスケープなので訂正解釈に回さない。
        if not msg.get("force_ready") and self._handle_correction(msg, progress=progress):
            return

        # workspace 指定（repo: 記法・自然文）は発話ごとに抽出してセッションへ持続させる
        # （§8.13 / #143。ready を発火させた最終発話だけを見る方式では「冒頭で指定 → 後の
        # 発話で着手」の流れで指定が落ちる）。`repo:` 記法の不正はこの時点で差し戻し、
        # 会話・着手へ進めない（fail-closed。従来は ready 時のみ検証していたが、指摘が早い
        # ほど人間の修正コストが低い）。自然文候補の不正は案内のみで会話は続ける。
        try:
            text_wo_repo, workspace, guidance = self._extract_workspace(msg["text"])
        except InvalidWorkspaceError as e:
            # 差し戻し = 指定し直しの返答を待つ（§8.3 (C) 能動昇格）
            self._set_awaiting(cid, True)
            self.slack.notify(
                str(e), msg.get("channel_id"),
                team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
            return
        if guidance:
            self.slack.notify(
                guidance, msg.get("channel_id"),
                team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))

        now = time.monotonic()
        with self._sessions_lock:
            self._evict_idle_sessions(now)
            self._last_seen[cid] = now
            history = self._load_or_create_session(cid)
            if workspace is not None:
                # 最後に指定された値が勝つ（同一セッションで指定し直せる）。永続化は
                # 直後の _persist_session が turns と一緒に書く
                if self.session_workspace.get(cid) != workspace:
                    # 場所の指定が変わった＝失敗の前提状況が変わった。反復停止判定
                    # （§8.10f）の連続カウントをリセットし、新指定での再試行を塞がない
                    self._causes().pop(cid, None)
                self.session_workspace[cid] = workspace
            history.append({"role": "user", "text": msg["text"]})
            self._persist_session(cid, history)
            # 脳 LLM 呼び出し（数十秒）中はロックを持たない。以降このターンの入力は
            # 二窓ビュー（冒頭 + 直近・§8.3 (C)）のスナップショットとして扱い、追記時に
            # 再ロックする。永続化側は丸めない（全履歴保持）
            history_snapshot = self._history_view(history)

        if msg.get("force_ready"):
            # /taka-ma-go: LLM 判定を待たず要約させて強制的に締める
            result = self._invoke_llm(history_snapshot, force=True, progress=progress)
            result["ready"] = True
        else:
            result = self._invoke_llm(history_snapshot, force=False, progress=progress)
            # ready 再検査（§8.3 細部質問の検品）: 会話継続の返信が「細部への質問」なら
            # 1 回だけ再判定する。対象・動作が特定できる依頼に「何を書くか」を聞き返して
            # 止まる取りこぼし（2026-08-24 E2E 実測。プロンプト規則のみでは残存 11%）の
            # コード側安全網。再判定が ready=true+summary を返したときだけ差し替え、
            # それ以外（依然質問・エラー）は元の応答を使う（二重生成のブレを持ち込まない）
            if (not result.get("ready") and not result.get("probe")
                    and not result.get("error") and result.get("reply")):
                verdict = self._recheck_detail_question(
                    msg["text"], result["reply"], progress=progress)
                if verdict == "detail":
                    logger.warning(
                        "ready 再検査: 細部質問を検出し再判定します（reply=%.80s）",
                        result["reply"])
                    retry = self._invoke_llm(history_snapshot, force=False,
                                             progress=progress, detail_retry=True)
                    if retry.get("ready") and retry.get("summary"):
                        result = retry

        # 確認系質問（リポジトリ実状態・進行状況）には宣言でなく実測を返す（§8.3 probe）。
        # 脳 LLM は「どの実測が要るか」の選別のみを担い、返信本文はコマンド実出力・
        # 実レコードから機械的に組み立てる（§8.9 と同じ規律）
        if not result.get("ready") and result.get("probe"):
            if result["probe"] == "task_status":
                self._answer_task_status(msg)
            else:
                self._answer_probe(msg)
            return

        if result.get("ready") and result.get("summary"):
            summary = result["summary"]
            self._append_turn(cid, "assistant", summary)
            # workspace はセッション持続値を採る（このターンの指定は上で反映済み。§8.13 / #143）。
            # `:opus` 等の明示モデル指定は要約（脳 LLM の言い換え）には残らないため、要約対象の
            # 生文から直接抽出する（設計書「ユーザーモデル指定」）。repo: を先に除去した
            # text_wo_repo を使う（`:/path` が parse_model に未登録モデルとして誤検出されるため）。
            with self._sessions_lock:
                workspace = self.session_workspace.get(cid)
            models: list[str] = []
            if self.classifier is not None:
                try:
                    _, models = self.classifier.parse_model(text_wo_repo)
                except InvalidModelError as e:
                    # モデル指定の差し戻し = 指定し直しの返答を待つ（§8.3 (C) 能動昇格）
                    self._set_awaiting(cid, True)
                    self.slack.notify(
                        str(e), msg.get("channel_id"),
                        team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                    return

            # ── 会話⇄実行の受け渡し契約（§8.10f）。`contract:` 未構成なら従来動作 ──
            contract_data = None
            if getattr(self, "_contract_enabled", False):
                # 反復停止ゲート: 同一原因コードで 2 回連続失敗している会話には再計画を
                # 提示せず、原因と必要な入力を平文で返す。/taka-ma-go（force_ready）は
                # 「解消済み・続行せよ」の明示エスケープとしてゲートを通す
                with self._sessions_lock:
                    causes = list(self._causes().get(cid) or [])
                if not msg.get("force_ready") and contract_rules.is_repeated_cause(causes):
                    cause = causes[-1]
                    history_text = " → ".join(causes)
                    # 不足入力の質問 = 返答待ち（§8.3 (C) 能動昇格）
                    self._set_awaiting(cid, True)
                    self.slack.notify(
                        f"連続 2 回失敗しているため、再計画を提示しません（原因: {history_text}）。\n"
                        f"必要な入力: {contract_rules.required_input_for(cause)}\n"
                        "解消済みで続行する場合は `/taka-ma-go` で明示してください。",
                        msg.get("channel_id"),
                        team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                    return
                contract_data = self._build_contract(cid, summary, progress=progress)
                if contract_data is None:
                    # 契約が確定できない依頼は実行へ進めない（fail-closed・§8.10f）。
                    # 言い直しの返答を待つ（§8.3 (C) 能動昇格）
                    self._set_awaiting(cid, True)
                    self.slack.notify(
                        "実行契約を確定できませんでした（命令・拘束・完了条件の抽出に失敗）。"
                        "作業リポジトリ（`repo:/絶対パス`）と、何ができたら完了かを明示して"
                        "言い直してください。",
                        msg.get("channel_id"),
                        team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                    return
                # workspace: 記法・自然文の明示指定（セッション持続値）が最優先。無ければ
                # 契約化パスの脳判定（会話全文脈からの特定・§8.13）を同一の検証・~ 展開に通す
                if workspace is None and contract_data.get("workspace"):
                    try:
                        _, proposed = self.parse_workspace(
                            f"repo:{contract_data['workspace']}",
                            worker_home=self.worker_home)
                        workspace = proposed
                        with self._sessions_lock:
                            self.session_workspace[cid] = workspace
                            history = self._load_or_create_session(cid)
                            self._persist_session(cid, history)
                    except InvalidWorkspaceError:
                        pass  # 提案が検証を通らなければ未解決のまま（下の fail-closed へ）
                if contract_data.get("needs_repo") and workspace is None:
                    # 着手前ブロック（§8.10f）: 実リポジトリを要するのに場所が未解決の
                    # 依頼に着手ボタンを出さない。空作業場で走ってから気づく構造を廃する。
                    # リポジトリ指定の返答を待つ（§8.3 (C) 能動昇格）
                    self._set_awaiting(cid, True)
                    self.slack.notify(
                        "この依頼は実リポジトリでの作業が必要ですが、作業場所が未解決です。"
                        "`repo:/絶対パス` で指定してください"
                        "（使い捨ての空作業場でよい場合はその旨を発話してください）。",
                        msg.get("channel_id"),
                        team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                    return
            self._present_summary(msg, summary, models, workspace,
                                  contract=contract_data, progress=progress)
        else:
            reply = result.get("reply") or "（応答を生成できませんでした。もう一度お願いします）"
            # 進行状態の主張（「作業中です」等）は宣言のまま返さない（§8.3 グラウンディングの
            # 安全網）。主張の有無の理解は LLM の 1 問選別（_claims_progress・言語非依存）が
            # 担い、判定はタスクキューの実レコードのみ。主張ありで実行系タスクが無ければ
            # 脳の返信は使わず実測文言へ差し替え、あれば実測を併記する。エラー由来の定型文は
            # 対象外（主張を含まない・選別呼び出しのコストも掛けない）
            grounded_replaced = False
            if not result.get("error") and self._claims_progress(reply, progress=progress):
                running = self._running_tasks(cid)
                if running:
                    reply = reply + "\n\n" + self._task_status_text(msg)
                else:
                    reply = self._task_status_text(msg)
                    grounded_replaced = True
            # エラー由来の返信（タイムアウト・接続失敗等）はシステムメッセージであり会話では
            # ないため履歴に残さない。残すと後続ターンで脳がエラー文言を会話文脈として
            # オウム返しする（実機で再現・2026-07-14）。
            if not result.get("error"):
                self._append_turn(cid, "assistant", reply)
            # 会話継続 = ユーザーの次の発話を待つ（§8.3 (C) 能動昇格。エラー由来も
            # 言い直しを求めており返答待ち）
            self._set_awaiting(cid, True)
            self.slack.notify(
                reply, msg.get("channel_id"),
                team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
            # 実測差し替え時は着手待ちの計画をボタン付きで再提示する（probe 経路と同じ規律）
            if grounded_replaced:
                self._represent_pending_plan(msg)

    # probe で実行する読み取り専用コマンド（§8.3「確認系質問への実測応答」で固定列挙）。
    # 任意コマンド実行の入り口にしない（脳 LLM が選べるのは probe 種別のみ・コマンドはコード固定）
    _PROBE_COMMANDS = ("git -C {ws} remote -v",
                       "git -C {ws} rev-parse --abbrev-ref HEAD",
                       "ls -la {ws}")
    # probe 1 コマンドの SSH タイムアウト（秒）と、返信へ載せる 1 出力の上限文字数
    _PROBE_TIMEOUT_SEC = 30
    _PROBE_OUTPUT_MAX_CHARS = 1500

    def _answer_probe(self, msg: dict):
        """確認系質問に実行結果（実出力）で答える（§8.3 probe）。

        返信は必ず 1 メッセージ。実行できた場合はコマンドごとの rc・実出力、実行不能
        （workspace 不明・SSH 手段なし・SSH 不達）の場合はその事実とエラーを返す。
        いずれも脳 LLM の生成テキストは使わない（宣言反復＝2026-08-10 インシデント F2 の再発防止）。
        """
        cid = msg["conversation_id"]
        with self._sessions_lock:
            # _last_workspace は永続化ファイルからの遅延回復を経る（_load_or_create_session）
            self._load_or_create_session(cid)
            workspace = self._last_workspace.get(cid)
        if self.process_mgr is None:
            text = "実測確認を実行できません: SSH 実行手段が未構成です（sa-ru の構成異常）"
        elif not workspace:
            text = ("実測確認を実行できません: この会話で実行したタスクの workspace が"
                    "見つかりません。タスクを実行してから再度お尋ねください")
        else:
            ws = shlex.quote(workspace)
            lines = [f"実測結果（workspace: {workspace}）"]
            for template in self._PROBE_COMMANDS:
                cmd = template.format(ws=ws)
                rc, output = self.process_mgr.run_ssh_probe(
                    cmd, timeout=self._PROBE_TIMEOUT_SEC)
                out = (output or "").strip()
                if len(out) > self._PROBE_OUTPUT_MAX_CHARS:
                    out = out[:self._PROBE_OUTPUT_MAX_CHARS] + "\n…（以降略）"
                lines.append(f"$ {cmd} (rc={rc})")
                lines.append(out if out else "（出力なし）")
            text = "\n".join(lines)
        # 実測結果を会話履歴にも残す（後続ターンで脳が事実として参照できるようにする）
        self._append_turn(cid, "assistant", text)
        # 実測応答後もユーザーの続きの発話を待つ（§8.3 (C) 能動昇格）
        self._set_awaiting(cid, True)
        self.slack.notify(
            text, msg.get("channel_id"),
            team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))

    # ── 進行状況発言のグラウンディング（§8.3。宣言では返さず実測で返す） ──

    def _running_tasks(self, cid: str) -> list[dict]:
        """当該会話の実行系タスク（init/accepted/in_progress/pending_approval）を返す。

        タスクキュー（task_dir 直下）の実レコードだけを根拠にする（LLM 不使用・§8.3）。
        done/ failed/ へ退避済みの終端タスクは走査対象外（サブディレクトリは見ない）。
        読めないファイルはスキップする（進行確認のために会話を止めない）。
        """
        tasks: list[dict] = []
        try:
            names = os.listdir(self.task_dir)
        except OSError:
            return tasks
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.task_dir, name)) as f:
                    task = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if (task.get("conversation_id") == cid
                    and task.get("status") in _ACTIVE_TASK_STATUSES):
                tasks.append(task)
        return tasks

    def _task_status_text(self, msg: dict) -> str:
        """当該会話の実行系タスクの実測テキストを組み立てる（§8.3。LLM 不使用）。

        根拠はタスクキューの実レコードと着手待ちの計画（pending の確認レコード）のみ。
        task_status probe の応答本文と、進行主張の差し替え/併記の両方で使う。
        """
        running = self._running_tasks(msg["conversation_id"])
        if running:
            lines = [f"（実測）この会話の実行系タスク {len(running)} 件:"]
            lines += [f"- {t.get('task_id', '')[:8]}: {t.get('status')}" for t in running[:5]]
            return "\n".join(lines)
        text = "タスクは走っていません（実測: この会話の実行系タスク 0 件）。"
        pendings = self._pending_confirms(msg)
        if pendings:
            # 件数は実数（固定文言「1 件」が実態と食い違った 2026-08-26 E2E 検出の是正）。
            # ボタンは呼び出し側が _represent_pending_plan で手元に再提示する（文言だけの
            # 案内で上に流れたボタンを探させない・§8.10b と同じ規律）
            text += (f"\n着手待ちの計画が {len(pendings)} 件あります。"
                     "最新の計画を着手ボタン付きで再提示します。")
        return text

    def _represent_pending_plan(self, msg: dict):
        """最新の着手待ち計画を着手ボタン付きでその場に再提示する（§8.10b の再利用）。

        進行状況の実測応答が「着手待ちの計画あり」と告げるとき、ボタンの実体を同じ場所へ
        出す（提示済みメッセージはスレッド上方に流れており、文言だけの案内はボタンを
        探させる欠陥になる — 2026-08-26 E2E 検出）。exec_request_id は変えないため、
        どのメッセージのボタンを押しても同じ確認レコードを決着させる（訂正の再提示と同一）。
        """
        pending = self._pending_confirm(msg)
        if not pending:
            return
        _path, record = pending
        body = record.get("summary") or ""
        plan = record.get("plan")
        if plan and self.plan_service:
            body += "\n\n" + self.plan_service.render(plan)
        self.slack.send_plan_update(
            record["exec_request_id"], body,
            channel=msg.get("channel_id"), team_id=msg.get("team_id"),
            thread_ts=msg.get("thread_ts"))

    def _answer_task_status(self, msg: dict):
        """進行状況の質問に実測で答える（§8.3 probe="task_status"。宣言は返さない）。

        返信本文は _task_status_text（実レコードのみ）で組み立て、脳 LLM の生成テキストは
        使わない（repo_status probe・§8.9 と同じ規律）。実測結果は会話履歴にも残す。
        """
        cid = msg["conversation_id"]
        text = self._task_status_text(msg)
        self._append_turn(cid, "assistant", text)
        # 実測応答後もユーザーの続きの発話を待つ（§8.3 (C) 能動昇格）
        self._set_awaiting(cid, True)
        self.slack.notify(
            text, msg.get("channel_id"),
            team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
        # 実行系タスクが無く着手待ちの計画があるときは、ボタンの実体を手元に出す
        # （文言だけの案内でボタンを探させない・§8.10b）
        if not self._running_tasks(cid):
            self._represent_pending_plan(msg)

    def _claims_progress(self, reply: str, progress: GenerationProgress | None = None) -> bool:
        """返信が「進行中の作業がある」と主張しているかを脳 LLM の 1 問で選別する（§8.3）。

        言語理解は LLM の仕事であり、語列挙の正規表現は使わない（活用形・言い換え・英語を
        取りこぼした 2026-08-25 E2E FAIL の是正）。ここは選別のみで、事実判定（タスク実在）は
        _running_tasks が担う。選別不能（パース不能・LLM 不達）は素通し（従来動作）へ縮退し、
        warning で発生率を観測する — 選別失敗で返信を全置換する誤爆より安全側。
        """
        prompt = self._progress_claim_template.replace("{reply}", reply)
        try:
            stdout = run_ollama(self.model, prompt, timeout=self.timeout,
                                host=self.ollama_host, think=self.think,
                                progress=progress)
            try:
                parsed = json.loads(extract_json(stdout))
            except json.JSONDecodeError:
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
            return parsed.get("claims_progress") is True
        except (json.JSONDecodeError, OllamaTimeoutError, OllamaConnectionError) as e:
            logger.warning("進行主張の選別に失敗（素通しへ縮退）: %s", e)
            return False

    def _recheck_detail_question(self, message_text: str, reply: str,
                                 progress: GenerationProgress | None = None) -> str:
        """会話継続の返信が「細部への質問」かを脳 LLM の 1 問で選別する（§8.3 細部質問の検品）。

        対象・動作が特定できる依頼に「何を書くか」等の細部を聞き返して止まる取りこぼし
        （2026-08-24 E2E で実測・プロンプト規則のみでは残存 11% を分離実測）の安全網。
        言語理解は LLM の仕事であり、語列挙の正規表現・記号判定は使わない（質問形の
        判定さえ言語・文体依存のため置かない。「〜ください。」で終わる聞き返しを実測）。
        返り値は "detail" / "essential" / "other"。選別不能（パース不能・LLM 不達）は
        "other"（素通し＝従来動作）へ縮退し、warning で発生率を観測する。
        """
        prompt = (self._ready_recheck_template
                  .replace("{message}", message_text).replace("{reply}", reply))
        try:
            stdout = run_ollama(self.model, prompt, timeout=self.timeout,
                                host=self.ollama_host, think=self.think,
                                progress=progress)
            try:
                parsed = json.loads(extract_json(stdout))
            except json.JSONDecodeError:
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
            # JSON としては妥当でも object でない出力（配列・文字列等）は契約外 → other
            verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
            return verdict if verdict in ("detail", "essential", "other") else "other"
        except (json.JSONDecodeError, OllamaTimeoutError, OllamaConnectionError) as e:
            logger.warning("ready 再検査の選別に失敗（素通しへ縮退）: %s", e)
            return "other"

    def _set_last_workspace(self, cid: str, workspace: str):
        """会話の「直近タスクの workspace」を記録し、セッションと一緒に永続化する（§8.3 probe）。"""
        if not cid or not workspace:
            return
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            self._last_workspace[cid] = workspace
            self._persist_session(cid, history)

    # ── 停止命令の即時実行（設計書 §8.10d） ──

    def _handle_cancel(self, msg: dict):
        """停止命令を即時実行し、対象の特定結果と停止一覧のみを 1 メッセージで返す（§8.10d）。

        承認ゲート（着手/やり直す）は経由しない。停止の実体（計画破棄・タスク停止）は注入された
        canceller（Orchestrator.request_cancel）へ委譲し、ここは報告整形と会話履歴への追記
        （後続会話の文脈維持）のみを担う。失敗は原因を明示して返す（無言ドロップ防止・§8.3 の
        エラーハンドリングと同じ規律。原因不明の包括表現は使わない）。
        """
        cid = msg["conversation_id"]
        logger.info("停止命令を検知: conversation_id=%s", cid)
        error = False
        try:
            report = self.canceller(msg)
            reply = self._format_cancel_report(report)
        except Exception as e:
            logger.exception("停止命令の実行に失敗")
            reply = f"中止処理に失敗しました（{type(e).__name__}: {e}）。"
            error = True
        self._append_turn(cid, "user", msg["text"])
        # エラー文言はシステムメッセージであり会話ではないため履歴に残さない（残すと後続
        # ターンで脳が文脈としてオウム返しする・handle_message のエラー経路と同じ規律）
        if not error:
            self._append_turn(cid, "assistant", reply)
            # 中止の決着報告で返答待ちは解ける（§8.3 (C)。続きの依頼はメンションで受ける）
            self._set_awaiting(cid, False)
        self.slack.notify(
            reply, msg.get("channel_id"),
            team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))

    @staticmethod
    def _format_cancel_report(report: dict) -> str:
        """停止結果を「特定結果 + 停止一覧のみ」の 1 メッセージに整形する（§8.10d 報告規律）。

        作業手順の説明・次アクションの提案は含めない。対象ゼロは「停止対象なし」を返す
        （承認ゲート形式の確認は用いない）。
        """
        sections = [
            ("提示中の計画を破棄", report.get("confirms") or []),
            ("実行中タスクを停止", report.get("running") or []),
            ("未着手タスクを停止", report.get("queued") or []),
            ("承認待ちタスクを停止", report.get("held") or []),
        ]
        lines = [f"- {label}: {item}" for label, items in sections for item in items]
        if not lines:
            return ("停止対象の作業はありませんでした"
                    "（この会話に提示中の計画・実行中/待機中のタスクは見つかりません）。")
        return f"🛑 中止しました（{len(lines)} 件）\n" + "\n".join(lines)

    def _append_turn(self, cid: str, role: str, text: str):
        """セッションへ 1 ターン追記し、永続化まで行う（排他付き。丸めない・§8.3 (C)）。"""
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            history.append({"role": role, "text": text})
            self._persist_session(cid, history)

    def append_task_result(self, task: dict, result_text: str, result_path: str,
                           workspace: str | None = None):
        """タスク完了結果を発生元の会話セッションへ assistant ターンとして還流する。

        設計書 §8.9「会話への還流」。これにより完了後の後続質問（「さっきの回答はどこ」等)
        に会話脳が文脈として答えられる。conversation_id はタスクの team/channel/thread から
        復元する（u-zu の採番規則 §8.3: thread_ts が無い DM 等は user_id）。
        result_text はグラウンディング判定を先頭に併記したもの（§8.9。worker の自己申告を
        会話脳が事実として引き継がない）。workspace は確認系質問（§8.3 probe）の実測対象
        として会話に紐付ける。
        """
        # 還流先はタスク自身が持つ紐づけキーを最優先する（§8.3 (C)。導出規則の二重管理の解消）
        cid = task.get("conversation_id")
        if not cid:
            # キーを持たない旧タスクへのフォールバック: u-zu の derive_conversation_id と
            # 同一規則で復元する（別パッケージ・別配備のため import 共有はできず、空要素の
            # '-' 置換まで含めて式を一致させる。ズレると還流が実セッションに届かない）
            tail = task.get("thread_ts") or task.get("user_id") or ""
            if not tail:
                return  # 会話由来でないタスク（file_audit 等）は還流先セッションを持たない
            cid = f"{task.get('team_id') or '-'}:{task.get('channel_id') or '-'}:{tail}"
        if workspace:
            self._set_last_workspace(cid, workspace)
        summary = result_text[:RESULT_REFLOW_MAX_CHARS]
        if len(result_text) > RESULT_REFLOW_MAX_CHARS:
            summary += "\n…（以降略）"
        self._append_turn(
            cid, "assistant",
            f"（タスク実行完了。結果の要約は以下、全文は結果ファイル {result_path} にあります）\n{summary}")
        # 完了還流で返答待ちは解ける（§8.3 (C)。続きの依頼はメンションで受ける）
        self._set_awaiting(cid, False)

    @staticmethod
    def _coerce_ready(parsed: dict) -> bool:
        """パース済み応答から契約キー `ready` を取り出し、boolean へ確定させる（#taka-ma/145）。

        契約 {reply, ready, summary}（§8.3）では ready は必須の boolean だが、脳 LLM
        （qwen3.6:35b-a3b・think=false）が ready キー自体を欠落した JSON を返すことが
        実測されている（2026-08-16 分離実行）。従来は None が falsy として偶然会話継続に
        落ちるだけで契約逸脱を検知していなかった。逸脱の扱いを暗黙でなく明示コードで
        定義する:

        - ready キー欠落 → 安全側の会話継続（False）を明示的に選び、warning で欠落を記録
        - ready が boolean 以外（"true" 等の文字列・数値・null） → 同じく False + warning。
          従来の bool() 変換では文字列 "false" が truthy となり誤って実行確認へ進み得た
        - ready が boolean → そのまま返す（正常系・ログなし）

        いずれも例外を投げない（壊れ応答で会話を止めない。JSONDecodeError フォール
        バック・#142 の契約キーフィルタと同じく「解釈できない出力で実行へ進めない」側に
        倒す）。warning は発生率の観測点（このログの件数 / 会話ターン数）として使う。
        """
        if "ready" not in parsed:
            logger.warning(
                "会話 LLM 応答が契約逸脱: ready キー欠落（会話継続へ縮退・keys=%s）",
                sorted(parsed.keys()))
            return False
        ready = parsed["ready"]
        if not isinstance(ready, bool):
            logger.warning(
                "会話 LLM 応答が契約逸脱: ready が boolean でない"
                "（type=%s value=%.80r・会話継続へ縮退）",
                type(ready).__name__, ready)
            return False
        return ready

    def _invoke_llm(self, history: list[dict], force: bool,
                    progress: GenerationProgress | None = None,
                    detail_retry: bool = False) -> dict:
        """脳 LLM（sa-ru.model）を呼び、{reply, ready, summary} を返す。

        パース失敗時は会話継続（ready=false）にフォールバックし、素の stdout を返信に回す
        （安全側: 解釈できない出力で勝手に実行へ進めない）。ただし契約 JSON の断片が
        混じった出力は人向け文言へ縮退し、内部 JSON を Slack へ生で見せない（#taka-ma/142）。
        force=True は要約を促す指示を足す。

        失敗は原因別に扱う（設計書 §8.3 エラーハンドリング）: タイムアウト・接続失敗は
        1 回リトライし、それでも失敗したら原因を明示した文言を返信に回す。原因不明の
        包括表現（「内部エラー」）は使わない。
        """
        history_text = "\n".join(
            f"{'ユーザー' if t['role'] == 'user' else 'sa-ru'}: {t['text']}" for t in history
        )
        latest = history[-1]["text"] if history else ""
        prompt = self._prompt_template.replace("{history}", history_text).replace("{message}", latest)
        if force:
            prompt += (
                "\n\n## 指示\n"
                "ユーザーが明示的に実行を指示しました。会話が短くても、これまでの会話から意図を読み取り、"
                "ready=true として summary に実行指示をまとめてください。"
            )
        if detail_retry:
            # ready 再検査（§8.3 細部質問の検品）の再判定。直前の応答が細部への質問と
            # 選別されたときだけ 1 回付く。force と違い ready=true を強制しない — 対象・
            # 動作が本当に特定できないなら質問し直す余地を残す（誤検品の安全側）
            prompt += (
                "\n\n## 指示\n"
                "あなたは直前に、実装の細部（書く内容・文言・書式・ブランチ名・コミットメッセージ等）を"
                "尋ねる質問を返そうとしました。細部への質問は禁止です。対象と動作が発話から特定できる"
                "なら、質問せず ready=true として summary に実行指示をまとめてください（細部は worker が"
                "妥当な既定で決めます）。対象か動作そのものが特定できない場合のみ、それを確かめる質問を"
                "返してください。"
            )

        stdout = None
        try:
            try:
                stdout = run_ollama(self.model, prompt, timeout=self.timeout,
                                    host=self.ollama_host, think=self.think,
                                    progress=progress)
            except (OllamaTimeoutError, OllamaConnectionError) as first_err:
                # 一過性（モデルロード直後の混雑・ollama 再起動中等）を 1 回だけ吸収する
                logger.warning("会話 LLM 呼び出し失敗（リトライ 1 回目）: %s", first_err)
                stdout = run_ollama(self.model, prompt, timeout=self.timeout,
                                    host=self.ollama_host, think=self.think,
                                    progress=progress)
            # 脳モデルは json.loads が失敗する ```json フェンス付きで出力することがある
            # （gemma4:12b の実機検証で再現・2026-07-04）。ai_gateway 側 classifier/decomposer
            # と同じ extract_json でフェンス除去してからパースする（同根の欠陥・§9.2 と同一パターン）。
            # markdown 癖の不正エスケープ（\` 等）はパース失敗時のみ機械修復を 1 度試す
            # （2026-08-24 E2E で実測。正しい出力には触れない）
            try:
                parsed = json.loads(extract_json(stdout))
            except json.JSONDecodeError:
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
            # probe は許可値のみ通す（応答の組み立てはコード側で固定。脳 LLM の
            # 出力を任意コマンド実行・任意動作に接続しない・§8.3「確認系質問への実測応答」）
            probe = parsed.get("probe")
            return {
                "reply": parsed.get("reply", ""),
                "ready": self._coerce_ready(parsed),
                "summary": parsed.get("summary"),
                "probe": probe if probe in _PROBE_KINDS else None,
            }
        except json.JSONDecodeError:
            # JSON 化できない出力は会話継続に回す（解釈できない出力で実行へ進めない）
            text = (stdout or "").strip()
            if _CONTRACT_KEY_RE.search(text):
                # 契約 JSON 断片の混じった壊れ出力は人に見せない（会話出口の内部 JSON
                # フィルタ・#taka-ma/142）。タスクは止めず会話継続（ready=false）のまま
                # 言い直しを促す。error=True でエラー文言を履歴に残さない（脳がオウム
                # 返しする実機再現 2026-07-14 と同じ扱い。壊れ出力自体も文脈にしない）
                logger.warning(
                    "会話 LLM 出力に契約 JSON 断片が混入（人向け文言へ縮退・生出力 %d 文字）",
                    len(text))
                return {
                    "reply": "（応答の整形に失敗しました。もう一度お願いします）",
                    "ready": False, "summary": None, "error": True,
                }
            return {"reply": text, "ready": False, "summary": None}
        except OllamaTimeoutError:
            logger.exception("会話 LLM がタイムアウト（リトライ含め 2 回失敗）")
            return {
                "reply": (
                    f"応答の生成が {self.timeout} 秒の上限を超えました（会話モデル {self.model}）。"
                    "少し時間を置いて再度お送りください。"),
                "ready": False, "summary": None, "error": True,
            }
        except OllamaConnectionError as e:
            logger.exception("会話 LLM へ接続失敗（リトライ含め 2 回失敗）")
            return {
                "reply": f"ローカル LLM（ollama）へ接続できませんでした: {e}",
                "ready": False, "summary": None, "error": True,
            }
        except Exception as e:
            # 想定外も原因を明示する（原因不明の包括表現は使わない・設計書 §8.3）
            logger.exception("会話 LLM 呼び出しで想定外の失敗")
            return {
                "reply": f"応答を生成できませんでした（{type(e).__name__}: {e}）",
                "ready": False, "summary": None, "error": True,
            }

    # ── 契約化パス（設計書 §8.10f。ready=true 後の第 2 の構造化呼び出し） ──

    def _build_contract(self, cid: str, summary: str, progress=None) -> dict | None:
        """会話から実行契約 {directive, constraints, acceptance, workspace, needs_repo} を得る。

        会話出口契約 {reply, ready, summary} にはキーを足さず、契約化専用のプロンプト
        （contract.md）で脳モデルをもう 1 回呼ぶ（§8.10f。逸脱実績のある契約の対象面を
        広げない）。出力はコード側（contract_rules.validate_contract）で検証し、directive /
        constraints は**ユーザー発話の逐語引用**のみ受理する。逸脱・パース不能は 1 回
        リトライし、それでも確定しなければ None（呼び出し側が fail-closed で人へ差し戻す）。
        """
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            snapshot = self._history_view(history)
            # 逐語照合の出典はユーザー発話のみ（summary は脳の言い換えであり出典にしない）
            source_text = "\n".join(t["text"] for t in history if t.get("role") == "user")
        history_text = "\n".join(
            f"{'ユーザー' if t['role'] == 'user' else 'sa-ru'}: {t['text']}" for t in snapshot)
        prompt = (self._contract_template
                  .replace("{history}", history_text)
                  .replace("{summary}", summary))
        for attempt in (1, 2):
            try:
                stdout = run_ollama(self.model, prompt, timeout=self.timeout,
                                    host=self.ollama_host, think=self.think,
                                    progress=progress)
                try:
                    parsed = json.loads(extract_json(stdout))
                except json.JSONDecodeError:
                    # 不正エスケープの機械修復（_invoke_llm と同じ規律・2026-08-24 実測）
                    parsed = json.loads(extract_json(repair_json_escapes(stdout)))
            except (json.JSONDecodeError, OllamaTimeoutError, OllamaConnectionError) as e:
                logger.warning("契約化パス失敗（%d 回目）: %s", attempt, e)
                continue
            validated, problems = contract_rules.validate_contract(parsed, source_text)
            if validated is not None:
                # push を含む依頼で完了条件が空なら既定検査を付与する（§8.10f。脳の
                # 立て損ねで「push の実測検査なしに完了」と言える状態を作らない）
                return contract_rules.apply_default_acceptance(validated, summary)
            logger.warning("契約化パスの出力が逸脱（%d 回目）: %s", attempt, problems)
        return None

    # ── 計画プレビューの訂正（設計書 §8.10b / §10.2.1） ──

    def _pending_confirm(self, msg: dict) -> tuple[str, dict] | None:
        """提示中（pending）の確認レコードを 1 件返す。無ければ None。

        照合は 2 段階（設計書 §8.10b「訂正の受け口」）:
          1. conversation_id 完全一致 — 同じスレッド内での返信
          2. 同一の (team_id, channel_id, user_id) — 同じ相手との同じ会話面での発話

        2 を持つのは、u-zu が DM・メンションの `thread_ts` に「スレッド起点、無ければ
        その投稿自身の ts」を入れるため、**新規投稿は毎回別の conversation_id になる**
        ことによる（実機で確認）。1 だけだと、計画プレビューに対してユーザーがスレッド
        ではなく普通に投稿した訂正が届かず、無言で新しい会話として扱われる。
        誤爆（新しい依頼を訂正と誤読する）は起きにくい: 簡易記法は決定的で、自然言語は
        ya-ta が訂正でなければ空パッチを返し通常会話へ落ちる（§10.2.1）。

        done/ 等のサブディレクトリは走査しない（決着済みは訂正対象にならない）。
        """
        found = self._pending_confirms(msg)
        if not found:
            return None
        _, path, record = max(found)
        return path, record

    def _pending_confirms(self, msg: dict) -> list[tuple]:
        """提示中（pending）の確認レコード群を (created_at, path, record) で返す。

        照合規則は _pending_confirm の 2 段階と同一（スレッド一致を優先し、無いときだけ
        同一会話面へ落とす）。件数は進行状況の実測応答が実数で報告する（§8.3。固定文言
        「1 件」が実態 2 件と食い違った 2026-08-26 E2E 検出の是正）。
        """
        if not os.path.isdir(self.confirm_dir):
            return []
        cid = msg.get("conversation_id")
        same_cid, same_channel = [], []
        for name in os.listdir(self.confirm_dir):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.confirm_dir, name)
            try:
                with open(path) as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue  # 壊れたレコードは確認ループ側が failed/ へ隔離する
            if record.get("status") != "pending":
                continue
            entry = (record.get("created_at") or "", path, record)
            if record.get("conversation_id") == cid:
                same_cid.append(entry)
            elif (record.get("team_id") == msg.get("team_id")
                    and record.get("channel_id") == msg.get("channel_id")
                    and record.get("user_id") == msg.get("user_id")):
                same_channel.append(entry)
        return same_cid or same_channel

    def _handle_correction(self, msg: dict, progress=None) -> bool:
        """提示済みプランへの訂正として処理できたら True を返す（会話処理はスキップ）。

        簡易記法は即適用して更新後プラン全体を再提示、自然言語（音声の主経路）は適用後に
        差分だけ返して再確認する（設計書 §10.2.1「差分エコー再確認」）。訂正の適用前に
        レコードを読み直し、既に着手済みなら適用しない（承認されたプランと実行される
        プランの食い違いを作らない・§8.10b）。
        """
        if self.plan_service is None:
            return False
        pending = self._pending_confirm(msg)
        if not pending:
            return False
        path, record = pending
        plan = record.get("plan")
        if not plan:
            return False  # プレビュー無しで提示された確認（分解失敗時の縮退）は訂正対象外
        updated, echo, route = self.plan_service.correct(plan, msg["text"], progress=progress)
        if route is None:
            return False  # 訂正ではない → 通常の会話処理へ

        # 適用直前に読み直す（訂正の解釈中に「着手」が押されている可能性がある）
        try:
            with open(path) as f:
                latest = json.load(f)
        except (OSError, json.JSONDecodeError):
            latest = None
        if not latest or latest.get("status") != "pending":
            self.slack.notify(
                "この計画は既に決着済みです（訂正は反映していません）。",
                msg.get("channel_id"), team_id=msg.get("team_id"),
                thread_ts=msg.get("thread_ts"))
            return True

        latest["plan"] = updated
        # 返信先を「最後に人が話しかけてきた場所」へ更新する。訂正は提示スレッド外
        # （新規 DM 投稿）からも受けるため（上の _pending_confirm）、元の場所に固定したままだと
        # 着手後の実行通知だけが人の居ないスレッドへ流れる。channel が取れる時のみ更新する
        if msg.get("channel_id"):
            latest["team_id"] = msg.get("team_id", latest.get("team_id", ""))
            latest["channel_id"] = msg["channel_id"]
            latest["thread_ts"] = msg.get("thread_ts")
        atomic_write_json(path, latest)
        logger.info("計画訂正を適用: id=%s route=%s changes=%d",
                    latest.get("exec_request_id"), route, len(echo))

        if route == "simple":
            body = self.plan_service.render(updated)
            if echo:
                body += "\n\n【変更】\n" + "\n".join(echo)
            else:
                body += "\n\n（変更はありませんでした）"
        else:
            # 自然言語・音声は取り違え（sonnet ↔ opus 等）を 1 往復で捕捉するため差分のみ返す
            body = "【変更】\n" + "\n".join(echo)
        # 訂正の再提示もユーザーの決定（着手/さらに訂正）待ち（§8.3 (C) 能動昇格）
        self._set_awaiting(msg.get("conversation_id") or "", True)
        # 更新後の計画は着手ボタン付きで再提示する。訂正を重ねると最初の提示メッセージが
        # 上へ流れ、押すべきボタンを探させることになるため（§8.10b）。exec_request_id は
        # 変えないので、どのメッセージのボタンを押しても同じ確認レコードを決着させる
        self.slack.send_plan_update(
            latest["exec_request_id"], body,
            channel=msg.get("channel_id"), team_id=msg.get("team_id"),
            thread_ts=msg.get("thread_ts"))
        return True

    def _build_plan(self, summary: str, progress=None) -> list[dict] | None:
        """確定要約を分解して計画プレビュー用のサブタスク列を返す（失敗時は None）。

        分解失敗（想定外の例外）でゲート自体を落とさない。None のときはプレビュー無しで
        従来どおり要約のみを提示し、分解は dispatcher 側で行われる（縮退動作）。
        """
        if self.plan_service is None:
            return None
        try:
            return self.plan_service.build(summary, progress=progress)
        except Exception:
            logger.exception("計画プレビューの分解に失敗（要約のみで提示）")
            return None

    def _present_summary(self, msg: dict, summary: str, models: list[str] | None = None,
                         workspace: str | None = None, contract: dict | None = None,
                         progress=None):
        """計画確認レコード（status=pending）を作り、要約 + 契約 + 計画 + ボタンを Slack に提示する。

        models: `:opus` 等で明示指定されたモデル名（handle_message が生文から抽出済み）。
        workspace: 実開発リポジトリの絶対パス（§8.13。記法指定または契約化パス。検証済み）。
        contract: 契約化パスの検証済み出力（§8.10f）。directive / constraints / acceptance を
        確定タスクまで運ぶため record に保持し、提示文に**常に**明示する（空なら「なし」。
        見えていない契約は承認されない）。None は契約未構成（従来動作）。
        分解はここで済ませ、承認されたプランを凍結して実行へ渡す（§8.10b。dispatcher は再分解しない）。
        """
        exec_request_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        # 逐語命令（directive）は分解しない（§10.2。原文を command とする 1 件のプラン）。
        # ya-ta の言い換えを通すと変換のたびに劣化する
        directive = (contract or {}).get("directive")
        if directive:
            plan = [{"step": 1, "command": directive, "execution": "agent",
                     "depth": None, "confidence": 1.0, "depends_on": []}]
        else:
            plan = self._build_plan(summary, progress=progress)
        record = {
            "exec_request_id": exec_request_id,
            "conversation_id": msg["conversation_id"],
            "summary": summary,
            "plan": plan,
            "status": "pending",
            "user_id": msg.get("user_id", ""),
            "team_id": msg.get("team_id", ""),
            "channel_id": msg.get("channel_id", ""),
            "thread_ts": msg.get("thread_ts"),
            "model_override": models or [],
            "workspace": workspace,
            "directive": directive,
            "constraints": (contract or {}).get("constraints") or [],
            "acceptance": (contract or {}).get("acceptance") or [],
            "needs_repo": bool((contract or {}).get("needs_repo")),
            "created_at": now,
            "decided_at": None,
            "decided_by": None,
        }
        path = os.path.join(self.confirm_dir, f"{exec_request_id}.json")
        # 原子書込。u-zu / sa-ru 双方が確認レコードを読むため torn-read を防ぐ（§8.3 書込の原子性）。
        atomic_write_json(path, record)

        # 着手確認提示のタイミングを可視化する（従来はログが無く、発話受信〜要約提示の
        # 所要時間が計測不能だった。実運用フィードバックを受けて追加）。
        logger.info("計画確認提示: id=%s conversation_id=%s subtasks=%s",
                    exec_request_id, msg["conversation_id"],
                    len(plan) if plan else 0)
        plan_text = self.plan_service.render(plan) if (plan and self.plan_service) else None
        # workspace は指定の有無にかかわらず常に提示する（§8.13 / #143。未指定のまま空の
        # 使い捨て作業場で worker が走ることに人間が着手前に気づけるようにする。2026-08-10
        # インシデントでは未指定が提示文に出ず、10 日間誰も気づけなかった）
        workspace_text = workspace or (
            "未指定（既定の空作業場）— 実リポジトリで作業させるには"
            " `repo:/絶対パス` を指定してください")
        kwargs = {"channel": msg.get("channel_id"), "team_id": msg.get("team_id"),
                  "thread_ts": msg.get("thread_ts"), "plan_text": plan_text,
                  "workspace_text": workspace_text}
        if contract is not None:
            # 契約は空でも「なし」を明示する（§8.10f。見えていない契約は承認されない）
            kwargs["contract_text"] = self._format_contract(contract)
        # 計画提示 = 着手/訂正の返答待ち（§8.3 (C) 能動昇格。訂正はスレッド返信で届く）
        self._set_awaiting(msg["conversation_id"], True)
        self.slack.send_exec_confirm_request(exec_request_id, summary, **kwargs)

    @staticmethod
    def _format_contract(contract: dict) -> str:
        """着手確認へ載せる契約の提示文（§8.10f。空欄も「なし」と明示する）。"""
        lines = [f"命令（逐語実行）: {contract.get('directive') or 'なし'}"]
        constraints = contract.get("constraints") or []
        if constraints:
            lines.append("拘束条件:")
            for c in constraints:
                prefix = "（禁止）" if c.get("forbid") else ""
                lines.append(f"- {prefix}{c.get('text', '')}")
        else:
            lines.append("拘束条件: なし")
        acceptance = contract.get("acceptance") or []
        if acceptance:
            lines.append("完了条件（完了時に sa-ru が実測検査）:")
            for a in acceptance:
                params = " ".join(f"{k}={v}" for k, v in (a.get("params") or {}).items())
                lines.append(f"- {a.get('kind')} {params}".rstrip())
        else:
            lines.append("完了条件: なし（完了検査は行われません）")
        return "\n".join(lines)

    # ── 着手確認の決着（確認ループから呼ばれる） ──

    def create_exec_task(self, record: dict) -> str:
        """確認済み要約から確定タスク（status=init）を生成する。生成した task_id を返す。

        u-zu の task_queue.enqueue_task と同じ §8.3 タスク形式。source="conversation"、
        command は生文ではなく sa-ru が固めた構造化要約（責任分界の移動）。
        conversation_id / parent_task_id で発生元会話と直前タスクへの紐づけをタスク
        ファイル自身に永続化する（§8.3 (C) 配管層）。
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = str(uuid.uuid4())
        cid = record.get("conversation_id") or ""
        # 同一会話から直前に生成したタスクを親として継承する（§8.3 (C) 親子チェーン）。
        # 永続化ファイルからの遅延回復を経るため、sa-ru 再起動をまたいでも途切れない
        parent_task_id = None
        if cid:
            with self._sessions_lock:
                self._load_or_create_session(cid)
                parent_task_id = self._last_task_id.get(cid)
        # "_model" は _execute_worker_task が読む明示モデル指定キー（設計書「ユーザーモデル指定」）。
        # queue_item = {**task, ...} でサブタスクへそのまま伝播する（新規配線不要でこのキー名に揃える）。
        model_override = record.get("model_override") or []
        task = {
            "task_id": task_id,
            "status": "init",
            "source": "conversation",
            "command": record["summary"],
            "user_id": record.get("user_id", ""),
            "team_id": record.get("team_id", ""),
            "channel_id": record.get("channel_id", ""),
            "thread_ts": record.get("thread_ts"),
            # 会話→タスクの継続紐づけ（§8.3 (C)）。完了還流・intent レコード（§8.10e）が使う
            "conversation_id": cid or None,
            "parent_task_id": parent_task_id,
            "_model": model_override or None,
            "created_at": now,
            "updated_at": now,
        }
        # `repo:` 実開発リポジトリ指定（§8.13）。指定時のみキーを持たせ、dispatcher の
        # queue_item = {**task, ...} 伝播と _resolve_workspace が workspace を解決する
        if record.get("workspace"):
            task["workspace"] = record["workspace"]
        # 承認された計画（訂正の上書き反映済み）を凍結して渡す。dispatcher は _plan があれば
        # 再分解しない（提示した計画と実際に走る計画を一致させる・設計書 §10.2「凍結プランの実行」）
        if record.get("plan"):
            task["_plan"] = record["plan"]
        # 会話⇄実行の受け渡し契約（§8.10f）。着手確認で人が承認した値だけを実行・検証へ運ぶ
        for key in ("directive", "constraints", "acceptance"):
            if record.get(key):
                task[key] = record[key]
        os.makedirs(self.task_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        path = os.path.join(self.task_dir, f"{ts}_{task_id}.json")
        # 原子書込。dispatcher が部分書込の init タスクを拾う torn-read を防ぐ（§8.3 書込の原子性）。
        atomic_write_json(path, task)
        # 直近確定タスクを更新する（§8.3 (C)。次のタスクの parent になる。永続化は直後の
        # _set_last_workspace が last_task_id ごとセッションと一緒に書く）
        if cid:
            with self._sessions_lock:
                self._last_task_id[cid] = task_id
        # 着手で返答待ちは解ける（§8.3 (C)。実行中スレッドの非メンション返信は passive）
        self._set_awaiting(cid, False)
        # 実行 workspace を会話に紐付ける（§8.3 probe。repo: 指定が無ければ既定の
        # {workspace_base}/{task_id} — Orchestrator._workspace_for と同じ解決規則）
        self._set_last_workspace(
            cid, record.get("workspace") or f"{self.workspace_base}/{task_id}")
        # intent レコード（§8.10e / §8.10f 依頼の寿命）。完了条件が PASS するまで open で
        # 閉じない台帳。作成失敗はタスク実行を止めない（記録の欠落は goal 更新のスキップに
        # 閉じ、実行自体は従来経路で進む）
        intents_dir = getattr(self, "intents_dir", None)
        if intents_dir:
            try:
                intent_store.create(
                    intents_dir, task_id=task_id, conversation_id=cid or None,
                    summary=record["summary"],
                    acceptance=record.get("acceptance") or [],
                    workspace=record.get("workspace") or f"{self.workspace_base}/{task_id}",
                    user_id=record.get("decided_by") or record.get("user_id", ""))
            except OSError:
                logger.exception("intent レコード作成失敗（タスク実行は継続）: %s", task_id)
        self.slack.notify(
            "着手します。実行を開始しました。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"))
        return task_id

    def record_task_outcome(self, task: dict, cause: str | None):
        """タスク終端の失敗原因コードを会話セッションへ記録する（§8.10f 反復停止判定の材料）。

        cause=None は成功（連続カウントをリセット）。会話由来でないタスクは対象外。
        orchestrator がタスク終端時に to_thread で呼ぶ（append_task_result と同じ規律）。
        """
        cid = task.get("conversation_id")
        if not cid:
            return
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            if cause:
                causes = self._causes().setdefault(cid, [])
                causes.append(cause)
                # 判定（直近 2 件の一致）に要る分だけ保持する（無限成長の防止）
                del causes[:-4]
            else:
                self._causes().pop(cid, None)
            self._persist_session(cid, history)

    def notify_rejected(self, record: dict):
        """やり直し選択時。実行はせず会話継続を促す（履歴は維持される）。"""
        # 「続けて指示してください」= 次の指示の返答待ち（§8.3 (C) 能動昇格）
        self._set_awaiting(record.get("conversation_id") or "", True)
        self.slack.notify(
            "やり直します。続けて指示してください。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"))
