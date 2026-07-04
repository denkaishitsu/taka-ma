"""sa-ru （orchestrator）デプロイ。

構築手順書: docs/procedures/05-orchestrator.md（Pyinfra対応、Step 1〜10 各サブセクションに対応）
"""

import os
import sys
from pathlib import Path

import yaml
from pyinfra.operations import files, server, pip

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# pyinfra files.* は "~" を展開しない（@local ではリテラル ./~ を生成し偽成功になる）。
# LaunchAgents の配置先は絶対パスで指定する。
HOME = os.path.expanduser("~")

# Step 1: ollama モデルのダウンロード（モデル名は sa-ru.yaml を単一ソースとして参照 — Task #26）
# モデルは component="models" で記録（ollama rm は host グローバルで
# コンポーネント属性は撤去に無関係。複数 deploy が同一モデルを宣言しても
# upsert で 1 レコードに集約され、重複撤去を避ける）。
_model = yaml.safe_load(Path("src/orchestrator/config/sa-ru.yaml").read_text())["sa-ru"]["model"]
server.shell(commands=[f"ollama pull {_model}"])
record("models", f"ollama pull {_model}", _model,
       {"op": "ollama.rm", "model": _model})

# Step 2: ソースコードの配置
files.directory(path="/opt/taka-ma/sa-ru", present=True)
files.sync(src="src/orchestrator/", dest="/opt/taka-ma/sa-ru/orchestrator/")
record("sa-ru", "files.directory /opt/taka-ma/sa-ru", "/opt/taka-ma/sa-ru",
       {"op": "files.directory", "path": "/opt/taka-ma/sa-ru", "present": False})

# Step 3: データディレクトリ（タスクキュー、承認ファイル、アーカイブ）
files.directory(path="/opt/taka-ma/data/tasks", present=True)
files.directory(path="/opt/taka-ma/data/tasks/done", present=True)
files.directory(path="/opt/taka-ma/data/approvals", present=True)
for _d in ("/opt/taka-ma/data/tasks", "/opt/taka-ma/data/approvals"):
    record("sa-ru", f"files.directory {_d}", _d,
           {"op": "files.directory", "path": _d, "present": False})

# Step 6: ai-gateway がライブラリとして import 可能であることを確認
server.shell(commands=[
    "/opt/taka-ma-env/bin/python -c \"import sys; sys.path.insert(0, '/opt/taka-ma/ya-ta'); "
    "from ai_gateway.decomposer import TaskDecomposer; from ai_gateway.classifier import TaskClassifier; "
    "from ai_gateway.risk_classifier import RiskClassifier; print('ai-gateway import OK')\"",
])

# Step 7: Slack 通知 — 依存パッケージ
pip.packages(
    packages=["slack-sdk", "python-dotenv", "pexpect", "pyyaml", "watchdog"],
    virtualenv="/opt/taka-ma-env",
)
record("sa-ru", "pip.packages (sa-ru)",
       "slack-sdk,python-dotenv,pexpect,pyyaml,watchdog",
       {"op": "pip.uninstall",
        "packages": ["slack-sdk", "python-dotenv", "pexpect", "pyyaml", "watchdog"],
        "virtualenv": "/opt/taka-ma-env"})

# Step 9: 設定ファイルの配置（ホスト共通の静的 YAML。変数置換不要のため files.put で配置）
files.put(
    src="src/orchestrator/config/sa-ru.yaml",
    dest="/opt/taka-ma/sa-ru/config/sa-ru.yaml",
)
record("sa-ru", "files.template sa-ru.yaml",
       "/opt/taka-ma/sa-ru/config/sa-ru.yaml",
       {"op": "files.file", "path": "/opt/taka-ma/sa-ru/config/sa-ru.yaml",
        "present": False})

# Step 10: launchd サービス登録（冪等: unload してから load）
files.template(
    src="pyinfra/templates/com.taka-ma.sa-ru.plist.j2",
    dest=f"{HOME}/Library/LaunchAgents/com.taka-ma.sa-ru.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）。
    # bootout は XPC 経由で非同期に片付くため、直後に bootstrap すると解体未完了の
    # ジョブと衝突し "Bootstrap failed: 5: Input/output error" になることを qu-e の
    # 同一パターンで実機再現（#68 E2E T07）。固定 sleep 1 でも足りない場合が実機で
    # あったため、bootstrap 成功まで最大 5 回・1 秒間隔でリトライする（リトライ上限
    # 到達時は exit 1 して pyinfra へ失敗を伝播する。until をそのまま抜けると exit 0
    # になり偽成功を報告してしまうため）。
    "launchctl bootout gui/$(id -u)/com.taka-ma.sa-ru 2>/dev/null; "
    "i=0; until launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.sa-ru.plist; do "
    "i=$((i+1)); if [ $i -ge 5 ]; then exit 1; fi; sleep 1; done",
])
record("sa-ru", "launchd com.taka-ma.sa-ru", "com.taka-ma.sa-ru",
       {"op": "launchctl.bootout", "label": "com.taka-ma.sa-ru"})
