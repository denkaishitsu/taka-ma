"""承認パイプラインデプロイ。

構築手順書: docs/procedures/08-approval-pipeline.md（Pyinfra対応）
"""

import os
import sys

from pyinfra.operations import files, pip, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# LaunchAgents の配置先は絶対パスで指定する（pyinfra files.* は ~ を展開しないため、
# dest="~/..." はリテラル "./~" を作ってしまう。他デプロイと同じ HOME 展開パターン）。
HOME = os.path.expanduser("~")

# pytest（下の server.shell が実行するテストランナー本体）。/opt/taka-ma-env に未導入だと
# 「No module named pytest」でテスト実行が 0 コマンドのまま失敗する（実機検証で確認・是正）。
pip.packages(
    packages=["pytest"],
    virtualenv="/opt/taka-ma-env",
)
record("approval-pipeline", "pip.packages (approval-pipeline)",
       "pytest",
       {"op": "pip.uninstall", "packages": ["pytest"], "virtualenv": "/opt/taka-ma-env"})

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

# 旧 decide_cli.py の残骸掃除（files.sync は delete しないため過去配備分が残る。
# 旧 1 ショット判定はデーモンへ吸収済みで、残すと到達不可時の迂回実行を誘発する）。
files.file(path="/opt/taka-ma/sa-ru/approval-pipeline/decide_cli.py", present=False)

# テスト実行
# classifier.py が import する ai_gateway は /opt/taka-ma/ya-ta 配下（04-ai-gateway デプロイ）、
# decide デーモンが import する slack_notifier は /opt/taka-ma/sa-ru/orchestrator 配下
# （05-orchestrator デプロイ）。decide-daemon の launchd plist（PYTHONPATH）と同じ解決先を与える。
server.shell(commands=[
    "cd /opt/taka-ma/sa-ru/approval-pipeline && "
    "PYTHONPATH=/opt/taka-ma/ya-ta:/opt/taka-ma/sa-ru/orchestrator "
    "/opt/taka-ma-env/bin/python -m pytest tests/ -v",
])

# decide デーモン（headless フックの判定実行系・設計 Appendix §2.1）の launchd 常駐化
files.template(
    src="pyinfra/templates/com.taka-ma.decide-daemon.plist.j2",
    dest=f"{HOME}/Library/LaunchAgents/com.taka-ma.decide-daemon.plist",
)
server.shell(commands=[
    # macOS 10.10+ 推奨構文。再ロードに備えて bootout を先に実行（未登録時はエラー無視）。
    # bootout は XPC 経由で非同期に片付くため、直後の bootstrap が解体未完了のジョブと
    # 衝突し得る（他デプロイの実機で再現済み）。bootstrap 成功まで最大 5 回・1 秒間隔で
    # リトライし、上限到達時は明示 exit 1 で失敗を伝播する（until の偽成功防止）。
    "launchctl bootout gui/$(id -u)/com.taka-ma.decide-daemon 2>/dev/null; "
    "i=0; until launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.decide-daemon.plist; do "
    "i=$((i+1)); if [ $i -ge 5 ]; then exit 1; fi; sleep 1; done",
])
record("approval-pipeline", "launchd com.taka-ma.decide-daemon", "com.taka-ma.decide-daemon",
       {"op": "launchctl.bootout", "label": "com.taka-ma.decide-daemon"})
