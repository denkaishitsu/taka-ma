"""PTY ラッパー — SSH 経由で MBP 上の対話型 worker CLI を制御する。

汎用設計: 起動コマンドを引数化し、Claude Code / Gemini CLI / 将来の Codex 等の
任意の対話型 CLI を同じインタフェースで扱う。
"""

import logging
import subprocess

import pexpect

logger = logging.getLogger("sa-ru.pty_wrapper")


class WorkerPtyWrapper:
    """
    Mac mini から SSH 経由で MBP 上の対話型 worker CLI を制御する汎用ラッパー。

    pexpect が SSH セッションごとラップし、tmux で切断耐性を確保する。
    Claude Code / Gemini CLI など Node.js 製の対話型 CLI を共通に扱える。

    Args:
        instance_id: tmux セッション名（一意な識別子）
        command:     起動する worker CLI コマンド（例: "claude", "gemini", "codex"）
        model_flag:  起動コマンドに付加するフラグ（例: "--model sonnet-4.6"）
        ssh_host:    SSH 接続先ホスト
        cwd:         worker を起動する作業ディレクトリ（タスク専用 workspace）
    """

    def __init__(self, instance_id: str, command: str, model_flag: str = "",
                 ssh_host: str = "mbp", cwd: str | None = None):
        """起動パラメータを保持する（実際の spawn は start まで遅延）。

        Args は本クラスの docstring 参照。child は start/reconnect で生成される
        pexpect セッション（未起動時は None）。
        """
        self.instance_id = instance_id
        self.command = command
        self.model_flag = model_flag
        self.ssh_host = ssh_host
        self.cwd = cwd
        # pexpect セッション。start/reconnect で生成され、close まで保持する
        self.child = None

    def _build_invocation(self) -> str:
        """worker CLI の完全な起動コマンドを構築する"""
        return f"{self.command} {self.model_flag}".strip()

    def start(self):
        """SSH + tmux で worker CLI を起動し、pexpect でその対話セッションに繋ぐ。

        tmux の detached セッション内で CLI を起動してから attach するため、SSH が切れても
        セッションは生存し reconnect で復帰できる（切断耐性）。cwd 指定時はディレクトリを
        用意した上でそこを tmux の開始ディレクトリにする（タスク専用 workspace で起動）。
        """
        invocation = self._build_invocation()
        # タスク専用 workspace を tmux の開始ディレクトリに指定
        start_dir = f" -c {self.cwd}" if self.cwd else ""
        logger.info("worker インスタンス起動: id=%s command=%s cwd=%s",
                    self.instance_id, invocation, self.cwd or "(default)")
        # ssh -tt で疑似端末を強制割当する。割当が無いと tmux が端末サイズを取得できず
        # 「open terminal failed: not a terminal」で tmux new-session 自体が失敗し、worker
        # CLI が一度も起動しないまま stderr がそのまま出力として扱われる（実機検証で確認・
        # 是正）。-tt は多重指定でパイプ非対話でも強制割当する。
        # sa-ru は launchd 常駐（TERM 未設定の非対話環境）から起動するため、ssh の pty には
        # クライアント側 TERM が転送されず、tmux が terminfo を引けず「terminal does not
        # support clear」で失敗する（実機検証で確認・是正）。TERM を明示指定する。
        self.child = pexpect.spawn(
            f"ssh -tt {self.ssh_host} 'export TERM=xterm-256color; mkdir -p {self.cwd} && tmux new-session -d -s {self.instance_id}{start_dir} \"{invocation}\" "
            f"&& tmux attach -t {self.instance_id}'" if self.cwd else
            f"ssh -tt {self.ssh_host} 'export TERM=xterm-256color; tmux new-session -d -s {self.instance_id} \"{invocation}\" "
            f"&& tmux attach -t {self.instance_id}'",
            encoding="utf-8",
            timeout=300)

    # Ink TUI のメニュー系プロンプト（interceptor.PromptType の値）。文字列値で比較し
    # interceptor モジュールへの依存を持たない（ハイフン込みディレクトリ間の疎結合を維持）。
    _MENU_PROMPT_VALUES = {"menu", "trust_dialog"}

    def approve(self, prompt_type=None):
        """検出した承認プロンプトへ承認を送る。

        Ink TUI メニュー（MENU/TRUST_DIALOG）はデフォルトでハイライトされた先頭の
        「Yes」系選択肢を Enter で確定する。レガシーの単純テキスト y/n プロンプトには
        "y" を送る（実機検証で Claude Code が `y`/`n` の文字入力を受け付けないメニュー
        UI を採用していることを確認・是正）。

        Enter は "\\r"（CR）を直接送る。sendline の既定改行は os.linesep（macOS/Linux
        では "\\n"）で、Ink の生端末モード入力は Enter として "\\r" を待つため、
        sendline("") では確定されない（実機検証で承認が反映されず無応答のまま止まる
        欠陥を確認・是正）。
        """
        value = getattr(prompt_type, "value", prompt_type)
        if value in self._MENU_PROMPT_VALUES:
            self.child.send("\r")
        else:
            self.child.sendline("y")

    def deny(self, prompt_type=None):
        """検出した承認プロンプトへ拒否を送る。

        Ink TUI メニューは Esc がキャンセル（選択肢の文言・番号位置に依存しない拒否手段）。
        レガシーの単純テキスト y/n プロンプトには "n" を送る。
        """
        value = getattr(prompt_type, "value", prompt_type)
        if value in self._MENU_PROMPT_VALUES:
            self.child.send("\x1b")
        else:
            self.child.sendline("n")

    def send_task(self, task: str):
        """worker にタスクを送信する。

        sendline() は既定で末尾に os.linesep（macOS/Linux では "\\n"）を送るが、
        Ink の生端末モード入力は Enter として "\\r"（CR）を待つため、"\\n" では
        入力欄に文字列が残ったまま送信されない（approve/deny と同根の欠陥。実機検証で
        タスク文字列が入力欄に残留し worker が一切動き出さないまま PTY タイムアウトに
        達することを確認・是正）。テキストを送ってから "\\r" を直接送る。
        """
        self.child.send(task)
        self.child.send("\r")

    def reconnect(self):
        """SSH 切断後の再接続（tmux セッションは生存している）"""
        logger.info("再接続: %s", self.instance_id)
        self.child = pexpect.spawn(
            f"ssh -tt {self.ssh_host} 'export TERM=xterm-256color; tmux attach -t {self.instance_id}'",
            encoding="utf-8",
            timeout=300)

    def close(self):
        """pexpect セッションを閉じ、リモートの tmux セッションも kill する（§8.5 資源回収）。

        tmux は detached 設計ゆえ attach（pexpect）が切れても new-session したセッションは
        MBP 上に生き続ける。タスク終了時にここで kill しないとセッションがリークし積み上がる。
        pexpect を先に閉じてから、切断耐性のために作った tmux セッションを明示的に落とす。
        kill-session はベストエフォート（既に消えている・SSH 不通でも次を止めない）。短い timeout を
        付けてこの後始末自体がハングしないようにする。この close は同期ブロッキング（SSH）を含むため、
        呼び出し側はイベントループ上で直接呼ばず別スレッドで実行すること（§10.7・_run_worker_pty）。
        """
        if self.child:
            self.child.close()
        try:
            subprocess.run(
                ["ssh", self.ssh_host, f"tmux kill-session -t {self.instance_id}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except (subprocess.SubprocessError, OSError):
            logger.warning("tmux セッションの kill に失敗（既に消えている可能性）: %s",
                           self.instance_id)


# 後方互換エイリアス（既存呼び出し箇所が `ClaudeCodeWrapper` を import している場合の救済）
# 新規コードは `WorkerPtyWrapper` を直接使用すること。
ClaudeCodeWrapper = WorkerPtyWrapper
