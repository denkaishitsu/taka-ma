"""テスト共通設定 — src/ を import パスに通す。

orchestrator のテストは `from orchestrator import ...` とパッケージ名で import するため、
sentinel/slack_bot（コンポーネント直下を通す）と違い、**その 1 つ上の src/** を通す。

conftest.py に置くのは、pytest がテストモジュールより先に必ず読み込むため。従来は
一部のテストファイル冒頭の `sys.path.insert` に頼っており、「アルファベット順で先に
読まれるファイルがパスを用意する」という暗黙の前提で成立していた。#132 が `a` で始まる
テストを追加した結果その前提が崩れ、収集エラーで全件が実行されなくなった（実測）。
ファイル名に依存しない場所へ移すことで再発を防ぐ。
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
