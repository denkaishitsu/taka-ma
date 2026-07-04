"""qu-e (sentinel) デプロイ。

構築手順書: docs/procedures/07-sentinel.md（Pyinfra対応）
"""

import os
import sys
from pathlib import Path

import yaml
from pyinfra.operations import files, pip, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# LaunchAgents の配置先は絶対パスで指定する（pyinfra files.* は ~ を展開しないため、
# dest="~/..." はリテラル "./~" を作ってしまう。他デプロイと同じ HOME 展開パターン）。
HOME = os.path.expanduser("~")

# モデルダウンロード（モデル名は qu-e.yaml を単一ソースとして参照 — Task #26）
# モデルは component="models" で記録（host グローバルな ollama rm のため、
# 他デプロイが同一モデルを宣言しても upsert で 1 レコードに集約）。
_model = yaml.safe_load(Path("src/sentinel/config/qu-e.yaml").read_text())["qu-e"]["model"]
server.shell(commands=[f"ollama pull {_model}"])
record("models", f"ollama pull {_model}", _model,
       {"op": "ollama.rm", "model": _model})

# 依存パッケージ（file_auditor/main.py の watchdog、health_checker/resource_optimizer の
# psutil、reviewer.py の httpx。既存の他コンポーネントには無い qu-e 固有の依存で、
# 本 deploy が pip.packages を欠いていたため未導入だった＝実機で ModuleNotFoundError 起動失敗を確認・是正）。
pip.packages(
    packages=["watchdog", "psutil", "httpx", "pyyaml"],
    virtualenv="/opt/taka-ma-env",
)
record("qu-e", "pip.packages (qu-e)",
       "watchdog,psutil,httpx,pyyaml",
       {"op": "pip.uninstall",
        "packages": ["watchdog", "psutil", "httpx", "pyyaml"],
        "virtualenv": "/opt/taka-ma-env"})

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
    src="pyinfra/templates/com.taka-ma.qu-e.plist.j2",
    dest=f"{HOME}/Library/LaunchAgents/com.taka-ma.qu-e.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）。
    # bootout は XPC 経由で非同期に片付くため、直後に bootstrap すると解体未完了の
    # ジョブと衝突し "Bootstrap failed: 5: Input/output error" になることを実機で再現
    # （再デプロイ時のレース）。固定 sleep 1 でも解体完了まで足りない場合が実機であった
    # ため、bootstrap 成功まで最大 5 回・1 秒間隔でリトライする。until は失敗し続けた
    # 場合そのまま抜けて exit 0 になり pyinfra へ偽成功を報告してしまうため、リトライ
    # 上限到達時は明示的に exit 1 して失敗を伝播する。
    "launchctl bootout gui/$(id -u)/com.taka-ma.qu-e 2>/dev/null; "
    "i=0; until launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.qu-e.plist; do "
    "i=$((i+1)); if [ $i -ge 5 ]; then exit 1; fi; sleep 1; done",
])
record("qu-e", "launchd com.taka-ma.qu-e", "com.taka-ma.qu-e",
       {"op": "launchctl.bootout", "label": "com.taka-ma.qu-e"})
