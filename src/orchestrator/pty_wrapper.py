"""PTY ラッパー — SSH 経由で MBP 上の対話型 worker CLI を制御する。

汎用設計: 起動コマンドを引数化し、Claude Code / Gemini CLI / 将来の Codex 等の
任意の対話型 CLI を同じインタフェースで扱う。
"""

import logging

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
        """起動パラメータを保持する（実際の spawn は start() まで遅延）。

        Args は本クラスの docstring 参照。child は start()/reconnect() で生成される
        pexpect セッション（未起動時は None）。
        """
        self.instance_id = instance_id
        self.command = command
        self.model_flag = model_flag
        self.ssh_host = ssh_host
        self.cwd = cwd
        # pexpect セッション。start()/reconnect() で生成され、close() まで保持する
        self.child = None

    def _build_invocation(self) -> str:
        """worker CLI の完全な起動コマンドを構築する"""
        return f"{self.command} {self.model_flag}".strip()

    def start(self):
        """SSH + tmux で worker CLI を起動し、pexpect でその対話セッションに繋ぐ。

        tmux の detached セッション内で CLI を起動してから attach するため、SSH が切れても
        セッションは生存し reconnect() で復帰できる（切断耐性）。cwd 指定時はディレクトリを
        用意した上でそこを tmux の開始ディレクトリにする（タスク専用 workspace で起動）。
        """
        invocation = self._build_invocation()
        # タスク専用 workspace を tmux の開始ディレクトリに指定
        start_dir = f" -c {self.cwd}" if self.cwd else ""
        logger.info("worker インスタンス起動: id=%s command=%s cwd=%s",
                    self.instance_id, invocation, self.cwd or "(default)")
        self.child = pexpect.spawn(
            f"ssh {self.ssh_host} 'mkdir -p {self.cwd} && tmux new-session -d -s {self.instance_id}{start_dir} \"{invocation}\" "
            f"&& tmux attach -t {self.instance_id}'" if self.cwd else
            f"ssh {self.ssh_host} 'tmux new-session -d -s {self.instance_id} \"{invocation}\" "
            f"&& tmux attach -t {self.instance_id}'",
            encoding="utf-8",
            timeout=300,
        )

    def wait_for_prompt(self) -> int:
        """y/n プロンプトを監視（Claude Code / Gemini CLI / Codex 等で共通のパターン）"""
        index = self.child.expect([
            r"\[y/n\]",
            r"\(yes/no\)",
            r"Allow\?",
            pexpect.EOF,
            pexpect.TIMEOUT,
        ])
        return index

    def approve(self):
        """検出した y/n プロンプトへ承認（y）を送る。"""
        self.child.sendline("y")

    def deny(self):
        """検出した y/n プロンプトへ拒否（n）を送る。"""
        self.child.sendline("n")

    def send_task(self, task: str):
        """worker にタスクを送信"""
        self.child.sendline(task)

    def reconnect(self):
        """SSH 切断後の再接続（tmux セッションは生存している）"""
        logger.info("再接続: %s", self.instance_id)
        self.child = pexpect.spawn(
            f"ssh {self.ssh_host} 'tmux attach -t {self.instance_id}'",
            encoding="utf-8",
            timeout=300,
        )

    def close(self):
        """pexpect セッションを閉じる（tmux セッション自体の kill は呼び出し側の責務）。"""
        if self.child:
            self.child.close()


# 後方互換エイリアス（既存呼び出し箇所が `ClaudeCodeWrapper` を import している場合の救済）
# 新規コードは `WorkerPtyWrapper` を直接使用すること。
ClaudeCodeWrapper = WorkerPtyWrapper
