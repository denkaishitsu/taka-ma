"""headless アダプタ — Claude Code を `claude -p`（非対話・stream-json）で実行する。

設計書 §8.5（worker CLI・実行アダプタ抽象）の headless アダプタ。PTY で対話モードを覗き見て
y/n を送る interactive アダプタ（pty_wrapper）と異なり、非対話 1 プロセスで実行し、各ツールの
allow/deny は PreToolUse フック（設計上 CLI 非依存の decide() を呼ぶ）が同期ゲートする。

本モジュールの責務は「起動・stream-json 解析・完了/ハング検知」まで（実行アダプタの起動側）。
承認判定そのものはフック→decide() が担い、本モジュールは関与しない（中核は CLI 非依存）。
"""

import asyncio
import json
import logging
import shlex

logger = logging.getLogger("sa-ru.headless_runner")

# `claude -p` に必須のフラグ（実機検証で確定）:
#   --output-format stream-json … 構造化イベント（tool_use / result / permission_denials）
#   --verbose                    … これが無いと system/init も tool_use も一切出力されない
#   --include-hook-events        … PreToolUse フックの発火/応答を stream に載せる（観測用）
#   --permission-mode default    … 判定はフックに一元化（フックが allow/deny を権威決定）
_BASE_FLAGS = [
    "--output-format", "stream-json",
    "--verbose",
    "--include-hook-events",
    "--permission-mode", "default",
]


def build_hook_settings(mini_host: str, decide_client: str, decide_socket: str, *,
                        task_id: str = "", team_id: str | None = None,
                        channel: str | None = None, thread_ts: str | None = None,
                        instance_id: str = "", timeout_sec: int = 310,
                        python_bin: str = "/opt/taka-ma-env/bin/python3") -> dict:
    """PreToolUse フックの settings dict を生成する（headless アダプタ用）。

    フックは worker と同じ MBP で発火するが、承認判定は Mac mini 常駐の decide デーモンが
    担う（設計 Appendix §2.1。旧 decide_cli の 1 ショット起動はコールドスタート遅延が
    ツール数に比例して累積するため撤去）。フックコマンドは SSH で Mac mini の薄い
    クライアント（decide_client.py・標準ライブラリのみ＝PYTHONPATH / venv 依存なし）を
    起動し、デーモンの Unix ドメインソケットへ中継させる。
    Tier3 承認リクエストの宛先（team_id / channel / thread_ts / task_id）は sa-ru が
    per-task でここに焼き込む（フック stdin には tool 情報しか無いため）。

    フックコマンドの構造（終了コード契約・fail-closed）:
      - SSH は ControlMaster で多重化し、TCP/認証 handshake を初回のみに抑える。
      - 末尾の `|| exit 2` が SSH 失敗（255）・リモート起動失敗（127 等）を deny に集約する。
        exit 2 以外の非 0 はフックエラーとして Claude Code の既定権限評価に落ち、read 系
        ツールが承認を素通りし得る（fail-open）ため、exit 0/2 以外で終わる経路を持たない。

    timeout_sec はフックの上限。クライアントの応答待ち（308 秒）・デーモンの 1 判定上限
    （305 秒）より外側の既定 310 秒（Tier3 の人間待ち最大 300 秒を切らない）。

    Args:
        mini_host: MBP から見た Mac mini の SSH ホスト名（config の ssh ブロックで定義）。
        decide_client: Mac mini 上の decide_client.py の絶対パス。
        decide_socket: Mac mini 上の decide デーモンの Unix ドメインソケットパス。
        python_bin: Mac mini 上の decide_client.py 実行バイナリ。標準ライブラリのみのため
            素の python3 でも動くが、存在が deploy（01-common-base）で保証される venv を既定にする。
    """
    args = ["--socket", decide_socket, "--task-id", task_id, "--instance-id", instance_id]
    if team_id:
        args += ["--team-id", team_id]
    if channel:
        args += ["--channel", channel]
    if thread_ts:
        args += ["--thread-ts", thread_ts]
    remote = (f"{shlex.quote(python_bin)} {shlex.quote(decide_client)} "
              + " ".join(shlex.quote(a) for a in args))
    # ControlPath の %C は接続 4 要素（local host / remote host / port / user）のハッシュ。
    # ~/.ssh/config を書き換えず、フックコマンド内で多重化を完結させる（Appendix §2.1）。
    ssh_opts = ("-o ControlMaster=auto -o ControlPath=~/.ssh/cm-decide-%C "
                "-o ControlPersist=600 -o ConnectTimeout=10")
    hook_cmd = f"ssh {ssh_opts} {shlex.quote(mini_host)} {shlex.quote(remote)} || exit 2"
    # matcher は空文字＝全ツール一致（仕様上の確実な表現）。"*" も実機 2.1.x では発火を確認したが、
    # 不発になれば承認ゲート全体が無効化される致命箇所のため、仕様保証側に倒す。
    return {"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
        {"type": "command", "command": hook_cmd, "timeout": timeout_sec}]}]}}


class HeadlessResult:
    """1 回の headless 実行の結果。

    text: 最終出力（result イベントの result、無ければ蓄積した assistant テキスト）。
    session_id: system/init で得たセッション ID（--resume 等の将来利用向け）。
    """

    def __init__(self, text: str, session_id: str | None):
        self.text = text
        self.session_id = session_id


class WorkerHeadlessRunner:
    """Claude Code を SSH 越しに `claude -p` で起動し、stream-json を解析して結果を返す。

    Args:
        instance_id: ログ用の一意識別子（例 "{task_id}-step{n}-{model}"）。
        command:     起動する CLI（既定 "claude"）。
        model_flag:  ya-ta 由来のモデル指定（例 "--model opus"）。空なら付けない。
        ssh_host:    worker が動く MBP の SSH ホスト名。
        cwd:         タスク専用 workspace（起動前に mkdir し、そこを cwd にする）。
        hook_settings_path: PreToolUse フックを注入する settings JSON のパス（MBP 上のパス）。
                            None なら --settings を付けない（フックなし＝permission-mode default の
                            既定挙動に従う。テスト・段階導入用）。
    """

    def __init__(self, instance_id: str, command: str = "claude", model_flag: str = "",
                 ssh_host: str = "mbp", cwd: str | None = None,
                 hook_settings_path: str | None = None):
        self.instance_id = instance_id
        self.command = command
        self.model_flag = model_flag
        self.ssh_host = ssh_host
        self.cwd = cwd
        self.hook_settings_path = hook_settings_path

    def _build_argv(self, task: str) -> list[str]:
        """`claude -p <task> ...` の argv を組み立てる（シェル文字列連結を避ける）。

        model_flag は "--model opus" のような空白区切り文字列を split して argv に展開する
        （ya-ta.yaml の記法を保つ・設計 §8.5 モデルルーティング保持）。
        """
        argv = [self.command, "-p", task, *_BASE_FLAGS]
        if self.hook_settings_path:
            argv += ["--settings", self.hook_settings_path]
        if self.model_flag:
            argv += self.model_flag.split()
        return argv

    def _build_remote_cmd(self, task: str) -> str:
        """SSH 越しに MBP 上で実行するリモートコマンド文字列を組み立てる。

        argv は各要素を shlex.quote してから連結する（tmux/ssh のシェル再解釈での引数事故を防ぐ）。
        cwd 指定時は起動前に mkdir し、そこを cwd にする（interactive の WorkerPtyWrapper.start が
        担っていた workspace 作成を headless 側に再配置・設計 §11）。
        """
        argv = self._build_argv(task)
        cmd = " ".join(shlex.quote(a) for a in argv)
        if self.cwd:
            cwd_q = shlex.quote(self.cwd)
            cmd = f"mkdir -p {cwd_q} && cd {cwd_q} && {cmd}"
        return cmd

    async def run(self, task: str, timeout: int = 1800) -> HeadlessResult:
        """worker を起動し、stream-json を逐次解析して最終結果を返す。

        完了検知は `result` イベント（構造化・確定的）。無音ヒューリスティックは使わない。
        プロセスが `result` を出さずに終了したらハングとみなし RuntimeError（呼び出し側が
        retry/fallback の土台にする・設計 §8）。全体上限 timeout も併置する。
        """
        remote = self._build_remote_cmd(task)
        logger.info("headless worker 起動: id=%s cwd=%s", self.instance_id, self.cwd or "(default)")
        # -tt で疑似端末を強制割当する。これが無いと、タイムアウト時に proc.kill() で殺せるのは
        # ローカルの ssh クライアントだけで、リモートの claude -p は切断を知らされず孤児化して
        # 走り続ける（§8.5 資源回収）。-tt があるとセッション切断時に sshd がリモート側へ SIGHUP を
        # 送り、claude -p が終了する。stream-json は 1 行ごとに strip して解析するため（_consume_stream）、
        # pty の改行変換が混じっても解析は壊れない。
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-tt", self.ssh_host, remote,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            result = await asyncio.wait_for(self._consume_stream(proc), timeout=timeout)
        except asyncio.TimeoutError:
            # ローカル ssh を落とす。-tt により切断が SIGHUP としてリモート claude -p へ伝播し、
            # 孤児を残さず終了する。
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"headless worker timeout: {self.instance_id}")
        await proc.wait()
        return result

    async def _consume_stream(self, proc) -> HeadlessResult:
        """stream-json を 1 行ずつ解析し、result で完了を返す。result 無し終了はハング。

        - system/init: session_id を採取（--resume 等の将来利用向け）。
        - assistant: text ブロックを蓄積（result が無い場合の出力フォールバック）。
        - result: 最終出力を確定して返す（完了シグナル）。
        """
        # worker の stdout に PTY（ssh -tt・§8.5 資源回収のため）の行編集シーケンスや
        # fable の思考表示由来の ANSI 制御コード（カーソル移動 `\x1b[1D`・行クリア `\x1b[K` 等）が
        # 混じり、そのまま Slack 通知に出ると可読性を損なう（実機確認済み）。pty 経路（__init__ の
        # _drive）と同じ interceptor.strip_ansi で除去し、両経路の出力クリーニングを揃える。
        from interceptor import strip_ansi

        session_id = None       # system/init から採取（後続で追跡・--resume 等の将来利用向け）
        texts: list[str] = []   # assistant の text ブロックを蓄積（result が空だった場合の出力に使う）

        # worker の stdout（SSH 越し）には stream-json が 1 行 1 イベントで流れてくる。
        # 1 行ずつ読み、イベント種別（system/init・assistant・result）ごとに必要な値を取り出す。
        # result イベントを受け取った時点でタスク完了とみなし、そこで確定して返す。
        async for raw in proc.stdout:
            # SSH の stdout はバイト列。文字化けで解析全体を落とさないよう replace で decode し、
            # 行末の改行・空白を落とす（stream-json は 1 行 = 1 JSON）。
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue  # 空行（イベント間の区切り等）はスキップ
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # stream-json の行として解釈できない出力（起動時のノイズ等）は無視して継続。
                continue

            etype = event.get("type")
            if etype == "system" and event.get("subtype") == "init":
                # セッション開始イベント。session_id はここでしか得られない。
                session_id = event.get("session_id")
            elif etype == "assistant":
                # モデルの発話イベント。content 配列のうち text ブロックだけを拾って蓄積する
                # （tool_use ブロックは承認フックが別途処理するため、ここでは出力に含めない）。
                for block in event.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        # text が JSON null だと .get の既定は効かず None が渡るため
                        # `or ""` でガードする（result 経路と対称。strip_ansi(None) の
                        # TypeError で _consume_stream ごと worker が落ちるのを防ぐ）。
                        texts.append(strip_ansi(block.get("text") or ""))
            elif etype == "result":
                # 完了イベント。出力は result フィールドを優先し、空なら蓄積 assistant テキストで
                # 補う（簡潔な応答で result が空になったときに出力を取りこぼさないため）。
                # result も ANSI 混入し得るため strip_ansi を通す（texts は蓄積時に除去済み）。
                text = strip_ansi(event.get("result") or "")
                if not text:
                    text = "\n".join(t for t in texts if t)
                return HeadlessResult(text=text, session_id=session_id)
        # stream が result を出さずに尽きた＝ハング（v2.1.163+ の 5 秒 grace kill 等）。
        # 呼び出し側で retry/fallback の土台にする（設計 §8・無応答検知の統合）。
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"headless worker ended without result (hang?): {self.instance_id}: {stderr[:200]}")
