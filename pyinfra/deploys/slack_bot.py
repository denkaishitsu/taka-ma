"""Slack Bot デプロイ。

構築手順書: docs/procedures/03-slack-bot.md（Pyinfra対応）
"""

import os
import sys

from pyinfra.operations import files, server, pip

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# 実ホームの絶対パス。pyinfra の files.* は `~` を展開せず、@local では
# リテラル ./~ をカレント配下に作ってしまうため、files 系は必ず絶対パスを使う
# （ssh_tunnel.py と同一方針・T02 で是正済の defect 再発防止）。
HOME = os.path.expanduser("~")

# u-zu 依存パッケージ
_PIP = [
    "slack-bolt>=1.20,<2",
    "slack-sdk>=3.33,<4",
    "python-dotenv>=1.0,<2",
    "pyyaml>=6,<7",  # users.yaml / ya-ta.yaml の読取（services/user_store.py 等）
]
pip.packages(packages=_PIP, virtualenv="/opt/taka-ma-env")
record("u-zu", "pip.packages (u-zu)", ",".join(_PIP),
       {"op": "pip.uninstall", "packages": _PIP, "virtualenv": "/opt/taka-ma-env"})

# アプリケーション配置
files.sync(src="src/slack_bot/", dest="/opt/taka-ma/u-zu/slack_bot/")
record("u-zu", "files.directory /opt/taka-ma/u-zu", "/opt/taka-ma/u-zu",
       {"op": "files.directory", "path": "/opt/taka-ma/u-zu", "present": False})

# .envファイルはPyinfraで配置しない（手動 or Vault）
# テンプレートのみ配置
files.template(
    src="pyinfra/templates/env.example.j2",
    dest="/opt/taka-ma/config/.env.example",
)
record("u-zu", "files.template .env.example",
       "/opt/taka-ma/config/.env.example",
       {"op": "files.file", "path": "/opt/taka-ma/config/.env.example",
        "present": False})

# launchdサービス
files.template(
    src="pyinfra/templates/com.taka-ma.u-zu.plist.j2",
    dest=f"{HOME}/Library/LaunchAgents/com.taka-ma.u-zu.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）。
    # bootout は XPC 経由で非同期に片付くため、直後に bootstrap すると解体未完了の
    # ジョブと衝突し "Bootstrap failed: 5: Input/output error" になることを qu-e の
    # 同一パターンで実機再現（#68 E2E T07）。固定 sleep 1 でも足りない場合が実機で
    # あったため、bootstrap 成功まで最大 5 回・1 秒間隔でリトライする（リトライ上限
    # 到達時は exit 1 して pyinfra へ失敗を伝播する。until をそのまま抜けると exit 0
    # になり偽成功を報告してしまうため）。
    "launchctl bootout gui/$(id -u)/com.taka-ma.u-zu 2>/dev/null; "
    "i=0; until launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.u-zu.plist; do "
    "i=$((i+1)); if [ $i -ge 5 ]; then exit 1; fi; sleep 1; done",
])
record("u-zu", "launchd com.taka-ma.u-zu", "com.taka-ma.u-zu",
       {"op": "launchctl.bootout", "label": "com.taka-ma.u-zu"})
