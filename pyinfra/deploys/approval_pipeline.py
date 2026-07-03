"""承認パイプラインデプロイ。

構築手順書: docs/procedures/08-approval-pipeline.md（Pyinfra対応）
"""

import os
import sys

from pyinfra.operations import files, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# アプリケーション配置
files.sync(src="src/approval-pipeline/", dest="/opt/taka-ma/sa-ru/approval-pipeline/")
record("approval-pipeline",
       "files.directory /opt/taka-ma/sa-ru/approval-pipeline",
       "/opt/taka-ma/sa-ru/approval-pipeline",
       {"op": "files.directory",
        "path": "/opt/taka-ma/sa-ru/approval-pipeline", "present": False})

# 設定ファイル pipeline.yaml は上の files.sync が
# /opt/taka-ma/sa-ru/approval-pipeline/config/pipeline.yaml へ既に配備済み（同一パス）のため個別配置は不要。
# 撤去は上の files.sync 配備ディレクトリ record（present=False）で一括カバーされる。

# テスト実行
server.shell(commands=[
    "cd /opt/taka-ma/sa-ru/approval-pipeline && /opt/taka-ma-env/bin/python -m pytest tests/ -v",
])
