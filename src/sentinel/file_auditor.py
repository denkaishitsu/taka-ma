"""qu-e File Auditor — ファイルシステム変更の監査（A1 §1〜§4 / 設計書 §8.12）。

構築手順書: docs/procedures/07-sentinel.md Step 5
- watchdog（FSEvents）でファイル変更を即時検知
- 静的フィルタ（ignore_patterns ∪ .gitignore mtime キャッシュ）
- 同パス連続変更を debounce_sec で集約（A1 §1 ノイズ抑制）
- qu-e LLM が approve/deny/escalate を判定
- jsonl 追記（A1 §4: id フィールド、日付別ファイル、retention 90 日）
- deny/escalate を sa-ru に SSH push（§8.12）
"""

import asyncio
import datetime
import json
import logging
import os
import subprocess
import uuid
from fnmatch import fnmatch
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("qu-e.file_auditor")


class GitignoreCache:
    """`.gitignore` のパターン列を mtime ベースでキャッシュする。

    ファイル変更は高頻度で発生するため、判定のたびに `.gitignore` を読み直すと
    ディスク I/O が嵩む。最終更新時刻（mtime）が前回と同じならパース済みリストを
    再利用し、編集された時だけ読み直すことで監査ホットパスを軽くする（A1 §1）。
    """

    def __init__(self):
        """空のキャッシュで初期化する。"""
        # gitignore パス → (読み込み時の mtime, パース済みパターン列) のキャッシュ
        self._cache: dict[str, tuple[float, list[str]]] = {}

    def get_patterns(self, gitignore_path: str) -> list[str]:
        """`.gitignore` の有効パターン列を返す（コメント・空行は除外）。

        mtime がキャッシュと一致すれば再パースせずキャッシュを返す。ファイルが
        存在しない場合は空リスト（無視パターン無し）を返す。
        """
        # ファイルが無ければ無視ルール無しとして扱う
        if not os.path.exists(gitignore_path):
            return []
        # mtime が前回と同じなら内容は不変とみなしキャッシュを使い回す
        mtime = os.path.getmtime(gitignore_path)
        cached = self._cache.get(gitignore_path)
        if cached and cached[0] == mtime:
            return cached[1]
        # 初回 or 編集後: 実ファイルをパース（空行・コメント行は無視パターンに含めない）
        with open(gitignore_path) as f:
            patterns = [line.strip() for line in f
                        if line.strip() and not line.strip().startswith("#")]
        self._cache[gitignore_path] = (mtime, patterns)
        return patterns


class FileAuditHandler(FileSystemEventHandler):
    """ファイル変更を即時検知し、debounce 経由で qu-e LLM 判定 → sa-ru に SSH push する。"""

    def __init__(self, config: dict, reviewer, task_context_store: dict, loop: asyncio.AbstractEventLoop):
        """監査ハンドラを構築する。

        Args:
            config: file_audit セクションを含む設定 dict（無視パターン・debounce 秒・
                ログ/アラート出力先・sa-ru への SSH ホスト等）。
            reviewer: 変更を approve/deny/escalate 判定する qu-e LLM レビューア。
            task_context_store: sa-ru が共有する {task_id: タスク情報} の参照。
                変更パスを実行中タスクへ紐付け、誰の作業による変更かを判定するのに使う。
            loop: watchdog（別スレッド）から asyncio へ橋渡しするためのイベントループ。
        """
        self.config = config
        self.reviewer = reviewer
        # sa-ru と共有するタスク文脈。値は {command, channel_id, status, workspace, thread_ts} 等
        self.task_context = task_context_store
        # watchdog はワーカースレッドでコールバックするため、本ループに投げ直して async 化する
        self.loop = loop
        # file_audit 設定の取り出し（無視ルール・集約待ち時間・出力先・通知先）
        self.ignore_patterns: list[str] = config["file_audit"].get("ignore_patterns", [])
        # sa-ru が SSH push する qu-e 自身の制御プレーンディレクトリ（§8.13）。固定の絶対パスが
        # 既知なので、ignore_patterns のベアネーム fnmatch（"task-context" という名前の
        # ユーザー成果物まで誤って除外し得る）ではなく、このパス配下かどうかで厳密に判定する
        # （Layer3 review で指摘・是正）。
        self.task_context_dir: str | None = config.get("task_context", {}).get("dir")
        self.debounce_sec: float = config["file_audit"].get("debounce_sec", 1)
        self.log_dir: str = config["file_audit"]["log_dir"]
        self.alert_dir: str = config["file_audit"]["o_moi_alert_dir"]
        self.ssh_host: str = config["file_audit"]["mac_mini_host"]
        self._gitignore = GitignoreCache()
        # debounce 用: path → 集約中タイマー。同パスの連続変更を 1 回の監査にまとめる
        self._pending: dict[str, asyncio.TimerHandle] = {}
        # ログ出力先は起動時に作っておく（初回書き込みで失敗しないように）
        os.makedirs(self.log_dir, exist_ok=True)

    def _should_ignore(self, path: str) -> bool:
        """このパスを監査対象から除外すべきか判定する。

        静的フィルタの第一段（A1 §1）。設定の固定無視パターンに加え、変更パスの属する
        リポジトリの `.gitignore` を合わせて適用する。パスの各構成要素（ディレクトリ名・
        ファイル名）のいずれかがいずれかのパターンに合致したら除外する。
        """
        # qu-e/sa-ru 間の内部通信ディレクトリは既知の絶対パスなので、パス prefix で厳密に
        # 除外する（ベアネーム fnmatch だと同名のユーザー成果物まで巻き込むため対象外）。
        if self.task_context_dir and (
            path == self.task_context_dir
            or path.startswith(self.task_context_dir.rstrip(os.sep) + os.sep)
        ):
            return True
        # パスを構成要素に分解し、各要素単位でパターン照合する（中間ディレクトリの除外も拾うため）
        parts = path.split(os.sep)
        # 固定の無視パターン ∪ そのリポジトリの .gitignore
        patterns = list(self.ignore_patterns)
        gitignore = self._find_gitignore(path)
        if gitignore:
            patterns.extend(self._gitignore.get_patterns(gitignore))
        # いずれかの構成要素がいずれかのパターンに合致すれば除外
        for pattern in patterns:
            for part in parts:
                if fnmatch(part, pattern):
                    return True
        return False

    def _find_gitignore(self, path: str) -> str | None:
        """path から親ディレクトリを辿って .gitignore を探す（最初に見つかったもの）。"""
        p = Path(path).parent
        while p != p.parent:
            gi = p / ".gitignore"
            if gi.exists():
                return str(gi)
            p = p.parent
        return None

    # watchdog のイベントコールバック群。種別を文字列に正規化して共通処理へ集約する。
    def on_modified(self, event):
        """ファイル更新イベント（watchdog からワーカースレッドで呼ばれる）。"""
        self._on_event(event, "modified")

    def on_created(self, event):
        """ファイル作成イベント（watchdog からワーカースレッドで呼ばれる）。"""
        self._on_event(event, "created")

    def on_deleted(self, event):
        """ファイル削除イベント（watchdog からワーカースレッドで呼ばれる）。"""
        self._on_event(event, "deleted")

    def _on_event(self, event, event_type: str):
        """全イベント共通の入口。除外判定 → debounce 登録までを行う。

        watchdog のワーカースレッドから呼ばれるため、ここでは重い処理をせず
        `call_soon_threadsafe` でイベントループ側に橋渡しするに留める。
        """
        # ディレクトリ自体の変更や無視対象パスは監査しない
        if event.is_directory or self._should_ignore(event.src_path):
            return
        # debounce: 同パスに集約待ちタイマーが居れば取り消して張り直す（連続保存を 1 回に集約）
        timer = self._pending.pop(event.src_path, None)
        if timer:
            timer.cancel()
        # スレッド境界を越えてループ側でタイマーを仕掛ける
        self._pending[event.src_path] = self.loop.call_soon_threadsafe(
            lambda: self._schedule_audit(event.src_path, event_type)
        )

    def _schedule_audit(self, path: str, event_type: str):
        """debounce_sec 後に `_audit` を発火する asyncio タイマーを仕掛ける。

        イベントループ上で実行される。待機中に同パスの新たな変更が来れば
        `_on_event` がこのタイマーを取り消して張り直すため、最後の変更から
        debounce_sec 静かになって初めて 1 回だけ監査が走る。
        """
        timer = self.loop.call_later(
            self.debounce_sec,
            lambda: asyncio.create_task(self._audit(path, event_type)))
        self._pending[path] = timer

    async def _audit(self, path: str, event_type: str):
        """1 件の確定変更を監査する本体（debounce 後にイベントループ上で実行）。

        変更を実行中タスクへ紐付けて文脈を集め、qu-e LLM に判定させ、結果を監査ログに
        残し、要対応のものだけ sa-ru へ通知する、という一連を行う。途中で例外が出ても
        監査ループ全体は止めず、ログに残して握り潰す。
        """
        # このパスの集約は完了。タイマー台帳から外す
        self._pending.pop(path, None)
        try:
            # 変更パスが属する実行中タスクを特定し、文脈（指示文・通知先・状態）を引き当てる（§8.13）。
            # 紐付かない場合は値を既定（空 / "none"）に倒して匿名の変更として続行する。
            task = self._pick_task_context(path)
            command = task.get("command", "") if task else ""
            status = task.get("status", "none") if task else "none"
            channel_id = task.get("channel_id", "") if task else ""
            team_id = task.get("team_id", "") if task else ""
            thread_ts = task.get("thread_ts") if task else None
            task_id = task.get("task_id", "") if task else ""

            # 変更内容の要約を作り、それを材料に qu-e LLM へ approve/deny/escalate を判定させる
            diff = self._compute_diff_summary(path, event_type)
            result = await self.reviewer.review_file_audit(path, diff, command, status)

            # 監査レコードを組み立てて日付別 jsonl に追記（id は後段の人間承認と突き合わせる鍵）
            audit_id = uuid.uuid4().hex
            record = {
                "id": audit_id,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "event": event_type,
                "path": path,
                "task_id": task_id,
                "command": command,
                "status": status,
                **result,                # decision / reason / confidence / diff_summary
            }
            self._append_jsonl(record)

            # approve は記録のみで完結。要対応（deny/escalate）だけ sa-ru に通知する（A1 §2）
            if result.get("decision") in ("deny", "escalate"):
                self._push_alert_to_o_moi(audit_id, record, channel_id, thread_ts, team_id)
        except Exception:
            # 1 件の失敗で監視を止めない。原因追跡のためスタックトレースは残す
            logger.exception("file_audit 処理失敗: path=%s", path)

    def _pick_task_context(self, path: str) -> dict | None:
        """変更パスが属するタスクを workspace 接頭辞で特定する（§8.13、複数並行対応）。

        各タスクは専用作業ディレクトリ（`workspace`）で実行される。
        変更パスがいずれかの workspace 配下なら、その task_id を返す（最長一致優先）。
        接頭辞一致が無い場合: in_progress が 1 件だけならそれを返す（後方互換）。
        複数あって特定できないときは None を返す（誤帰属を避け、フォールバック通知に委ねる）。
        """
        # 帰属候補は実行中タスクのみ（完了済み・待機中の作業ディレクトリには紐付けない）
        in_progress = [t for t in self.task_context.values() if t.get("status") == "in_progress"]

        # 変更パスを各タスクの workspace 接頭辞と突き合わせ、最も深く（長く）一致するものを選ぶ。
        # 入れ子の workspace でも「より具体的な方」のタスクへ正しく帰属させるための最長一致。
        norm = os.path.abspath(path)
        matched, matched_len = None, -1
        for task in in_progress:
            ws = task.get("workspace")
            if not ws:
                continue
            ws_abs = os.path.abspath(ws)
            if norm == ws_abs or norm.startswith(ws_abs + os.sep):
                if len(ws_abs) > matched_len:
                    matched, matched_len = task, len(ws_abs)
        if matched:
            return matched

        # workspace で特定できないとき: 実行中が 1 件だけなら従来挙動でそれに帰属。
        # 複数あると誤帰属しうるので帰属せず None（後段はフォールバック通知に委ねる）。
        return in_progress[0] if len(in_progress) == 1 else None

    def _compute_diff_summary(self, path: str, event_type: str) -> str:
        """LLM 判定に渡す変更内容の要約を作る。

        可能なら `git diff --stat` で実際の変更規模を要約する。git 管理外・git 不在・
        タイムアウト等で取れない場合は「種別: パス」だけの最小要約に縮退する（判定は
        止めない）。削除は diff が取れないため種別とパスのみ。
        """
        # 削除済みファイルは diff を取りようがないので種別＋パスで即返す
        if event_type == "deleted":
            return f"deleted: {path}"
        try:
            # 変更ファイルのあるディレクトリで git diff。stat（増減行数の要約）のみ取得する
            result = subprocess.run(
                ["git", "diff", "--stat", "--", path],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(path) or ".")
            # diff が空（未追跡・ステージ外等）でも最低限の要約は返す
            return (result.stdout or f"{event_type}: {path}").strip()
        except Exception:
            # git が無い/失敗しても監査は続行。最小要約に縮退する
            return f"{event_type}: {path}"

    def _append_jsonl(self, record: dict):
        """監査レコードを当日分の jsonl ファイルに 1 行追記する（A1 §4）。

        ファイルは日付別（file-audit-YYYY-MM-DD.jsonl）に分け、retention rotation で
        古い日付ごと削除できるようにしている。日本語を保持するため ensure_ascii=False。
        """
        date = datetime.date.today().isoformat()
        path = os.path.join(self.log_dir, f"file-audit-{date}.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _push_alert_to_o_moi(self, audit_id: str, record: dict, channel_id: str,
                             thread_ts: str | None, team_id: str = ""):
        """要対応（deny/escalate）の監査結果を sa-ru へアラートとして届ける（§8.12）。

        qu-e と sa-ru は別マシンのため、sa-ru の alert_dir に JSON ファイルを SSH 経由で
        書き込む（sa-ru 側がそれを拾って Slack の人間承認へ回す）。通知先を復元できるよう
        audit ログ id・チャンネル・ワークスペース（team_id）・スレッドも同梱する。
        push 失敗は監査本体を巻き込まないようログのみで握り潰す。
        """
        # sa-ru 側が承認フローを組み立てるのに必要な一式をまとめる
        payload = {
            "audit_log_id": audit_id,
            "task_id": record.get("task_id", ""),
            "path": record["path"],
            "decision": record["decision"],
            "reason": record["reason"],
            "confidence": record.get("confidence", 0.0),
            "diff_summary": record.get("diff_summary", ""),
            "command": record.get("command", ""),
            "status": record.get("status", "none"),
            "channel_id": channel_id,
            "team_id": team_id,
            "thread_ts": thread_ts,
        }
        # alert_dir を作ってから標準入力で JSON を流し込む（1 コマンドで mkdir + 書き込み）。
        # ファイル名を audit_id にすることで sa-ru 側が監査ログと突き合わせられる。
        remote = f"{self.alert_dir}/{audit_id}.json"
        try:
            subprocess.run(
                ["ssh", self.ssh_host, f"mkdir -p {self.alert_dir} && cat > {remote}"],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True, text=True, timeout=10, check=True)
        except Exception:
            # 通知が落ちても監査記録（jsonl）は残っている。原因追跡のためログのみ残す
            logger.exception("file_audit alert push 失敗: audit_id=%s", audit_id)


def start_audit(config: dict, reviewer, task_context_store: dict,
                loop: asyncio.AbstractEventLoop) -> Observer:
    """ファイル監査を開始し、稼働中の watchdog Observer を返す。

    設定の watch_paths を再帰監視し、各変更を FileAuditHandler へ流す。返した
    Observer は呼び出し側が保持し、停止時に stop/join する想定。
    """
    handler = FileAuditHandler(config, reviewer, task_context_store, loop)
    observer = Observer()
    # 設定された監視対象ルートをすべて再帰的に登録する
    for path in config["file_audit"]["watch_paths"]:
        observer.schedule(handler, path, recursive=True)
    observer.start()
    return observer


def rotate_jsonl(log_dir: str, retention_days: int):
    """保持期間を過ぎた日付別監査ログを削除する（A1 §4、retention 90 日運用）。

    ファイル名の日付部分（file-audit-YYYY-MM-DD.jsonl）を基準に判定する。命名規則に
    合わないファイルや日付として解釈できないものは対象外として触れない。
    """
    # 今日から retention_days 遡った日付。これより古い日付のログを削除対象とする
    cutoff = datetime.date.today() - datetime.timedelta(days=retention_days)
    for f in os.listdir(log_dir):
        # 監査ログの命名規則に合致しないファイルは無関係なので飛ばす
        if not (f.startswith("file-audit-") and f.endswith(".jsonl")):
            continue
        # ファイル名から日付を取り出す。解釈不能なら安全側でスキップ（消さない）
        try:
            date_str = f[len("file-audit-"):-len(".jsonl")]
            date = datetime.date.fromisoformat(date_str)
        except ValueError:
            continue
        # 保持期限より古いものだけ削除。削除失敗は致命ではないのでログに留める
        if date < cutoff:
            try:
                os.remove(os.path.join(log_dir, f))
                logger.info("retention rotation: removed %s", f)
            except OSError:
                logger.exception("rotation 削除失敗: %s", f)
