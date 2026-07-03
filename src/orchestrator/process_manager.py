"""プロセスマネージャ — MBP上のプロセス起動・停止・監視。

構築手順書: docs/procedures/05-orchestrator.md Step 5（プロセスマネージャ更新）
- run_ssh_command(): SSH 経由で任意コマンド実行（汎用、モデル固有関数は作らない）
"""

import logging
import subprocess

from orchestrator.pty_wrapper import ClaudeCodeWrapper

logger = logging.getLogger("sa-ru.process_manager")


class RemoteProcessManager:
    """MBP 上のプロセス（worker CLI・ollama モデル）を SSH 越しに起動/停止/監視する。

    Mac mini=司令塔 / MBP=実行ハブ の分担により、実行系プロセスはすべて MBP 上にあり
    SSH で操作する。SSH 先ホストと軽い操作のタイムアウトは本インスタンスが保持し、
    ResourceMonitor 等の他コンポーネントも注入された本インスタンス経由で同値を共有する。
    """

    def __init__(self, ssh_host: str = "mbp", *, ssh_timeout: int):
        """SSH 先ホストと軽い操作のタイムアウトを受け取る。

        Args:
            ssh_host: MBP の SSH 接続先ホスト名。
            ssh_timeout: ollama ps/stop・Blender 検知など軽い SSH 操作の応答待ち上限（秒）。
                キーワード必須で既定値を持たない（設定漏れを黙って 30 等で動かさず注入時に落とす）。
        """
        self.ssh_host = ssh_host
        # ollama ps/stop・Blender 検知など軽い SSH 操作の応答待ち上限（秒）。値の唯一の出所は
        # sa-ru.yaml の ssh.timeout_sec で運用者が管理し、Orchestrator から必須注入する（コードに
        # 既定値を持たせない＝設定漏れは黙って 30 で動かず注入時に落とす）。実行時はこのインスタンスが
        # 保持し、ResourceMonitor も注入済み process_mgr 経由で同値を参照する。
        # run_ssh_command の既定 120 はモデル実行など重い操作向けで、軽い操作には長すぎる。
        self.ssh_timeout = ssh_timeout
        # instance_id → 起動済み worker ラッパー。停止時に対応インスタンスを引くための台帳
        self.instances: dict[str, ClaudeCodeWrapper] = {}

    def start_claude_code(self, instance_id: str) -> ClaudeCodeWrapper:
        """MBP 上で worker CLI インスタンスを起動し、ラッパーを台帳へ登録して返す（SSH + tmux）。"""
        wrapper = ClaudeCodeWrapper(instance_id, ssh_host=self.ssh_host)
        wrapper.start()
        self.instances[instance_id] = wrapper
        logger.info("Claude Code 起動: %s", instance_id)
        return wrapper

    def stop_claude_code(self, instance_id: str):
        """worker インスタンスを停止する。ラッパーを閉じて台帳から外し、残った tmux も殺す。

        ラッパーの close だけでは tmux セッションが残りうるため、kill-session も併せて行う
        （取り残しによる instance_id 衝突・資源占有を防ぐ）。
        """
        if instance_id in self.instances:
            self.instances[instance_id].close()
            del self.instances[instance_id]
        subprocess.run(
            ["ssh", self.ssh_host, f"tmux kill-session -t {instance_id}"],
            capture_output=True,
        )
        logger.info("Claude Code 停止: %s", instance_id)

    def start_ollama_model(self, model: str):
        """MBP 上で指定 ollama モデルを起動する（明示プリロード用）。"""
        subprocess.run(["ssh", self.ssh_host, f"ollama run {model}"])

    def stop_ollama(self) -> dict:
        """MBP で稼働中の ollama モデルを列挙し、各々を停止する（§7.1 GPU/メモリ解放）。

        停止トリガーは複数ある（Blender 検知による自動停止＝ResourceMonitor、将来の手動停止・
        アイドルスリープ）。停止ロジックの二重実装を避けるため、停止の実体はこの 1 メソッドに
        集約し（SSOT）、各トリガーはここを呼ぶ。ResourceMonitor._stop_llms はここへ委譲する。

        `ollama stop` は MODEL 必須（引数なしは usage エラーで何も停止しない no-op）。稼働モデル名は
        config に直書きせず `ollama ps` から取得する（yaml と乖離しても追随）。各 stop の
        returncode/stderr を確認しログに残す（失敗を握り潰さない）。

        SSH 不達/タイムアウトでも例外を呼び出し側へ伝播させない。本メソッドは複数トリガー（Blender
        自動停止・手動停止/アイドルスリープ）から呼ばれる SSOT で、各呼び出し側に try/except を
        強制しないため、ここで握って次回起動に委ねる。各モデルの stop も独立に try/except で包み、
        1 モデルの失敗で残りの停止を中断しない（後続モデルの GPU/メモリが解放されないのを防ぐ・§7.1）。

        結果は dict で返す（成否を呼び出し側が判別できるようにする＝Slack 手動停止 §8.10c が
        「停止しました」を偽報告しないため）。例外は依然送出しない。返り値:
          {"ok": bool, "stopped": [model...], "failed": [model...], "reason": str|None}
          - ok=False の reason は ps 不達/失敗、または一部モデルの stop 失敗を表す。
          - stopped 空 かつ ok=True は「稼働モデル無し＝停止不要」。
        ResourceMonitor 等の自動トリガーは返り値を無視してよい（従来どおりの呼び出しで動く）。
        """
        try:
            ps = subprocess.run(
                ["ssh", self.ssh_host, "ollama", "ps"],
                capture_output=True, text=True, timeout=self.ssh_timeout,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.warning("ollama ps 実行不可（SSH 不達/タイムアウト）: %s", e)
            return {"ok": False, "stopped": [], "failed": [],
                    "reason": "ollama ps 実行不可（SSH 不達/タイムアウト）"}
        if ps.returncode != 0:
            logger.warning("ollama ps 失敗 (rc=%s): %s", ps.returncode, ps.stderr.strip())
            return {"ok": False, "stopped": [], "failed": [],
                    "reason": f"ollama ps 失敗 (rc={ps.returncode})"}
        # 出力1行目はヘッダ（NAME ...）。以降の各行の先頭列が稼働中モデル名。
        models = [ln.split()[0] for ln in ps.stdout.splitlines()[1:] if ln.strip()]
        if not models:
            logger.info("稼働中の ollama モデルなし（停止不要）")
            return {"ok": True, "stopped": [], "failed": [], "reason": None}
        stopped, failed = [], []
        for model in models:
            try:
                r = subprocess.run(
                    ["ssh", self.ssh_host, "ollama", "stop", model],
                    capture_output=True, text=True, timeout=self.ssh_timeout,
                )
            except (subprocess.TimeoutExpired, OSError) as e:
                logger.warning("ollama stop %s 実行不可（SSH 不達/タイムアウト）: %s", model, e)
                failed.append(model)
                continue
            if r.returncode != 0:
                logger.warning("ollama stop %s 失敗 (rc=%s): %s", model, r.returncode, r.stderr.strip())
                failed.append(model)
            else:
                logger.info("ollama stop %s 成功", model)
                stopped.append(model)
        return {"ok": not failed, "stopped": stopped, "failed": failed,
                "reason": (f"一部モデルの停止に失敗: {', '.join(failed)}" if failed else None)}

    def run_ssh_command(self, command: str, timeout: int = 120, stdin_text: str | None = None) -> str:
        """MBP上で任意のコマンドをSSH経由で実行する。stdin_text 指定時は stdin に流し込む。"""
        result = subprocess.run(
            ["ssh", self.ssh_host, command],
            input=stdin_text,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"SSH command failed: {result.stderr}")
        return result.stdout

    def run_model_subprocess(self, model_name: str, model_conf: dict, prompt: str,
                             timeout: int = 300) -> str:
        """MBP 上の worker モデルに 1 回だけ推論させ、その出力テキストを返す。

        いつ使うか: 対話（y/n 介入）を伴わない「投げて結果を受け取るだけ」の実行。具体的には
        Gemma 等ローカルモデルの単発（§8.7）、Gemini のマルチモーダル単発・cross-review・
        フォールバック（§8.6）など。対話が要る heavy タスクは別経路（PTY ラッパー）が担当する。

        どう動かすか: worker モデルは MBP 上にあるため SSH 越しに起動コマンドを実行し
        （Mac mini=司令塔 / MBP=実行ハブ の分担）、プロンプト本文を起動コマンドの標準入力へ
        流し込む（ollama も多くの CLI もプロンプトを stdin から受け取るため）。

        起動コマンドはモデル定義（ya-ta.yaml の models.<name>）から組み立てる:
          - ローカルモデル (type=local): ``<command> run <model_id>``  例) ollama run gemma4:31b
          - 外部 API の CLI (type=api):  ``<command> <model_flag>``    例) gemini

        引数:
          model_name: ログ・エラー表示用のモデル識別名（routing のキー）
          model_conf: ya-ta.yaml の当該モデル定義（command=CLI 名 / type / model_id / model_flag）
          prompt:     モデルへ渡すプロンプト本文（標準入力で渡す）
        戻り値: モデルの標準出力（推論結果テキスト）。非ゼロ終了なら RuntimeError を送出。
        """
        # 起動する CLI の実行ファイル名（例: "ollama" / "agy"）。プロンプト本文 prompt とは別物。
        cli = model_conf.get("command", "")
        if model_conf.get("type") == "local":
            # ローカルモデルは `<cli> run <model_id>`（ollama 形式）で起動する
            model_id = model_conf.get("model_id", model_name)
            remote = f"{cli} run {model_id}".strip()
        else:
            # 外部 API の CLI は `<cli> <flag>` で起動（どのモデルを使うかはフラグ側で指定）
            model_flag = model_conf.get("model_flag", "")
            remote = f"{cli} {model_flag}".strip()
        # 認証が macOS keychain 依存の CLI（agy 等、ya-ta.yaml で keychain_auth: true）は
        # SSH セッションから keychain を読めないため、GUI 起源 tmux サーバ内で実行する（§8.6）
        if model_conf.get("keychain_auth"):
            return self._run_in_gui_tmux(model_name, remote, prompt, timeout)
        # MBP 上で起動コマンドを SSH 実行し、プロンプトを標準入力へ渡して出力を受け取る
        result = subprocess.run(
            ["ssh", self.ssh_host, remote],
            input=prompt,
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"model subprocess failed ({model_name}): {result.stderr}")
        return result.stdout

    # GUI 起源 tmux サーバのセッション名（launchd com.taka-ma.worker-tmux が起動、構築手順書 06 Step 2-3b）
    GUI_TMUX_SESSION = "taka-ma-worker"

    def _run_in_gui_tmux(self, model_name: str, remote: str, prompt: str,
                         timeout: int) -> str:
        """keychain 依存 CLI を MBP の GUI 起源 tmux サーバ内で単発実行し、出力を回収する。

        なぜ必要か: agy（Antigravity CLI）の認証トークンは macOS keychain 管理で、SSH の
        セキュリティセッションからは読めない（素の `ssh mbp agy -p` は認証失敗になる。
        実測 2026-07-03）。GUI ログインセッション起源の tmux サーバ内なら keychain を参照できる。

        どう動かすか（§8.6）:
          1. プロンプトを MBP 上の一時ファイルへ SSH stdin 経由で書き込む
             （agy -p はプロンプトを引数で受け、stdin を受理しない。実測確定）
          2. tmux new-window で GUI 起源サーバ内にコマンドを投入し、出力をファイルへ書かせる
             （完了マーカとして .done へ mv）
          3. .done の出現をポーリングで待ち、内容を回収して一時ファイルを掃除する
        """
        import time
        import uuid
        base = f"/tmp/taka-ma-subproc-{uuid.uuid4().hex[:12]}"
        # 1. プロンプトと実行スクリプトをファイルで引き渡す。プロンプトの引数直渡しや
        #    コマンドのインライン投入は ssh→tmux→zsh の多重クォートで壊れるため、
        #    どちらも SSH stdin 経由のファイル書込にしてクォート入れ子を排除する
        subprocess.run(
            ["ssh", self.ssh_host, f"cat > {base}.prompt"],
            input=prompt, capture_output=True, text=True, timeout=self.ssh_timeout,
        )
        # exit code は .rc に残し、出力の完成は .out → .done の mv で通知する
        script = (
            f'{remote} "$(cat {base}.prompt)" > {base}.out 2>&1\n'
            f"echo $? > {base}.rc\n"
            f"mv {base}.out {base}.done\n"
        )
        subprocess.run(
            ["ssh", self.ssh_host, f"cat > {base}.sh"],
            input=script, capture_output=True, text=True, timeout=self.ssh_timeout,
        )
        # 2. GUI 起源 tmux サーバ内で実行（window はコマンド終了で自動クローズ）
        run = subprocess.run(
            ["ssh", self.ssh_host,
             f"tmux new-window -d -t {self.GUI_TMUX_SESSION} 'zsh -l {base}.sh'"],
            capture_output=True, text=True, timeout=self.ssh_timeout,
        )
        if run.returncode != 0:
            raise RuntimeError(
                f"model subprocess failed ({model_name}): GUI tmux サーバ "
                f"{self.GUI_TMUX_SESSION} に投入できない（launchd com.taka-ma.worker-tmux "
                f"未稼働の可能性）: {run.stderr}")
        # 3. 完了ポーリング（timeout は呼び出し元のモデル実行タイムアウトをそのまま適用）
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                probe = subprocess.run(
                    ["ssh", self.ssh_host,
                     f"test -f {base}.done && cat {base}.done && echo ---RC--- && cat {base}.rc"],
                    capture_output=True, text=True, timeout=self.ssh_timeout,
                )
                if probe.returncode == 0:
                    output, _, rc = probe.stdout.rpartition("---RC---")
                    if rc.strip() != "0":
                        raise RuntimeError(
                            f"model subprocess failed ({model_name}): rc={rc.strip()} "
                            f"output={output.strip()[:500]}")
                    return output.strip()
                time.sleep(2)
            raise RuntimeError(f"model subprocess timeout ({model_name}): {timeout}s 超過")
        finally:
            subprocess.run(
                ["ssh", self.ssh_host,
                 f"rm -f {base}.prompt {base}.sh {base}.out {base}.done {base}.rc"],
                capture_output=True, text=True, timeout=self.ssh_timeout,
            )
