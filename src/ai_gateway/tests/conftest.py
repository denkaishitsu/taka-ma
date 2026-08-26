"""テスト共通設定 — src/ を import パスに通す。

ai_gateway のテストは `from ai_gateway.xxx import ...` とパッケージ名で import するため、
その 1 つ上の src/ を通す（orchestrator/tests/conftest.py と同じ方式・同じ理由）。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
