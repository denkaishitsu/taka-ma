"""yata（ai_gateway) デプロイ。

構築手順書: docs/procedures/04-ai-gateway.md（Pyinfra対応）
"""

import os
import sys
from pathlib import Path

import yaml
from pyinfra.operations import files, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# Step 1: DeepSeek-R1 32B ダウンロード（モデル名は ya-ta.yaml を単一ソースとして参照 — Task #26）
# モデルは component="models" で記録（host グローバルな ollama rm のため、
# 他デプロイが同一モデルを宣言しても upsert で 1 レコードに集約）。
_model = yaml.safe_load(Path("src/ai_gateway/config/ya-ta.yaml").read_text())["ya-ta"]["model"]
server.shell(commands=[f"ollama pull {_model}"])
record("models", f"ollama pull {_model}", _model,
       {"op": "ollama.rm", "model": _model})

# Step 1b: num_ctx を 32K に焼き込む（初期投入で自動・冪等）
# WHY: ya-ta は Mac mini 64GB に sa-ru と同居する。DeepSeek を既定 128K で起動すると
#      KV キャッシュで常駐 47GB に達し予算 56GB を食い尽くす（2026-06-06 実測）。32K なら常駐 ~26GB。
#      ya-ta コードは `ollama run` 経由で num_ctx を渡さないため、モデル既定値として焼き込む。
#      同タグ上書き・重み共有（再 DL なし）。値は src/ai_gateway/config/ya-ta.yaml の num_ctx と一致させる。
# NOTE: ollama 0.30 系は `create -f -`（stdin）で Modelfile を読めず
#       「no Modelfile found」になるため、一時ファイルに書いてパス渡しする。
server.shell(commands=[
    f"M=$(mktemp -d)/Modelfile; "
    f"printf 'FROM {_model}\\nPARAMETER num_ctx 32768\\n' > \"$M\"; "
    f"ollama create {_model} -f \"$M\"; rm -f \"$M\"",
])

# Step 2: アプリケーション配置（decomposer.py, classifier.py, risk_classifier.py 含む）
# 2 層構造: /opt/taka-ma/<コンポーネント名 ya-ta>/<役割名パッケージ ai_gateway>/...
files.directory(path="/opt/taka-ma/ya-ta", present=True)
files.sync(src="src/ai_gateway/", dest="/opt/taka-ma/ya-ta/ai_gateway/")
record("ya-ta", "files.directory /opt/taka-ma/ya-ta", "/opt/taka-ma/ya-ta",
       {"op": "files.directory", "path": "/opt/taka-ma/ya-ta", "present": False})

# Step 8: 設定ファイル（ホスト共通の静的 YAML。変数置換不要のため files.put で配置）
files.put(
    src="src/ai_gateway/config/ya-ta.yaml",
    dest="/opt/taka-ma/ya-ta/config/ya-ta.yaml",
)
record("ya-ta", "files.template ya-ta.yaml",
       "/opt/taka-ma/ya-ta/config/ya-ta.yaml",
       {"op": "files.file", "path": "/opt/taka-ma/ya-ta/config/ya-ta.yaml",
        "present": False})


# Step 9: 旧 launchd サービスの削除（冪等、macOS 10.10+ 推奨構文）
server.shell(commands=[
    "launchctl bootout gui/$(id -u)/com.taka-ma.ya-ta 2>/dev/null; "
    "rm -f ~/Library/LaunchAgents/com.taka-ma.ya-ta.plist",
])

# Step 10: ライブラリ import 検証（decomposer 含む）
# sys.path に /opt/taka-ma/ya-ta を追加し、ai_gateway パッケージ階層で import
server.shell(commands=[
    "/opt/taka-ma-env/bin/python -c \"import sys; sys.path.insert(0, '/opt/taka-ma/ya-ta'); "
    "from ai_gateway.decomposer import TaskDecomposer; from ai_gateway.classifier import TaskClassifier; "
    "from ai_gateway.risk_classifier import RiskClassifier; print('ya-ta import OK')\"",
])
