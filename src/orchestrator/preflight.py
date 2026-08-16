"""worker ホスト認証プリフライト — worker 起動前の SSH / git remote / Anthropic 検査。

worker（headless の claude -p）が動く worker ホストでは、タスクの失敗が
(1) sa-ru → worker ホストの SSH 断、(2) worker ホスト → git remote の認証・到達性、
(3) Anthropic 認証の失効、のどの経路でも起きうる。事後のエラー文からの推測は
SSH 認証エラーと Anthropic subscription エラーの混同（登録済み鍵に対する再作成提案という
誤診断）を招いた実績があるため、worker を起動する前に経路別へ分けて検査し、
不合格なら「どの経路が・どの実出力で」落ちたかを確定させて起動を止める。

検査は依存の浅い順（SSH → git → Anthropic）に行い、最初の不合格で打ち切る。
上流が落ちている状態で下流を検査しても原因が混ざった報告になるだけのため、
1 回の不合格につき原因は常に 1 経路に確定する。

セキュリティ（~/.claude/SECURITY_RULES.md 準拠）:
- 判定は各検査コマンドの exit code のみ。鍵・トークン本体を出力するコマンドは使わない
  （git は ls-remote の成否、Anthropic は最小プローブの成否だけを見る。stdout は捨てる）。
- 報告へ載せるのはエラー出力の該当 1 行だけに絞り、既知のトークン形式は伏字化する。
"""

import re
import shlex
import subprocess
import threading
import time

# git 検査で「workspace が git repo でない / origin 未設定」を表す番兵 exit code。
# 新規 clone 運用の空 workspace は検査対象外（git 到達性を問う相手が居ない）を意味し、
# 不合格ではない。git ls-remote 自体のエラーは 128 等で返るため衝突しない。
_SKIP_RC = 42

# エラー出力の伏字化対象（万一トークン形式が紛れ込んでも Slack / ログへ出さない保険）
_TOKEN_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),                  # Anthropic API キー
    re.compile(r"xox[a-z]-[A-Za-z0-9-]+"),                 # Slack トークン
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]+"),   # GitHub トークン
    re.compile(r"github_pat_[A-Za-z0-9_]+"),               # GitHub fine-grained PAT
    re.compile(r"eyJ[A-Za-z0-9_.-]{20,}"),                 # JWT
]


def _redact(text: str) -> str:
    """既知のトークン形式を伏字化する（検査コマンド自体は鍵を出さない前提の二重防御）。"""
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _safe_line(stderr: str, stdout: str = "") -> str:
    """エラー出力から報告用の該当 1 行を取り出す（stderr 優先・伏字化・長さ制限）。

    「エラー実出力の該当行を添えて報告する」ための抽出。全文を流すと長大な上に
    無関係行が診断を濁すため、最後の非空行（エラーの結論が出る位置）に絞る。
    """
    for stream in (stderr, stdout):
        lines = [ln.strip() for ln in (stream or "").splitlines() if ln.strip()]
        if lines:
            return _redact(lines[-1])[:300]
    return "(エラー出力なし)"


class PreflightFailure(RuntimeError):
    """プリフライト不合格。kind で原因経路を機械的に区別する（ssh / git / anthropic）。

    detail はエラー実出力の該当 1 行（伏字化済み）。cached は TTL 内キャッシュからの
    再検出かどうかで、呼び出し側は初回のみ report() を Slack へ通知し、昇格ラダーの
    再試行で同じ通知を重ねない。
    """

    # 種別の説明は「どの経路の問題で、どの経路の問題ではないか」の切り分け事実のみを書く。
    # 対処提案（鍵の再作成等）は書かない — 検査は原因の確定までが責務で、OK だった経路への
    # 対処や推測の提案が過去の誤診断の源だったため。
    _LABELS = {
        "ssh": "sa-ru → worker ホストの SSH 接続不可（git remote・Anthropic の検査には未到達）",
        "git": ("worker ホスト → origin の git remote 到達不可"
                "（sa-ru → worker ホストの SSH 検査は通過。Anthropic 認証の問題ではない）"),
        "anthropic": ("Anthropic 認証 / worker CLI のエラー"
                      "（sa-ru → worker ホストの SSH は正常。git 用 SSH 鍵の問題ではない）"),
    }

    def __init__(self, kind: str, detail: str):
        super().__init__(f"認証プリフライト不合格（{kind}）: {detail}")
        self.kind = kind
        self.detail = detail
        self.cached = False

    def report(self) -> str:
        """Slack 通知用の不合格報告（種別＋該当行。対処提案は含めない）。"""
        return ("⛔ 認証プリフライト不合格 — worker を起動しません\n"
                f"  種別: {self._LABELS[self.kind]}\n"
                f"  該当行: {self.detail}")


class AuthPreflight:
    """worker 起動前の認証・到達性検査（SSH → git remote → Anthropic の順・不合格で打ち切り）。

    運用値（各検査のタイムアウト・キャッシュ TTL）は sa-ru.yaml の preflight ブロックが
    唯一の源（コード側に既定値なし。欠落は KeyError で即落とす）。

    検査結果は TTL キャッシュする。PASS のキャッシュ（pass_ttl_sec）は同一タスクの多段
    worker 起動で Anthropic プローブ（実推論 1 回ぶんの時間・使用量）を毎回払わないため。
    FAIL のキャッシュ（fail_ttl_sec）は昇格ラダーが次候補モデルで再突入したときに同じ検査と
    同じ Slack 通知を重ねないためで、復旧後の再試行を長く塞がないよう短くする。

    Args:
        ssh_host: worker ホストの SSH ホスト名（sa-ru.yaml ssh.mbp_host と同じ値を注入）。
        conf:     sa-ru.yaml の preflight ブロック。
        clock:    現在時刻の供給源（テストで TTL を進めるための注入点）。
    """

    def __init__(self, ssh_host: str, conf: dict, clock=time.monotonic):
        self.ssh_host = ssh_host
        self.ssh_timeout = conf["ssh_timeout_sec"]
        self.git_timeout = conf["git_timeout_sec"]
        self.anthropic_timeout = conf["anthropic_timeout_sec"]
        self.pass_ttl = conf["pass_ttl_sec"]
        self.fail_ttl = conf["fail_ttl_sec"]
        self._clock = clock
        self._cache: dict[str, tuple[float, PreflightFailure | None]] = {}
        # check() は to_thread 経由で並行 worker から同時に呼ばれる。ロックで検査を直列化し、
        # キャッシュ未命中の競合で同じプローブ（Anthropic は実推論コスト）と同じ Slack 通知が
        # 多重に走るのを防ぐ（2 本目はロック解放後にキャッシュ命中で即返る）
        self._lock = threading.Lock()

    def check(self, workspace: str | None, cli_command: str = "claude") -> None:
        """全検査を実行し、不合格なら PreflightFailure を送出する（合格・対象外は無音）。

        同期ブロッキング（SSH 実行）のため、イベントループからは to_thread で呼ぶこと。
        """
        with self._lock:
            self._checked("ssh", self._check_ssh)
            if workspace:
                self._checked(f"git:{workspace}", lambda: self._check_git(workspace))
            self._checked(f"anthropic:{cli_command}",
                          lambda: self._check_anthropic(cli_command))

    # ── キャッシュ層 ──

    def _checked(self, key: str, fn) -> None:
        """検査 1 件を TTL キャッシュ越しに実行する。FAIL は cached 印をつけて再送出。"""
        now = self._clock()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            failure = hit[1]
            if failure is not None:
                failure.cached = True
                raise failure
            return
        try:
            fn()
        except PreflightFailure as f:
            self._cache[key] = (now + self.fail_ttl, f)
            raise
        self._cache[key] = (now + self.pass_ttl, None)

    # ── 個別検査 ──

    def _run(self, remote_cmd: str, timeout: int, tty: bool = False):
        """worker ホスト上で検査コマンドを実行する。

        BatchMode=yes で SSH の対話認証プロンプトを禁止する — sa-ru → worker ホストの
        認証が壊れているとき、パスワード入力待ちでタイムアウトまで沈黙する（原因不明の
        timeout に化ける）のではなく、即座に認証エラーとして失敗させて種別を確定する。
        tty=True（-tt）はリモートを長く走らせうる検査（Anthropic プローブ）用 — タイムアウトで
        ローカル ssh を殺したときに SIGHUP がリモートへ伝播し、孤児プロセスを残さない
        （headless_runner.run と同じ理由）。
        """
        argv = ["ssh", "-o", "BatchMode=yes"]
        if tty:
            argv.append("-tt")
        argv += [self.ssh_host, remote_cmd]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    def _check_ssh(self) -> None:
        """sa-ru → worker ホストの SSH 到達性・認証（exit code のみで判定）。"""
        try:
            r = self._run("true", self.ssh_timeout)
        except subprocess.TimeoutExpired as e:
            raise PreflightFailure(
                "ssh", f"SSH 応答なし（{self.ssh_timeout}秒でタイムアウト）") from e
        except OSError as e:
            raise PreflightFailure("ssh", _redact(str(e))[:300]) from e
        if r.returncode != 0:
            raise PreflightFailure("ssh", _safe_line(r.stderr, r.stdout))

    def _check_git(self, workspace: str) -> None:
        """worker ホスト → origin の git 到達性（ls-remote の成否のみ。ref 一覧も出さない）。

        workspace が git repo でない / origin 未設定（新規 clone 運用の空 workspace）は
        番兵 exit code で検査対象外として合格扱いにする。ls-remote 側も BatchMode で
        対話プロンプトを禁止し、鍵不備を沈黙ではなく即時のエラー行として顕在化させる。
        """
        ws = shlex.quote(workspace)
        remote_cmd = (
            f"cd {ws} 2>/dev/null && git remote get-url origin >/dev/null 2>&1"
            f" || exit {_SKIP_RC}; "
            "GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=10' "
            "git ls-remote origin HEAD >/dev/null")
        try:
            r = self._run(remote_cmd, self.git_timeout)
        except subprocess.TimeoutExpired as e:
            raise PreflightFailure(
                "git", f"git ls-remote 応答なし（{self.git_timeout}秒でタイムアウト）") from e
        except OSError as e:
            raise PreflightFailure("git", _redact(str(e))[:300]) from e
        if r.returncode in (0, _SKIP_RC):
            return
        raise PreflightFailure("git", _safe_line(r.stderr, r.stdout))

    def _check_anthropic(self, cli_command: str) -> None:
        """Anthropic 認証の有効性（worker CLI の最小プローブの成否のみで判定）。

        認証ファイルの存在確認では「ファイルはあるが組織側で subscription が無効」を
        検出できない（実際に起きた失効形態）ため、実際に 1 往復する最小プローブが必要。
        成功時の stdout（モデルの応答）は使わず捨てる。失敗時のエラー行だけ報告に使う。
        コスト（実推論 1 回）は pass_ttl_sec のキャッシュで抑える。
        """
        cli = shlex.quote(cli_command)
        try:
            r = self._run(f"{cli} -p ok", self.anthropic_timeout, tty=True)
        except subprocess.TimeoutExpired as e:
            raise PreflightFailure(
                "anthropic",
                f"認証プローブ応答なし（{self.anthropic_timeout}秒でタイムアウト）") from e
        except OSError as e:
            raise PreflightFailure("anthropic", _redact(str(e))[:300]) from e
        if r.returncode != 0:
            raise PreflightFailure("anthropic", _safe_line(r.stderr, r.stdout))
