"""sa-ru 起動エントリポイント — `python -m orchestrator` で launchd から起動される。

`orchestrator` パッケージ（__init__.py）を import 済みの状態でこのモジュールが
`__main__` として実行される。ここで 2 つの YAML をマージして単一 config を構築し、
`Orchestrator(config).run()` を呼ぶ。plist（com.taka-ma.sa-ru.plist.j2）の
ProgramArguments はこのエントリを指す。

構築手順書: docs/procedures/05-orchestrator.md Step 7（launchd 登録）
関連: 設計書 §1.3 / §2.2
"""

import asyncio
from pathlib import Path

import yaml

from orchestrator import Orchestrator

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
