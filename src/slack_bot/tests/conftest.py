"""テスト共通設定 — src/slack_bot を import パスに通す。

approval-pipeline/tests と同様、ソースルートを直接 import する方式。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
