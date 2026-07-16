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
import re
import shutil
import subprocess
import threading
import uuid
from fnmatch import fnmatch
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("qu-e.file_auditor")

# ワイルドカード・区切りのみで構成される「過大パターン」（例: `*`、`**`、`/*`）。
# .gitignore にこの 1 行があるだけでリポジトリ全体が監査対象から消える（監査バイパス）
# ため適用しない（§8.12「.gitignore の適用限界」）。`.` は literal なので `.*`（dotfile
# 除外）等の実用パターンは対象外。
_BROAD_PATTERN_RE = re.compile(r"\A[*?/\[\]]+\Z")

# LLM 判定に渡す diff の上限文字数（§8.12「diff 要約」）。巨大 diff によるプロンプト肥大
# （ollama コンテキスト溢れ・判定劣化）を防ぐ。超過分は切り詰めて明示する。
_DIFF_MAX_CHARS = 4000


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
        # 初回 or 編集後: 実ファイルをパース（空行・コメント行は無視パターンに含めない）。
        # 過大パターンの排除は再パース時（低頻度）に行い、判定ホットパスに載せない。
        with open(gitignore_path) as f:
            patterns = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 過大パターン（ワイルドカードのみ）は監査バイパスになるため適用しない
                # （§8.12）。`!` 否定は監査を増やす方向なので過大でも保持してよい。
                if not line.startswith("!") and _BROAD_PATTERN_RE.match(line):
                    logger.warning("過大 .gitignore パターンを不適用: %r (%s)",
                                   line, gitignore_path)
                    continue
                patterns.append(line)
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
        # file_audit 設定の取り出し（無視ルール・集約待ち時間・出力先・通知先）。
        # いずれも qu-e.yaml を唯一の源にする（コード既定値なし。欠落は起動時に KeyError で
        # 即落とし診断位置を揃える）
        self.ignore_patterns: list[str] = config["file_audit"]["ignore_patterns"]
        # sa-ru が SSH push する qu-e 自身の制御プレーンディレクトリ（§8.13）。固定の絶対パスが
        # 既知なので、ignore_patterns のベアネーム fnmatch（"task-context" という名前の
        # ユーザー成果物まで誤って除外し得る）ではなく、このパス配下かどうかで厳密に判定する
        # （Layer3 review で指摘・是正）。
        self.task_context_dir: str = config["task_context"]["dir"]
        # sa-ru が worker 起動のたびに workspace へ配置する制御ファイル（PreToolUse フック設定
        # 等）。外部改変ではなく sa-ru 自身が毎タスク生成・上書きする自己生成物であり、監査対象に
        # すると全タスクで同一パスの escalate/deny アラートを量産する。ファイル名が固定・既知の
        # システム制御プレーンとして basename で除外する（§8.12 システム制御プレーンの除外）。
        self.control_plane_files: list[str] = config["file_audit"]["control_plane_files"]
        self.debounce_sec: float = config["file_audit"]["debounce_sec"]
        self.log_dir: str = config["file_audit"]["log_dir"]
        self.alert_dir: str = config["file_audit"]["o_moi_alert_dir"]
        self.ssh_host: str = config["file_audit"]["mac_mini_host"]
        self._gitignore = GitignoreCache()
        # debounce 用: path → 集約中タイマー。同パスの連続変更を 1 回の監査にまとめる
        self._pending: dict[str, asyncio.TimerHandle] = {}
        # debounce ウィンドウ内で同パスに届いた event 種別の集約結果（§8.12 原子的書き込みの集約）。
        # atomic write（tmp→本体 rename／本体削除→再作成）で delete と create/moved が対で
        # 来たとき、本体パスの「削除」アラート化を防ぐため modify に集約する。
        self._agg_event: dict[str, str] = {}
        # ログ出力先は起動時に作っておく（初回書き込みで失敗しないように）
        os.makedirs(self.log_dir, exist_ok=True)

    def _should_ignore(self, path: str) -> bool:
        """このパスを監査対象から除外すべきか判定する。

        静的フィルタの第一段（A1 §1）。設定の固定無視パターンに加え、変更パスの属する
        リポジトリの `.gitignore` を合わせて適用する。パスの各構成要素（ディレクトリ名・
        ファイル名）のいずれかがいずれかのパターンに合致したら除外する。
        `!` 否定（再包含）は「一致したパスは除外しない＝監査する」と安全側で近似する（§8.12）。
        """
        # qu-e/sa-ru 間の内部通信ディレクトリは既知の絶対パスなので、パス prefix で厳密に
        # 除外する（ベアネーム fnmatch だと同名のユーザー成果物まで巻き込むため対象外）。
        if self.task_context_dir and (
            path == self.task_context_dir
            or path.startswith(self.task_context_dir.rstrip(os.sep) + os.sep)
        ):
            return True
        # sa-ru が workspace に配る制御ファイル（`.taka-hook-settings.json` 等）は自己生成物で
        # 監査対象外。パスは workspace（task_id 可変）配下だがファイル名は固定・既知のため
        # basename で判定する（§8.12）。この名前のユーザー成果物はまず無く、パス固定・既知
        # ゆえ「除外ルール書き換えこそ監査」思想（下記 .gitignore）とは競合しない。
        if os.path.basename(path) in self.control_plane_files:
            return True
        # `.gitignore` 自身の変更は静的フィルタの対象外＝常に監査する。除外ルールを
        # 書き換える変更（例: `*` を書き込んで以降を無監査化する）こそ監査対象（§8.12）。
        if os.path.basename(path) == ".gitignore":
            return False
        # パスを構成要素に分解し、各要素単位でパターン照合する（中間ディレクトリの除外も拾うため）
        parts = path.split(os.sep)
        # 固定の無視パターン ∪ そのリポジトリの .gitignore
        patterns = list(self.ignore_patterns)
        gitignore = self._find_gitignore(path)
        if gitignore:
            patterns.extend(self._gitignore.get_patterns(gitignore))
        # `!` 否定（再包含）を分離。否定に一致したパスは除外せず監査する（監査を増やす
        # 方向の近似。gitignore 本来の last-match-wins は実装しない）。
        negations = [p[1:] for p in patterns if p.startswith("!") and len(p) > 1]
        positives = [p for p in patterns if not p.startswith("!")]
        for pattern in negations:
            for part in parts:
                if fnmatch(part, pattern):
                    return False
        # いずれかの構成要素がいずれかのパターンに合致すれば除外
        for pattern in positives:
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

    def on_moved(self, event):
        """リネーム/移動イベント。移動先を新規変更として監査し、移動元は削除として扱う（§8.12）。

        無視される名前（一時ファイル拡張子等）で作成してから目的パスへ rename する変更は
        on_created/on_modified に映らないため、moved を実装しないと検知ゼロで素通りする。
        移動先・移動元それぞれに除外判定を適用する。
        """
        if event.is_directory:
            return
        self._submit(getattr(event, "dest_path", None), "moved")
        self._submit(event.src_path, "deleted")

    def _on_event(self, event, event_type: str):
        """created/modified/deleted 共通の入口（watchdog ワーカースレッド側）。"""
        if event.is_directory:
            return
        self._submit(event.src_path, event_type)

    def _submit(self, path: str, event_type: str):
        """1 パスの変更を除外判定して debounce へ投入する（全イベント種別の共通口）。

        watchdog のワーカースレッドから呼ばれる。ここでは debounce 台帳（_pending）に
        一切触れず、`call_soon_threadsafe` でイベントループへ橋渡しするだけに留める。
        台帳の取り消し・登録を `_debounce`（ループ上）に直列化することで、別スレッド
        からの pop/cancel 競合による二重監査・取り消し漏れを防ぐ（§8.12 ノイズ抑制）。
        """
        if not path or self._should_ignore(path):
            return
        self.loop.call_soon_threadsafe(self._debounce, path, event_type)

    def _debounce(self, path: str, event_type: str):
        """debounce 台帳を更新し、debounce_sec 後に `_audit` を発火するタイマーを張り直す。

        イベントループ上でのみ実行される（台帳アクセスが単一スレッドに閉じるため
        ロック不要）。待機中に同パスの新たな変更が来ればタイマーを取り消して張り直す
        ため、最後の変更から debounce_sec 静かになって初めて 1 回だけ監査が走る。
        event 種別はウィンドウ内で集約し（`_merge_event`）、atomic write の delete↔create
        共起を modify に畳んでから監査する（§8.12 原子的書き込みの集約）。
        """
        self._agg_event[path] = self._merge_event(self._agg_event.get(path), event_type)
        timer = self._pending.pop(path, None)
        if timer:
            timer.cancel()
        self._pending[path] = self.loop.call_later(
            self.debounce_sec,
            lambda: asyncio.create_task(self._audit(path)))

    @staticmethod
    def _merge_event(prev: str | None, new: str) -> str:
        """debounce ウィンドウ内の同一パスの event 種別を集約する（§8.12 原子的書き込みの集約）。

        atomic write（一時ファイル→本体 rename、または本体削除→再作成）では、同じ本体パスに
        delete と create/moved がウィンドウ内で対で届く。これを素朴に「最後のイベント」で判定
        すると、順序次第で正当な保存が本体パスの **削除** アラートに化ける。そこで「delete と
        **新規実体の出現**（created / moved）がウィンドウ内で共起したら modify に集約」する。
        modified は「存在の出現」ではなく既存実体の更新なので集約対象に含めない——含めると
        「編集→即削除」（modified→deleted）まで modify に畳み、**真の削除を隠す**ため。delete が
        create/moved と対にならなければ（真の削除、または編集後削除）**削除として残す**。
        検知回避（無視名で作成→監査名へ rename）は移動先パスの監査で従来どおり担保される。
        """
        # 「新規実体の出現」＝ atomic write でファイルが実在に戻るイベント。modified は含めない。
        appears = ("created", "moved")
        if prev is None:
            return new
        # delete と新規実体出現の共起 → 更新（modify）に畳む。削除アラート化を防ぐ。
        if "deleted" in (prev, new) and (prev in appears or new in appears):
            return "modified"
        # それ以外は最新種別を採用（両 deleted・modified→deleted 等は削除/更新の意味論を保つ）。
        return new

    async def _audit(self, path: str):
        """1 件の確定変更を監査する本体（debounce 後にイベントループ上で実行）。

        変更を実行中タスクへ紐付けて文脈を集め、qu-e LLM に判定させ、結果を監査ログに
        残し、要対応のものだけ sa-ru へ通知する、という一連を行う。途中で例外が出ても
        監査ループ全体は止めず、ログに残して握り潰す。event 種別は debounce ウィンドウ内で
        集約済みの値を使う（§8.12、atomic write の削除誤検知はここに届く前に modify へ畳まれる）。
        """
        # このパスの集約は完了。タイマー台帳と集約 event 台帳から外す
        self._pending.pop(path, None)
        event_type = self._agg_event.pop(path, "modified")
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

            # 監査レコードを組み立てて日付別 jsonl に追記（id は後段の人間承認と突き合わせる鍵）。
            # `**result` を先に展開し固定キーを後で上書きすることで、LLM 応答由来の値が
            # 識別・突合キー（id/path/task_id/timestamp/event 等）を汚染するのを防ぐ（固定キー保全）。
            audit_id = uuid.uuid4().hex
            record = {
                **result,                # decision / reason / confidence / diff_summary
                "id": audit_id,
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "event": event_type,
                "path": path,
                "task_id": task_id,
                "command": command,
                "status": status,
            }
            self._append_jsonl(record)

            # fail-closed: approve と明示されたときのみ記録で完結し、それ以外（deny/escalate/
            # 想定外の値）はすべて sa-ru へ通知して人間確認に回す。reviewer 側で decision は
            # approve/deny/escalate に正規化済みだが、ここでも「approve 以外は通知」を基準にする。
            if record.get("decision") != "approve":
                self._push_alert_to_o_moi(audit_id, record, channel_id, thread_ts, team_id)
        except Exception:
            # 1 件の失敗で監視を止めない。原因追跡のためスタックトレースは残す
            logger.exception("file_audit 処理失敗: path=%s", path)
            # fail-closed: 監査できなかった変更を無音で通さない。判定を得られなかった事実を
            # 最小情報で人間へ escalate 通知する（監視が沈黙したまま危険変更が通る経路を塞ぐ）。
            self._push_audit_failure_alert(path, event_type)

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
        """LLM 判定に渡す変更内容の diff を作る（§8.12「diff 要約」）。

        可能なら `git diff`（変更内容を含む）で実際の変更を取る。件数のみの `--stat` では
        qu-e が判定材料ゼロで審査することになるため内容を渡し、肥大化は _DIFF_MAX_CHARS で
        切り詰める。git 管理外・git 不在・タイムアウト等で取れない場合は「種別: パス」の
        最小要約に縮退する（判定は止めない）。削除は diff が取れないため種別とパスのみ。
        """
        # 削除済みファイルは diff を取りようがないので種別＋パスで即返す
        if event_type == "deleted":
            return f"deleted: {path}"
        try:
            # 変更ファイルのあるディレクトリで git diff（変更内容を含む本文を取得する）
            result = subprocess.run(
                ["git", "diff", "--", path],
                capture_output=True, text=True, timeout=5,
                cwd=os.path.dirname(path) or ".")
            diff = (result.stdout or "").strip()
            # diff が空（未追跡・コミット直後等）でも最低限の要約は返す
            if not diff:
                return f"{event_type}: {path}"
            # 巨大 diff はプロンプト肥大（コンテキスト溢れ・判定劣化）を防ぐため切り詰める
            if len(diff) > _DIFF_MAX_CHARS:
                diff = diff[:_DIFF_MAX_CHARS] + "\n...(truncated)"
            return diff
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
        # sa-ru 側が承認フローを組み立てるのに必要な一式をまとめる。
        # 各フィールドは .get で安全に取り出し、キー欠落で push 自体が KeyError で
        # 落ちる（＝アラート無音ロスト）ことを防ぐ（fail-closed）。
        payload = {
            "audit_log_id": audit_id,
            "task_id": record.get("task_id", ""),
            "path": record.get("path", ""),
            "decision": record.get("decision", "escalate"),
            "reason": record.get("reason", ""),
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

    def _push_audit_failure_alert(self, path: str, event_type: str, reason: str | None = None):
        """監査機構自体の失敗を人間へ escalate 通知する（fail-closed）。

        判定結果を得られなかった＝「危険かもしれない変更が監査を素通りした」状態のため、
        最小情報（対象パス・種別）で escalate アラートを組み立てて push する。通知先の
        文脈（channel/thread/team）は失敗時点で不明なので空にし、sa-ru 側の既定投稿先
        フォールバックに委ねる。この二次処理でさらに例外が出ても監査ループは止めない。
        監査処理の例外のほか、動的監視の登録失敗（§8.12。監視できないまま沈黙する状態）
        からも reason を差し替えて使う。
        """
        try:
            audit_id = uuid.uuid4().hex
            record = {
                "id": audit_id,
                "path": path,
                "decision": "escalate",
                "reason": reason or f"file_audit 処理が例外で失敗（event={event_type}）。人間確認が必要",
                "confidence": 0.0,
                "diff_summary": "",
                "task_id": "",
                "command": "",
                "status": "none",
            }
            # 監査証跡にも失敗を残す（残せなくても通知は試みる）
            try:
                self._append_jsonl(record)
            except Exception:
                logger.exception("失敗 escalate の jsonl 追記に失敗: audit_id=%s", audit_id)
            self._push_alert_to_o_moi(audit_id, record, channel_id="", thread_ts=None, team_id="")
        except Exception:
            logger.exception("失敗 escalate 通知の組み立てに失敗: path=%s", path)


class DynamicWatchManager:
    """実行中タスクの実開発リポジトリを動的に watchdog 監視へ登録・解除する（§8.12 動的監視）。

    watch_paths（静的ルート）は起動時に固定で監視するが、実開発リポジトリ
    （例: MBP ~/DevDev/xxx の git clone）は場所が事前に決まらない。task_context（§8.13）の
    workspace を受けて、タスク期間中（in_progress）だけ observer.schedule し、終了
    （completed/failed）で解除する。同一リポジトリを複数タスクが並行使用している間は
    解除しない（参照カウント）。symlink を静的ルート内へ張る方式は watchdog/FSEvents が
    symlink 先のイベントを検知しないため不可（2026-07-14 実測）で、本方式を採る。
    """

    def __init__(self, observer, handler, static_roots: list[str], commit_gate: dict):
        """動的監視マネージャを構築する。

        Args:
            observer: 稼働中の watchdog Observer（schedule/unschedule を呼ぶ）。
            handler: FileAuditHandler。動的登録先のイベントも静的ルートと同一の監査経路に
                流す。登録失敗の escalate 通知（fail-closed）にも使う。
            static_roots: 起動時から監視済みの watch_paths。この配下は登録済みのため
                動的登録しない（二重イベント防止）。
            commit_gate: qu-e.yaml file_audit.commit_gate（pre-commit フック自動導入の設定。
                install_hook キー必須=yaml が唯一の源、コード側に既定値なし）。
        """
        self.observer = observer
        self.handler = handler
        self._static_roots = [os.path.abspath(os.path.expanduser(p)) for p in static_roots]
        # install_hook は構築時（qu-e 起動時）に読み切る。ここで添字アクセスしておかないと、
        # yaml の commit_gate がコメントのみ（None）や install_hook 欠落のとき、最初の動的
        # 監視登録（_install_commit_hook）まで発覚が遅れ、フックだけ黙って未導入になる
        # （欠落は起動時に即落とす、の診断位置合わせ）
        self._install_hook: bool = commit_gate["install_hook"]
        # path → (ObservedWatch, そのパスを使用中の task_id 集合)。参照カウントの台帳
        self._watches: dict[str, tuple[object, set[str]]] = {}
        # task_id → 登録先 path（解除・付け替えの逆引き）
        self._task_path: dict[str, str] = {}
        # sync() は task_context Observer のワーカースレッドと起動時初期スキャン
        # （メインスレッド）の両方から呼ばれるため、台帳の更新を直列化する
        self._lock = threading.Lock()

    def sync(self, task_id: str, ctx: dict | None):
        """task_context 1 件の受信を動的監視へ反映する（登録・付け替え・解除の共通口）。

        ctx=None（終了系 status）または workspace 無しは解除。workspace が静的ルート
        配下なら登録不要（付け替えで静的圏内へ戻った場合の後始末として解除だけ行う）。
        """
        workspace = (ctx or {}).get("workspace")
        with self._lock:
            if not workspace:
                self._unregister(task_id)
                return
            path = os.path.abspath(os.path.expanduser(workspace))
            if self._covered_by_static(path):
                self._unregister(task_id)
                return
            prev = self._task_path.get(task_id)
            if prev == path:
                return
            if prev:
                self._unregister(task_id)
            self._register(task_id, path)

    def _covered_by_static(self, path: str) -> bool:
        """path が静的 watch_paths のいずれかの配下（既に監視済み）かを返す。"""
        for root in self._static_roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False

    def _register(self, task_id: str, path: str):
        """path の監視を登録する（登録済みなら参照カウントに task_id を足すだけ）。"""
        entry = self._watches.get(path)
        if entry:
            entry[1].add(task_id)
            self._task_path[task_id] = path
            return
        try:
            watch = self.observer.schedule(self.handler, path, recursive=True)
        except Exception:
            logger.exception("動的監視の登録失敗: path=%s task_id=%s", path, task_id)
            # fail-closed: 監視できないまま沈黙すると、このタスクの変更が無監査で通る。
            # その事実を人間へ escalate 通知する（§8.12 動的監視「登録失敗の fail-closed」）
            self.handler._push_audit_failure_alert(
                path, "watch",
                reason=f"実開発リポジトリの動的監視の登録に失敗（task_id={task_id}）。"
                       "この間の変更は監査されない。人間確認が必要")
            return
        self._watches[path] = (watch, {task_id})
        self._task_path[task_id] = path
        logger.info("動的監視 登録: path=%s task_id=%s", path, task_id)
        self._install_commit_hook(path)

    def _unregister(self, task_id: str):
        """task_id の登録を外し、参照が尽きたパスの監視を解除する。"""
        path = self._task_path.pop(task_id, None)
        if not path:
            return
        entry = self._watches.get(path)
        if not entry:
            return
        watch, users = entry
        users.discard(task_id)
        # タスク中に clone されたリポジトリ（登録時は .git 不在）にも導入できるよう、
        # タスク終了時にも pre-commit フック導入を試みる（§8.12 コミット前ゲート「導入」）
        self._install_commit_hook(path)
        if users:
            return
        try:
            self.observer.unschedule(watch)
            logger.info("動的監視 解除: path=%s", path)
        except Exception:
            logger.exception("動的監視の解除失敗: path=%s", path)
        self._watches.pop(path, None)

    def _install_commit_hook(self, path: str):
        """workspace が git リポジトリなら pre-commit 監査フックを自動導入する（§8.12）。

        既存フックは上書きしない（ユーザーのリポジトリ設定を壊さない）。導入の要否は
        qu-e.yaml file_audit.commit_gate.install_hook（yaml が唯一の源・コード既定値なし）。
        導入失敗はコミットゲートが掛からないだけで監査（watchdog）自体は生きているため、
        ログのみで継続する。
        """
        if not self._install_hook:
            return
        if not os.path.isdir(os.path.join(path, ".git")):
            return
        target = os.path.join(path, ".git", "hooks", "pre-commit")
        if os.path.exists(target):
            return
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "pre-commit")
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(src, target)
            os.chmod(target, 0o755)
            logger.info("pre-commit 監査フック導入: %s", target)
        except OSError:
            logger.exception("pre-commit フック導入失敗: %s", target)


def start_audit(config: dict, reviewer, task_context_store: dict,
                loop: asyncio.AbstractEventLoop) -> tuple[Observer, FileAuditHandler]:
    """ファイル監査を開始し、稼働中の watchdog Observer と監査ハンドラを返す。

    設定の watch_paths を再帰監視し、各変更を FileAuditHandler へ流す。返した
    Observer は呼び出し側が保持し、停止時に stop/join する想定。ハンドラは
    DynamicWatchManager（動的登録先へ同じ監査経路を流す）の構築に使う。
    """
    handler = FileAuditHandler(config, reviewer, task_context_store, loop)
    observer = Observer()
    # 設定された監視対象ルートをすべて再帰的に登録する
    for path in config["file_audit"]["watch_paths"]:
        observer.schedule(handler, path, recursive=True)
    observer.start()
    return observer, handler


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
