"""qu-e (sentinel) デプロイ。

構築手順書: docs/procedures/07-sentinel.md（Pyinfra対応）
"""

import os
import sys
from pathlib import Path

import yaml
from pyinfra.operations import files, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# モデルダウンロード（モデル名は qu-e.yaml を単一ソースとして参照 — Task #26）
# モデルは component="models" で記録（host グローバルな ollama rm のため、
# 他デプロイが同一モデルを宣言しても upsert で 1 レコードに集約）。
_model = yaml.safe_load(Path("src/sentinel/config/qu-e.yaml").read_text())["qu-e"]["model"]
server.shell(commands=[f"ollama pull {_model}"])
record("models", f"ollama pull {_model}", _model,
       {"op": "ollama.rm", "model": _model})

# アプリケーション配置
files.sync(src="src/sentinel/", dest="/opt/taka-ma/qu-e/sentinel/")
record("qu-e", "files.directory /opt/taka-ma/qu-e", "/opt/taka-ma/qu-e",
       {"op": "files.directory", "path": "/opt/taka-ma/qu-e", "present": False})

# 設定ファイル（ホスト共通の静的 YAML。変数置換不要のため files.put で配置）
files.put(
    src="src/sentinel/config/qu-e.yaml",
    dest="/opt/taka-ma/qu-e/config/qu-e.yaml",
)
record("qu-e", "files.template qu-e.yaml",
       "/opt/taka-ma/qu-e/config/qu-e.yaml",
       {"op": "files.file", "path": "/opt/taka-ma/qu-e/config/qu-e.yaml",
        "present": False})

# launchdサービス
files.template(
    src="templates/com.taka-ma.qu-e.plist.j2",
    dest="~/Library/LaunchAgents/com.taka-ma.qu-e.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）
    "launchctl bootout gui/$(id -u)/com.taka-ma.qu-e 2>/dev/null; "
    "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.qu-e.plist",
])
record("qu-e", "launchd com.taka-ma.qu-e", "com.taka-ma.qu-e",
       {"op": "launchctl.bootout", "label": "com.taka-ma.qu-e"})
