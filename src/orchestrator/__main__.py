"""sa-ru 起動エントリポイント — `python -m orchestrator` で launchd から起動される。

`orchestrator` パッケージ（__init__.py）を import 済みの状態でこのモジュールが
`__main__` として実行される。ここで 2 つの YAML をマージして単一 config を構築し、
`Orchestrator(config).run()` を呼ぶ。plist（com.taka-ma.sa-ru.plist.j2）の
ProgramArguments はこのエントリを指す。

構築手順書: docs/procedures/05-orchestrator.md Step 7（launchd 登録）
関連: 設計書 §1.3 / §2.2
"""

import asyncio
import logging
import sys
from pathlib import Path

import yaml

from orchestrator import Orchestrator

# launchd 配下では stdout がそのままサービスログ（plist の StandardOutPath=sa-ru.log）になるため、
# 全ロガーの出力を標準出力へ集約する。u-zu（slack_bot/main.py）と同一方針。これが無いと
# logging.getLogger("sa-ru.orchestrator") にハンドラが付かず INFO ログが sa-ru.log に出ない。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ya-ta.yaml と sa-ru.yaml をマージして単一 config を構築する。
# モデル関連設定（models / routing / fallback / concurrency）は ya-ta.yaml に集約し、
# その他（ssh / task_queue / approval / resource_management / cleanup / qu-e）は sa-ru.yaml に置く。
# 両 yaml のトップレベルキーは重複しない前提（設計書 §1.3 / §2.2）。
ya_ta_path = Path("/opt/taka-ma/ya-ta/config/ya-ta.yaml")
sa_ru_path = Path("/opt/taka-ma/sa-ru/config/sa-ru.yaml")
config = {
    **yaml.safe_load(ya_ta_path.read_text()),
    **yaml.safe_load(sa_ru_path.read_text()),
}

asyncio.run(Orchestrator(config).run())
