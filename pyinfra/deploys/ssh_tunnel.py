"""SSH トンネル / Tailscale 構築デプロイ（@local・各マシンで自身に適用）。

構築手順書: docs/procedures/02-ssh-tunnel.md

SSH 確立前に動かすため、common.py と同様 `@local` で各マシンが自身に適用する
（制御ノード→対象機の SSH に依存しない）。相手機の情報は `--data` で渡す。

鍵方式（マシン別鍵）:
- 各マシンは**自分専用の鍵** `~/.ssh/taka-ma-<role>` を**自機で生成**し保持する
  （秘密鍵は機外に出さない／リポジトリに鍵を置かない）。生成は手順書02 Step1-2。
- 相手機の**公開鍵のみ**を `--data peer_pubkey=...` で受け取り、自機の
  `~/.ssh/authorized_keys` に登録する（公開鍵は秘密情報ではない）。
  → 相手機は自分の秘密鍵で本機へ接続できる。
- 片方の鍵が漏れても相手機の鍵は無事で、個別失効が可能。

実行は各マシンの実ターミナルで行う（sshd 変更の sudo パスワード入力に tty が要る）。
global `--sudo` は付けない（~/.ssh 配下まで root 所有になり破綻する）。sshd 関連
操作のみ per-op `_sudo=True` で昇格し、pyinfra が必要時に sudo パスワードを尋ねる。

  # MBP（相手 = Mac mini）。peer_pubkey は Mac mini の ~/.ssh/taka-ma-mac-mini.pub の中身
  pyinfra @local \
    --data role=mbp \
    --data peer_alias=mac-mini \
    --data peer_host=<Mac mini の Tailscale IP/ホスト名> \
    --data peer_user=hmt \
    --data peer_pubkey="ssh-ed25519 AAAA... taka-ma-mac-mini" \
    pyinfra/deploys/ssh_tunnel.py

  # Mac mini（相手 = MBP）。peer_pubkey は MBP の ~/.ssh/taka-ma-mbp.pub の中身
  pyinfra @local \
    --data role=mac-mini \
    --data peer_alias=mbp \
    --data peer_host=<MBP の Tailscale IP/ホスト名> \
    --data peer_user=youruser \
    --data peer_pubkey="ssh-ed25519 AAAA... taka-ma-mbp" \
    pyinfra/deploys/ssh_tunnel.py

構成方針: Tailscale 専用（10GbE 直結・静的IP は使わない）。
既存の ~/.ssh/config・/etc/ssh/sshd_config は破壊せず Include 方式で追加する。
"""

import os
import sys

from pyinfra import host
from pyinfra.operations import files, server

# 共有の記録ヘルパー（同ディレクトリの _manifest.py）を import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# --- --data 必須項目。未指定なら明確に失敗させる ---
role = host.data.get("role")
peer_alias = host.data.get("peer_alias")
peer_host = host.data.get("peer_host")
peer_user = host.data.get("peer_user")
peer_pubkey = (host.data.get("peer_pubkey") or "").strip()
_missing = [n for n, v in
            (("role", role), ("peer_alias", peer_alias), ("peer_host", peer_host),
             ("peer_user", peer_user), ("peer_pubkey", peer_pubkey))
            if not v]
if _missing:
    raise ValueError(
        "ssh_tunnel.py: 次の --data が未指定です: " + ", ".join(_missing)
        + "（例: pyinfra @local --data role=mbp --data peer_alias=mac-mini"
          " --data peer_host=100.x.x.x --data peer_user=hmt"
          " --data peer_pubkey=\"ssh-ed25519 AAAA... taka-ma-mac-mini\""
          " pyinfra/deploys/ssh_tunnel.py）"
    )

# 実ホームの絶対パス。pyinfra の files.* は `~` を展開せず、@local では
# リテラル ./~ をカレント配下に作ってしまうため、files 系は必ず絶対パスを使う。
# @local 実行なので control=target、expanduser はこのマシンの home を返す。
HOME = os.path.expanduser("~")

# 自機鍵のパス。server.shell / ssh 設定値はシェル・ssh が ~ を展開するため ~ のままでよい。
self_key = f"~/.ssh/taka-ma-{role}"
peer_pubkey = peer_pubkey.replace("\n", " ").replace("\r", " ").strip()
if "'" in peer_pubkey:
    raise ValueError("ssh_tunnel.py: peer_pubkey に不正な文字（単一引用符）が含まれます")

# Tailscale（外部資産）。未導入時のみ cask 導入（既存アプリには触らない）。
server.shell(name="Tailscale 導入（未導入時のみ）", commands=[
    "brew list --cask tailscale >/dev/null 2>&1 "
    "|| test -d /Applications/Tailscale.app "
    "|| brew install --cask tailscale",
])
record("ssh_tunnel", "brew.casks tailscale", "tailscale",
       {"op": "skip", "reason": "external (tailscale)"})

# Tailscale CLI を PATH へ公開。cask 版は CLI を app バンドル内
# (/Applications/Tailscale.app/Contents/MacOS/Tailscale) に閉じ込め PATH に出さない。
# 手順書02 は `tailscale up` / `tailscale status` を素のコマンドで呼ぶため公開が要る。
#
# symlink ではなく wrapper スクリプトを置く。symlink だと macOS が実行ファイルパスを
# symlink 側 (/usr/local/bin/tailscale) として解決し、GUI バイナリが自身の .app バンドルを
# 特定できず `Fatal error: bundleIdentifier is unknown to the registry` で crash する。
# wrapper が絶対実体パスを exec すれば実行ファイルパスがバンドル内になり正しく解決される。
# 既存に壊れた symlink が残っていると tee が実体（app バイナリ）を上書きするため先に rm する。
_TS_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
server.shell(name="Tailscale CLI を /usr/local/bin へ公開（wrapper・冪等）", commands=[
    f"test -x {_TS_CLI} && sudo mkdir -p /usr/local/bin && "
    f"sudo rm -f /usr/local/bin/tailscale && "
    f"printf '#!/bin/sh\\nexec {_TS_CLI} \"$@\"\\n' | "
    f"sudo tee /usr/local/bin/tailscale >/dev/null && "
    f"sudo chmod +x /usr/local/bin/tailscale",
])
record("ssh_tunnel", "tailscale cli wrapper", "/usr/local/bin/tailscale",
       {"op": "server.shell", "command": "sudo rm -f /usr/local/bin/tailscale",
        "sudo": True})

# ~/.ssh ディレクトリ（files.* は ~ 非展開のため絶対パス）
files.directory(path=f"{HOME}/.ssh", mode="700", present=True)

# 自機鍵が存在することを確認（無ければ Step1-2 を促して失敗）
server.shell(name="自機鍵の存在確認（無ければ Step1-2 を実施）", commands=[
    f'test -f {self_key} || '
    f'{{ echo "ERROR: {self_key} がありません。手順書02 Step1-2 で '
    f'ssh-keygen -t ed25519 -f {self_key} を実行してください" >&2; exit 1; }}',
])

# 相手機の公開鍵を authorized_keys に登録（冪等）
server.shell(name="authorized_keys に相手機公開鍵を登録（冪等）", commands=[
    "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
    f"grep -qF '{peer_pubkey}' ~/.ssh/authorized_keys || "
    f"echo '{peer_pubkey}' >> ~/.ssh/authorized_keys",
])
record("ssh_tunnel", "authorized_keys peer pubkey", peer_alias,
       {"op": "skip", "reason": "peer public key in authorized_keys (manual revoke)"})

# クラスタ用 SSH client 設定（既存 ~/.ssh/config を壊さず Include 方式で追加）
# files.* は ~ 非展開のため絶対パス。Include 側（ssh が読む）は ~ のままで ssh が展開する。
files.directory(path=f"{HOME}/.ssh/config.d", mode="700", present=True)
files.template(
    src="pyinfra/templates/ssh_config.j2",
    dest=f"{HOME}/.ssh/config.d/taka-ma-cluster",
    mode="600",
    peer_alias=peer_alias,
    peer_host=peer_host,
    peer_user=peer_user,
    identity_file=self_key,
)
record("ssh_tunnel", "files.template ssh client config",
       f"{HOME}/.ssh/config.d/taka-ma-cluster",
       {"op": "files.remove", "path": f"{HOME}/.ssh/config.d/taka-ma-cluster"})

# ~/.ssh/config に Include を冪等追加（無ければ作成し先頭に挿入）
server.shell(name="~/.ssh/config に Include を冪等追加", commands=[
    "touch ~/.ssh/config && chmod 600 ~/.ssh/config && "
    "grep -qF 'Include ~/.ssh/config.d/taka-ma-cluster' ~/.ssh/config || "
    "{ printf 'Include ~/.ssh/config.d/taka-ma-cluster\\n\\n' "
    "| cat - ~/.ssh/config > ~/.ssh/config.tmp && mv ~/.ssh/config.tmp ~/.ssh/config; }",
])

# --- 以下 sudo 必須（sshd ハードニング。pyinfra が必要時に sudo パスワードを尋ねる） ---
# sshd_config.d ディレクトリ
files.directory(path="/etc/ssh/sshd_config.d", present=True, _sudo=True)

# 既存 sshd_config 本体は変更しない（読み取り確認のみ）。
# macOS 既定の sshd_config は `Include /etc/ssh/sshd_config.d/*` を持ち、これが
# クラスタ drop-in (taka-ma-cluster.conf) を読み込む。よって明示的な Include 追記は不要。
# 過去版は `Include .../*.conf` を tee -a で追記していたが、(1) 既定 Include と重複し
# 冗長、(2) LIFO 撤去記録が無く撤去後も残渣化、(3)「既存 sshd_config を壊さない」方針に反する。
# ここでは既定 Include の存在のみ確認し、無ければ fail-fast（手順書02 で手動追記を案内）。
server.shell(name="sshd_config 既定 Include の存在確認（本体は変更しない）", commands=[
    r'grep -Eq "^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/" /etc/ssh/sshd_config || '
    '{ echo "ERROR: /etc/ssh/sshd_config に config.d を読む Include がありません。'
    '手順書02 の指示に従い Include 行を手動追加してください" >&2; exit 1; }',
])

# クラスタ専用 sshd ドロップイン
files.template(
    src="pyinfra/templates/taka-ma-cluster-sshd.conf.j2",
    dest="/etc/ssh/sshd_config.d/taka-ma-cluster.conf",
    _sudo=True,
)
record("ssh_tunnel", "sshd drop-in", "/etc/ssh/sshd_config.d/taka-ma-cluster.conf",
       {"op": "files.remove",
        "path": "/etc/ssh/sshd_config.d/taka-ma-cluster.conf", "sudo": True})

# sshd 再起動（設定反映）
server.shell(name="sshd 再起動", commands=[
    "sudo launchctl kickstart -kp system/com.openssh.sshd",
])
