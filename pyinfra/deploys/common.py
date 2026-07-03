"""共通基盤デプロイ（Homebrew, Python, ディレクトリ）。

構築手順書: docs/procedures/01-common-base.md（Pyinfra対応）

各リソースの宣言は、設計書 §6.5 のインストール来歴マニフェストへ
記録される。記録ヘルパー（ターゲット側）は /opt/taka-ma/lib/install_manifest.py、
記録発行（制御側・共有）は _manifest.record。
"""

import os
import sys

from pyinfra.operations import brew, server, files, pip

# 共有の記録ヘルパー（同ディレクトリの _manifest.py）を import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# --- マニフェスト記録の前提（chicken-and-egg 解決のため最初に置く） ---
# data ディレクトリと lib ディレクトリを先に作り、記録ヘルパーと
# アンインストール・ランナーを配置する。
files.directory(path="/opt/taka-ma/data", user="youruser", group="staff", present=True)
files.directory(path="/opt/taka-ma/lib", user="youruser", group="staff", present=True)
files.put(
    src="pyinfra/lib/install_manifest.py",
    dest="/opt/taka-ma/lib/install_manifest.py",
)
files.put(
    src="pyinfra/lib/uninstall.py",
    dest="/opt/taka-ma/lib/uninstall.py",
)

# Python仮想環境（Phase 0 で作成済みの場合はスキップ）。
# record() の server.shell は /opt/taka-ma-env/bin/python を使うため、最初の
# record() より前に venv の存在を保証する。
server.shell(commands=[
    "test -d /opt/taka-ma-env || uv venv /opt/taka-ma-env --python 3.12",
])

# data ディレクトリ自身を記録（撤去は LIFO 最後・退避はランナー側の責務）
record("common", "files.directory /opt/taka-ma/data", "/opt/taka-ma/data",
       {"op": "files.directory", "path": "/opt/taka-ma/data", "present": False})
record("common", "server.shell uv venv", "/opt/taka-ma-env",
       {"op": "skip", "reason": "pyinfra runtime venv (bootstrap-managed)"})

# Brewfile適用（汎用ツールのため撤去対象外）
_PACKAGES = [
    "python@3.12", "uv", "git", "jq", "curl", "wget",
    "ollama", "iperf3", "htop", "btop", "node", "gh", "tmux",
]
brew.packages(packages=_PACKAGES, present=True)
record("common", "brew.packages", ",".join(_PACKAGES),
       {"op": "skip", "reason": "shared homebrew packages"})

# Tailscale（外部資産のため撤去対象外）
brew.casks(casks=["tailscale"], present=True)
record("common", "brew.casks", "tailscale",
       {"op": "skip", "reason": "external (tailscale)"})

# NOTE: Python パッケージは各コンポーネントの deploy で pip.packages により個別管理

# ollama サービス起動（pyinfra 3.x の brew には service 操作が無いため server.shell で起動）
server.shell(name="ollama サービス起動・有効化", commands=["brew services start ollama"])
record("common", "brew.service ollama", "ollama",
       {"op": "brew.service", "service": "ollama",
        "running": False, "enabled": False})

# ディレクトリ作成（data は先頭で作成・記録済み。ここでは config/logs/models）
for d in ["config", "logs", "models"]:
    files.directory(
        path=f"/opt/taka-ma/{d}",
        user="youruser",
        group="staff",
        present=True,
    )
    record("common", f"files.directory /opt/taka-ma/{d}", f"/opt/taka-ma/{d}",
           {"op": "files.directory", "path": f"/opt/taka-ma/{d}",
            "present": False})
