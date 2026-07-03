"""共有ファイルキュー — sa-ru の各待受ループのファイル取り回しを一元化する。

sa-ru は tasks / conversations / controls / exec-confirmations の 4 経路を「ディレクトリ配下の
*.json を監視 → 1 件ずつ取得 → 処理 → 退避」という同型のループで捌いている。これらは個別に
書かれ、列挙・パース・壊れファイル隔離・done/ 退避がコピーで増殖し、ドリフト（exec-confirmations
だけ壊れファイルを隔離しない 等）が生じていた。本クラスはその取り回し部分だけを集約し、各ループは
「どの status を ready とするか」「処理後にどうするか」という固有判断のみを持つ形にする。

待受方式は poll（定期 glob）を採る。これは設計上の確定事項で、これら 4 経路は「自分が決めた
場所に既知形式の JSON が置かれる受信キュー」であり、ポーリングが最適（負荷は実質ゼロ・大量到着に
強い・実装が単純）。watchdog（FSEvents）はファイル内容の外部改変検知（file_audit §8.12・
リソース通知 §8.14）に限定し、本 4 経路へは広げない。方式選択の判断基準と根拠は設計書 §8.15 を参照。

設計書 §8.3 のエラーハンドリング規定「壊れた/読めないファイルは当該 1 件のみ failed/ へ隔離し、
ループ全体は止めない」を全経路で一律に満たす（このクラスを通す＝隔離が必ず効く）。
"""

import asyncio
import datetime
import glob
import json
import logging
import os
import shutil
import uuid

logger = logging.getLogger("sa-ru.orchestrator")


class FileQueue:
    """1 ディレクトリ分のファイルキュー。done/ と failed/ を内部管理する。

    - iter_records(): ready 判定を呼び出し側に委ねる全件走査（scan-all 系＝exec-confirmations 用）
    - claim():        最初の ready 1 件を取得し、必要なら status を予約書換（pick-one 系）
    - run():          claim → handler → done の pick-one ループ（conversation / control 用）
    - mark_done() / quarantine(): 処理済み退避・壊れファイル隔離

    壊れた JSON / 読めないファイルは iter_records / claim の中で failed/ へ隔離して走査対象から外す
    （その場に残すと毎ポーリングで再 glob・再パースされ続け滞留するため）。
    """

    def __init__(self, directory: str, *, poll_interval: float, log: logging.Logger = logger):
        """ファイルキューを構築する。

        Args:
            directory: 監視する基底ディレクトリ。処理済みは done/、失敗は failed/ に退避する。
            poll_interval: ディレクトリを走査する間隔（秒）。
            log: 進捗・エラーの出力先ロガー。
        """
        self.dir = directory
        self.done_dir = f"{directory}/done"
        self.failed_dir = f"{directory}/failed"
        self.poll_interval = poll_interval
        self.log = log
        # 起動時に基底ディレクトリだけ用意する。done/ failed/ は実際に退避が発生したとき遅延作成する。
        os.makedirs(self.dir, exist_ok=True)

    # ── 取得 ──

    def iter_records(self):
        """dir 配下の *.json を名前順に列挙し、(path, record) を逐次返すジェネレータ。

        壊れた/読めないファイルは failed/ へ隔離してスキップする（呼び出し側へは渡さない）。
        ready 判定（status の解釈）や timeout 判定は呼び出し側が record を見て行う。
        """
        for path in sorted(glob.glob(f"{self.dir}/*.json")):
            record = self._read(path)
            if record is None:
                continue
            yield path, record

    def claim(self, ready_status: str, *, reserve_status: str | None = None) -> tuple[str, dict] | None:
        """status==ready_status の最初の 1 件を取得する。なければ None。

        reserve_status を指定すると、取得時に status を書き換えて予約する（複数走査や再起動で
        二重取得されないようにするため）。予約は状態遷移なので、その時刻を `updated_at` に刻む
        （刻み点をここ 1 箇所に集約し、取得側で個別に時刻を渡さない）。reserve_status=None なら
        status を書き換えず取得のみ行う（単一消費者かつ「処理と退避の間でクラッシュしても再実行で
        取りこぼさない」冪等な経路向け）。
        """
        for path, record in self.iter_records():
            if record.get("status") == ready_status:
                if reserve_status is not None:
                    record["status"] = reserve_status
                    record["updated_at"] = datetime.datetime.now().isoformat()
                    self._write(path, record)
                return path, record
        return None

    # ── 退避 ──

    def mark_done(self, path: str):
        """処理済みファイルを done/ へ退避する（履歴保持・再処理防止）。"""
        os.makedirs(self.done_dir, exist_ok=True)
        shutil.move(path, os.path.join(self.done_dir, os.path.basename(path)))

    def quarantine(self, path: str):
        """壊れた/処理不能なファイルを failed/ へ隔離する（走査対象から外す）。"""
        os.makedirs(self.failed_dir, exist_ok=True)
        try:
            shutil.move(path, os.path.join(self.failed_dir, os.path.basename(path)))
        except OSError:
            self.log.exception("ファイルの隔離に失敗: %s", path)

    # ── pick-one 常駐ループ ──

    async def run(self, handler, *, ready_status: str, reserve_status: str | None = None,
                  quarantine_on_error: bool = True):
        """ready 1 件を取得 → handler(await) → done/ 退避、を繰り返す常駐ループ。

        handler(path, record) は async。handler が例外を送出した場合（または done/ 退避自体が
        失敗した場合）の扱いを quarantine_on_error で選ぶ（typo で分岐を取り違えないよう bool）:
          - True（既定）: failed/ へ隔離する。reserve_status=None で「未処理のまま残すと毎回
                          再実行されてしまう」経路（制御命令など）向け。
          - False:        その場に残す。reserve_status で予約済みのため再取得されない経路向け。
        ready が無いときは poll_interval 秒スリープする。
        """
        while True:
            picked = self.claim(ready_status, reserve_status=reserve_status)
            if picked is None:
                await asyncio.sleep(self.poll_interval)
                continue
            path, record = picked
            try:
                await handler(path, record)
                self.mark_done(path)
            except Exception:
                self.log.exception("キュー処理に失敗: %s", path)
                if quarantine_on_error:
                    self.quarantine(path)

    # ── 内部 I/O ──

    def _read(self, path: str) -> dict | None:
        """JSON を読む。壊れて読めなければ failed/ へ隔離して None を返す。"""
        try:
            with open(path) as fp:
                return json.load(fp)
        except (json.JSONDecodeError, OSError):
            self.log.warning("ファイルを読めず隔離: %s", path)
            self.quarantine(path)
            return None

    def _write(self, path: str, record: dict):
        """record を原子的に path へ書く（tmp へ全量書込 → os.replace で差し替え）。

        共有 FS では書込中の中途半端な JSON を別プロセスや次ポーリングが読み torn-read する。
        truncate-in-place（open(w)）はその窓を生むため、既存 tier3_handler / approval_store と
        同じ tmp→os.replace 方式に揃える。失敗時は tmp を後始末して例外を伝播する。
        """
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as fp:
                json.dump(record, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
