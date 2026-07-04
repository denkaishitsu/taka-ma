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
import time
import uuid
from pathlib import Path

from ai_gateway.llm import extract_json, run_ollama

logger = logging.getLogger("sa-ru.conversation")

PROMPTS_DIR = Path(__file__).parent / "prompts"

# 脳 LLM へ渡す会話履歴の最大ターン数（プロンプト肥大と KV キャッシュ膨張を防ぐ）。
MAX_HISTORY_TURNS = 20


class ConversationManager:
    """会話セッションの保持・脳 LLM 呼び出し・要約提示・確定タスク生成を担う。

    セッション履歴は in-memory（conversation_id → ターン列）。sa-ru 再起動で会話文脈は失われるが、
    確定済みタスク・確認レコードはファイルとして残るため実行の取りこぼしは起きない。
    """

    def __init__(self, config, slack_notifier, task_dir: str):
        """会話マネージャを構築する。

        Args:
            config: sa-ru の脳モデル・会話タイムアウト・確定タスク/着手確認の出力先を含む設定。
            slack_notifier: 要約や着手確認ボタンを人間へ提示する通知手段。
            task_dir: 会話から確定したタスクを書き出す先（dispatcher が走査して実行に回す）。
        """
        self.config = config
        self.model = config["sa-ru"]["model"]              # 脳（現 qwen3:8b、将来 Gemma 4 12B）
        self.timeout = config["sa-ru"].get("converse_timeout_sec", 120)
        self.slack = slack_notifier
        self.task_dir = task_dir                            # 確定タスクの書き出し先（dispatcher が走査）
        self.confirm_dir = config["exec_confirm"]["dir"]    # 着手確認レコードの dir
        os.makedirs(self.confirm_dir, exist_ok=True)
        # 会話プロンプトは静的なので起動時に 1 度だけ読む（毎ターンの disk I/O を避ける）
        self._prompt_template = (PROMPTS_DIR / "converse.md").read_text()
        # 一定時間使われない会話を捨てる TTL（常駐プロセスでの session 無制限増加を防ぐ）
        self.session_ttl_sec = config.get("conversation", {}).get("session_ttl_sec", 3600)
        # conversation_id → [{"role": "user"|"assistant", "text": str}, ...]
        self.sessions: dict[str, list[dict]] = {}
        # conversation_id → 最終アクセス時刻（monotonic 秒）。エビクション判定に使う
        self._last_seen: dict[str, float] = {}

    # ── 会話処理（会話ループから to_thread で呼ばれる：脳 LLM は同期ブロック） ──

    def _evict_idle_sessions(self, now: float):
        """TTL を超えて使われていない会話セッションを破棄する（メモリ無制限増加の防止）。"""
        stale = [c for c, seen in self._last_seen.items() if now - seen > self.session_ttl_sec]
        for c in stale:
            self.sessions.pop(c, None)
            self._last_seen.pop(c, None)

    def handle_message(self, msg: dict):
        """1 件の発話を処理する。会話継続なら返信、意図が固まれば着手確認を提示する。"""
        cid = msg["conversation_id"]
        now = time.monotonic()
        self._evict_idle_sessions(now)
        self._last_seen[cid] = now
        history = self.sessions.setdefault(cid, [])
        history.append({"role": "user", "text": msg["text"]})
        # 履歴は直近 MAX_HISTORY_TURNS に丸める（プロンプト肥大・KV キャッシュ膨張防止）
        if len(history) > MAX_HISTORY_TURNS:
            del history[:-MAX_HISTORY_TURNS]

        if msg.get("force_ready"):
            # /taka-ma-go: LLM 判定を待たず要約させて強制的に締める
            result = self._invoke_llm(history, force=True)
            result["ready"] = True
        else:
            result = self._invoke_llm(history, force=False)

        if result.get("ready") and result.get("summary"):
            summary = result["summary"]
            history.append({"role": "assistant", "text": summary})
            self._present_summary(msg, summary)
        else:
            reply = result.get("reply") or "（応答を生成できませんでした。もう一度お願いします）"
            history.append({"role": "assistant", "text": reply})
            self.slack.notify(
                reply, msg.get("channel_id"),
                team_id=msg.get("team_id"), thread_ts=msg.get("thread_ts"),
            )

    def _invoke_llm(self, history: list[dict], force: bool) -> dict:
        """脳 LLM（sa-ru.model）を呼び、{reply, ready, summary} を返す。

        パース失敗時は会話継続（ready=false）にフォールバックし、素の stdout を返信に回す
        （安全側: 解釈できない出力で勝手に実行へ進めない）。force=True は要約を促す指示を足す。
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
            stdout = run_ollama(self.model, prompt, timeout=self.timeout)
            # gemma4:12b は json.loads が失敗する ```json フェンス付きで出力することがある
            # （実機検証で再現・2026-07-04）。ai_gateway 側 classifier/decomposer と同じ
            # extract_json でフェンス除去してからパースする（同根の欠陥・§9.2 と同一パターン）。
            parsed = json.loads(extract_json(stdout))
            return {
                "reply": parsed.get("reply", ""),
                "ready": bool(parsed.get("ready", False)),
                "summary": parsed.get("summary"),
            }
        except json.JSONDecodeError:
            # JSON 化できない出力は会話継続に回す（解釈できない出力で実行へ進めない）
            return {"reply": (stdout or "").strip(), "ready": False, "summary": None}
        except Exception:
            logger.exception("会話 LLM 呼び出し失敗")
            return {"reply": "（内部エラーが発生しました）", "ready": False, "summary": None}

    def _present_summary(self, msg: dict, summary: str):
        """着手確認レコード（status=pending）を作り、要約 + ボタンを Slack に提示する。"""
        exec_request_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record = {
            "exec_request_id": exec_request_id,
            "conversation_id": msg["conversation_id"],
            "summary": summary,
            "status": "pending",
            "user_id": msg.get("user_id", ""),
            "team_id": msg.get("team_id", ""),
            "channel_id": msg.get("channel_id", ""),
            "thread_ts": msg.get("thread_ts"),
            "created_at": now,
            "decided_at": None,
            "decided_by": None,
        }
        path = os.path.join(self.confirm_dir, f"{exec_request_id}.json")
        with open(path, "w") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

        self.slack.send_exec_confirm_request(
            exec_request_id, summary,
            channel=msg.get("channel_id"), team_id=msg.get("team_id"),
            thread_ts=msg.get("thread_ts"),
        )

    # ── 着手確認の決着（確認ループから呼ばれる） ──

    def create_exec_task(self, record: dict) -> str:
        """確認済み要約から確定タスク（status=init）を生成する。生成した task_id を返す。

        u-zu の task_queue.enqueue_task と同じ §8.3 タスク形式。source="conversation"、
        command は生文ではなく sa-ru が固めた構造化要約（責任分界の移動）。
        """
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        task_id = str(uuid.uuid4())
        task = {
            "task_id": task_id,
            "status": "init",
            "source": "conversation",
            "command": record["summary"],
            "user_id": record.get("user_id", ""),
            "team_id": record.get("team_id", ""),
            "channel_id": record.get("channel_id", ""),
            "thread_ts": record.get("thread_ts"),
            "created_at": now,
            "updated_at": now,
        }
        os.makedirs(self.task_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
        path = os.path.join(self.task_dir, f"{ts}_{task_id}.json")
        with open(path, "w") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
        self.slack.notify(
            "着手します。実行を開始しました。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"),
        )
        return task_id

    def notify_rejected(self, record: dict):
        """やり直し選択時。実行はせず会話継続を促す（履歴は維持される）。"""
        self.slack.notify(
            "やり直します。続けて指示してください。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"),
        )

    def notify_timeout(self, record: dict):
        """着手確認が期限切れ。実行はせず、必要なら締め直しを促す。"""
        self.slack.notify(
            "着手確認がタイムアウトしました。続ける場合はもう一度指示してください。",
            record.get("channel_id"), team_id=record.get("team_id"),
            thread_ts=record.get("thread_ts"),
        )
