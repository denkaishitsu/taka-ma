"""Slack Bot デプロイ。

構築手順書: docs/procedures/03-slack-bot.md（Pyinfra対応）
"""

import os
import sys

from pyinfra.operations import files, server, pip

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# u-zu 依存パッケージ
_PIP = [
    "slack-bolt>=1.20,<2",
    "slack-sdk>=3.33,<4",
    "python-dotenv>=1.0,<2",
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
    src="templates/env.example.j2",
    dest="/opt/taka-ma/config/.env.example",
)
record("u-zu", "files.template .env.example",
       "/opt/taka-ma/config/.env.example",
       {"op": "files.file", "path": "/opt/taka-ma/config/.env.example",
        "present": False})

# launchdサービス
files.template(
    src="templates/com.taka-ma.u-zu.plist.j2",
    dest="~/Library/LaunchAgents/com.taka-ma.u-zu.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）
    "launchctl bootout gui/$(id -u)/com.taka-ma.u-zu 2>/dev/null; "
    "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.u-zu.plist",
])
record("u-zu", "launchd com.taka-ma.u-zu", "com.taka-ma.u-zu",
       {"op": "launchctl.bootout", "label": "com.taka-ma.u-zu"})
