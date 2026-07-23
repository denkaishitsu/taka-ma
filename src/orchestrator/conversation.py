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
    run_ollama,
)
from orchestrator.file_queue import atomic_write_json

logger = logging.getLogger("sa-ru.conversation")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 脳 LLM へ渡す会話履歴の最大ターン数（プロンプト肥大と KV キャッシュ膨張を防ぐ）。
MAX_HISTORY_TURNS = 20

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


class InvalidWorkspaceError(ValueError):
    """`repo:` 指定が検証を通らない（相対パス・危険文字・`..` 等）ときのエラー。"""


class ConversationManager:
    """会話セッションの保持・脳 LLM 呼び出し・要約提示・確定タスク生成を担う。

    セッション履歴はターン追記のたびにディスクへ原子書込で永続化する（設計書 §8.3
    「会話セッション履歴の永続化」）。sa-ru 再起動・TTL 経過後も次の発話時にファイルから
    文脈を回復するため、会話の記憶を失わない。
    """

    def __init__(self, config, slack_notifier, task_dir: str, classifier=None,
                 plan_service=None):
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
        """
        self.config = config
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
        self.confirm_dir = config["exec_confirm"]["dir"]    # 着手確認レコードの dir
        os.makedirs(self.confirm_dir, exist_ok=True)
        # 会話プロンプトは静的なので起動時に 1 度だけ読む（毎ターンの disk I/O を避ける）
        self._prompt_template = (PROMPTS_DIR / "converse.md").read_text()
        # TTL はセッションの「メモリからのアンロード」期限。永続化ファイルは残るため
        # TTL 経過・再起動後も次の発話時に文脈を回復できる（設計書 §8.3 永続化）。
        # sa-ru.yaml を唯一の供給元とする（コード既定値なし。sessions_dir と流儀を揃える）
        self.session_ttl_sec = config["conversation"]["session_ttl_sec"]
        # セッション永続化の保存先（conversation_id 単位の JSON・原子書込）。
        # sa-ru.yaml を唯一の供給元とする（コード側に既定値を置くと供給元が二重になる）
        self.sessions_dir = config["conversation"]["sessions_dir"]
        os.makedirs(self.sessions_dir, exist_ok=True)
        # conversation_id → [{"role": "user"|"assistant", "text": str}, ...]
        self.sessions: dict[str, list[dict]] = {}
        # conversation_id → 最終アクセス時刻（monotonic 秒）。エビクション判定に使う
        self._last_seen: dict[str, float] = {}
        # セッション辞書の排他。会話処理は to_thread（別スレッド）、タスク結果の還流
        # （append_task_result）はイベントループ側スレッドから呼ばれ、同一セッションを
        # 同時に触り得るため（設計書 §8.9「会話への還流」）
        self._sessions_lock = threading.Lock()

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
                    history = json.load(f).get("turns", [])
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
                "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
        except OSError:
            logger.exception("会話セッションの永続化失敗（メモリ上は継続）: %s", cid)

    # ── 会話処理（会話ループから to_thread で呼ばれる：脳 LLM は同期ブロック） ──

    @staticmethod
    def parse_workspace(text: str) -> tuple[str, str | None]:
        """生文から `repo:<絶対パス>` を抽出し、(除去後テキスト, workspace|None) を返す。

        `:モデル名` 指定（classifier.parse_model）より先に呼ぶこと。`repo:/path` の
        `:/path` 部分が parse_model の `:(\\S+)` に誤マッチして未登録モデル扱いになるため、
        先に取り除く必要がある。検証（§8.13 repo: パスの検証）に通らない指定は
        InvalidWorkspaceError で着手前に差し戻す（fail-closed）。
        """
        matches = _REPO_TOKEN_RE.findall(text)
        if not matches:
            return text, None
        if len(set(matches)) > 1:
            raise InvalidWorkspaceError("repo: 指定が複数あります。1 つにしてください")
        workspace = matches[0].rstrip("/")
        if workspace.startswith("~"):
            raise InvalidWorkspaceError(
                "repo: は絶対パスで指定してください（~ は使えません。"
                "例: repo:/Users/<user>/DevDev/xxx）")
        if not _SAFE_WORKSPACE_RE.match(workspace) or ".." in workspace.split("/"):
            raise InvalidWorkspaceError(
                "repo: のパスが不正です（絶対パス・英数と . _ - / のみ・.. 不可）")
        clean = _REPO_TOKEN_RE.sub("", text).strip()
        return clean, workspace

    def _evict_idle_sessions(self, now: float):
        """TTL を超えて使われていないセッションをメモリからアンロードする。

        永続化ファイルは削除しない（時間経過で記憶を失わない・設計書 §8.3）。
        """
        stale = [c for c, seen in self._last_seen.items() if now - seen > self.session_ttl_sec]
        for c in stale:
            self.sessions.pop(c, None)
            self._last_seen.pop(c, None)

    def handle_message(self, msg: dict, progress: GenerationProgress | None = None):
        """1 件の発話を処理する。会話継続なら返信、意図が固まれば着手確認を提示する。

        progress はハートビート進捗通知（§10.8）へ生成トークン数を届ける共有ホルダー
        （呼び出し元 _run_with_heartbeat が渡す）。
        """
        cid = msg["conversation_id"]
        # 脳 LLM 呼び出し開始のタイミングを可視化する(投稿受信〜着手確認提示の所要時間を
        # 計測できるようにする。実運用フィードバックを受けて追加）。
        logger.info("会話メッセージ処理開始: conversation_id=%s", cid)

        # 計画確認中（pending の確認レコードがある）なら、発話をまず「提示済みプランへの訂正」
        # として解釈する（設計書 §8.3 訂正経路 / §10.2.1）。訂正と解釈できなければ通常の会話へ
        # 落とす（人間がプランを捨てて話を続ける経路を塞がない）。/taka-ma-go は締め直しの
        # 明示エスケープなので訂正解釈に回さない。
        if not msg.get("force_ready") and self._handle_correction(msg, progress=progress):
            return
        now = time.monotonic()
        with self._sessions_lock:
            self._evict_idle_sessions(now)
            self._last_seen[cid] = now
            history = self._load_or_create_session(cid)
            history.append({"role": "user", "text": msg["text"]})
            # 履歴は直近 MAX_HISTORY_TURNS に丸める（プロンプト肥大・KV キャッシュ膨張防止）
            if len(history) > MAX_HISTORY_TURNS:
                del history[:-MAX_HISTORY_TURNS]
            self._persist_session(cid, history)
            # 脳 LLM 呼び出し（数十秒）中はロックを持たない。以降 history はこのターンの
            # スナップショットとして扱い、追記時に再ロックする
            history_snapshot = list(history)

        if msg.get("force_ready"):
            # /taka-ma-go: LLM 判定を待たず要約させて強制的に締める
            result = self._invoke_llm(history_snapshot, force=True, progress=progress)
            result["ready"] = True
        else:
            result = self._invoke_llm(history_snapshot, force=False, progress=progress)

        if result.get("ready") and result.get("summary"):
            summary = result["summary"]
            self._append_turn(cid, "assistant", summary)
            # `repo:` 実開発リポジトリ指定と `:opus` 等の明示モデル指定は要約（脳 LLM の
            # 言い換え）には残らないため、要約対象の生文から直接抽出する（§8.13 /
            # 設計書「ユーザーモデル指定」）。repo: を先に除去しないと `:/path` が
            # parse_model に未登録モデルとして誤検出される。
            try:
                text_wo_repo, workspace = self.parse_workspace(msg["text"])
            except InvalidWorkspaceError as e:
                self.slack.notify(
                    str(e), msg.get("channel_id"),
                    team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                return
            models: list[str] = []
            if self.classifier is not None:
                try:
                    _, models = self.classifier.parse_model(text_wo_repo)
                except InvalidModelError as e:
                    self.slack.notify(
                        str(e), msg.get("channel_id"),
                        team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))
                    return
            self._present_summary(msg, summary, models, workspace, progress=progress)
        else:
            reply = result.get("reply") or "（応答を生成できませんでした。もう一度お願いします）"
            # エラー由来の返信（タイムアウト・接続失敗等）はシステムメッセージであり会話では
            # ないため履歴に残さない。残すと後続ターンで脳がエラー文言を会話文脈として
            # オウム返しする（実機で再現・2026-07-14）。
            if not result.get("error"):
                self._append_turn(cid, "assistant", reply)
            self.slack.notify(
                reply, msg.get("channel_id"),
                team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"))

    def _append_turn(self, cid: str, role: str, text: str):
        """セッションへ 1 ターン追記し、丸め・永続化まで行う（排他付き）。"""
        with self._sessions_lock:
            history = self._load_or_create_session(cid)
            history.append({"role": role, "text": text})
            if len(history) > MAX_HISTORY_TURNS:
                del history[:-MAX_HISTORY_TURNS]
            self._persist_session(cid, history)

    def append_task_result(self, task: dict, result_text: str, result_path: str):
        """タスク完了結果を発生元の会話セッションへ assistant ターンとして還流する。

        設計書 §8.9「会話への還流」。これにより完了後の後続質問（「さっきの回答はどこ」等)
        に会話脳が文脈として答えられる。conversation_id はタスクの team/channel/thread から
        復元する（u-zu の採番規則 §8.3: thread_ts が無い DM 等は user_id）。
        """
        tail = task.get("thread_ts") or task.get("user_id") or ""
        if not tail:
            return  # 会話由来でないタスク（file_audit 等）は還流先セッションを持たない
        # u-zu の derive_conversation_id と同一規則で復元する（別パッケージ・別配備のため
        # import 共有はできず、空要素の '-' 置換まで含めて式を一致させる。ズレると還流が
        # 実セッションに届かず新規セッションへ落ちる）
        cid = f"{task.get('team_id') or '-'}:{task.get('channel_id') or '-'}:{tail}"
        summary = result_text[:RESULT_REFLOW_MAX_CHARS]
        if len(result_text) > RESULT_REFLOW_MAX_CHARS:
            summary += "\n…（以降略）"
        self._append_turn(
            cid, "assistant",
            f"（タスク実行完了。結果の要約は以下、全文は結果ファイル {result_path} にあります）\n{summary}")

    def _invoke_llm(self, history: list[dict], force: bool,
                    progress: GenerationProgress | None = None) -> dict:
        """脳 LLM（sa-ru.model）を呼び、{reply, ready, summary} を返す。

        パース失敗時は会話継続（ready=false）にフォールバックし、素の stdout を返信に回す
        （安全側: 解釈できない出力で勝手に実行へ進めない）。force=True は要約を促す指示を足す。

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
            parsed = json.loads(extract_json(stdout))
            return {
                "reply": parsed.get("reply", ""),
                "ready": bool(parsed.get("ready", False)),
                "summary": parsed.get("summary"),
            }
        except json.JSONDecodeError:
            # JSON 化できない出力は会話継続に回す（解釈できない出力で実行へ進めない）
            return {"reply": (stdout or "").strip(), "ready": False, "summary": None}
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
        if not os.path.isdir(self.confirm_dir):
            return None
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
        # 同一会話の pending が複数並存し得る（決着させず会話を続けた場合）。最新を対象にする。
        # スレッド一致を優先し、無いときだけ同一会話面の最新へ落とす
        found = same_cid or same_channel
        if not found:
            return None
        _, path, record = max(found)
        return path, record

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
                         workspace: str | None = None, progress=None):
        """計画確認レコード（status=pending）を作り、要約 + 計画 + ボタンを Slack に提示する。

        models: `:opus` 等で明示指定されたモデル名（handle_message が生文から抽出済み）。
        workspace: `repo:` で明示指定された実開発リポジトリの絶対パス（§8.13。検証済み）。
        いずれも確定タスク生成（create_exec_task）まで運ぶため record に保持する。
        分解はここで済ませ、承認されたプランを凍結して実行へ渡す（§8.10b。dispatcher は再分解しない）。
        """
        exec_request_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
        self.slack.send_exec_confirm_request(
            exec_request_id, summary,
            channel=msg.get("channel_id"), team_id=msg.get("team_id"),
            thread_ts=msg.get("thread_ts"), plan_text=plan_text)

    # ── 着手確認の決着（確認ループから呼ばれる） ──

    def create_exec_task(self, record: dict) -> str:
        """確認済み要約から確定タスク（status=init）を生成する。生成した task_id を返す。

        u-zu の task_queue.enqueue_task と同じ §8.3 タスク形式。source="conversation"、
        command は生文ではなく sa-ru が固めた構造化要約（責任分界の移動）。
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = str(uuid.uuid4())
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
        os.makedirs(self.task_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        path = os.path.join(self.task_dir, f"{ts}_{task_id}.json")
        # 原子書込。dispatcher が部分書込の init タスクを拾う torn-read を防ぐ（§8.3 書込の原子性）。
        atomic_write_json(path, task)
        self.slack.notify(
            "着手します。実行を開始しました。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"))
        return task_id

    def notify_rejected(self, record: dict):
        """やり直し選択時。実行はせず会話継続を促す（履歴は維持される）。"""
        self.slack.notify(
            "やり直します。続けて指示してください。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"))
