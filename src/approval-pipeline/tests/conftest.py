"""テスト共通設定 — approval-pipeline 本体と src/ を import パスに通す。

2 つ必要なのは import の形が 2 種あるため:
  - `import tier3_handler` / `from interceptor import ...` — ハイフン入りディレクトリは
    パッケージにできないため、コンポーネント直下を通して bare import する
  - `from ai_gateway.risk_classifier import ...`（classifier.py 経由）— こちらは
    パッケージ import なので、その 1 つ上の src/ が要る

本番では launchd の PYTHONPATH（/opt/taka-ma/ya-ta 等）が同じ役割を担う（08 の deploy 参照）。
テストは配備先に依存せずリポジトリ上の実体を見る。

conftest.py に置くのは、pytest がテストモジュールより先に必ず読み込むため。テストファイル
冒頭の sys.path 操作に頼ると、収集順（ファイル名のアルファベット順）に依存し、名前の
異なるテストが増えた時点で崩れる（orchestrator で実際に発生・実測）。
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_COMPONENT = os.path.abspath(os.path.join(_HERE, ".."))          # src/approval-pipeline
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))          # src

for _path in (_COMPONENT, _SRC):
    if _path not in sys.path:
        sys.path.insert(0, _path)
