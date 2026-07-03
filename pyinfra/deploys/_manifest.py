"""共有ヘルパー: 各 deploy からインストール来歴マニフェストへ記録する。

設計書 §6.5。各 deploy はリソース宣言の直後に record(...) を呼び、ターゲット上の
/opt/taka-ma/lib/install_manifest.py（common デプロイで配置）を介して
/opt/taka-ma/data/install-manifest.jsonl へ 1 ステップを upsert する。

記録は「構築する AI」が行う前提。pyinfra は逐次実行・失敗時停止のため、
record の server.shell は直前のリソース適用が成功した後にのみ走る（＝完了時記録）。
payload は base64(JSON) で授受し、shell/Python の引用符衝突を避ける。

teardown は撤去に必要な対称オペレーション（実行は uninstall.py が担う）:
  {"op": "files.directory", "path": ..., "present": False}
  {"op": "files.file", "path": ..., "present": False}
  {"op": "ollama.rm", "model": ...}
  {"op": "launchctl.bootout", "label": "com.taka-ma.<名>"}
  {"op": "brew.service", "service": ..., "running": False, "enabled": False}
  {"op": "pip.uninstall", "packages": [...], "virtualenv": ...}
  {"op": "npm.uninstall", "package": ..., "global": True}
  {"op": "skip", "reason": ...}        # 共有資源・外部資産（撤去しない）
"""
import base64
import json

from pyinfra import host
from pyinfra.operations import server


def _host_name() -> str:
    """host.data.role から host 名を決定（呼び出し時に解決）。

    inventory 未整備のため、role 未解決時は pyinfra のホスト識別子へフォールバック。
    """
    return {"command_center": "mac-mini", "execution_hub": "mbp"}.get(
        host.data.get("role")
    ) or host.name


def record(component, operation, target, teardown, source="pyinfra"):
    """マニフェストへ 1 ステップ upsert する server.shell を発行する。"""
    payload = {
        "component": component,
        "operation": operation,
        "target": target,
        "teardown": teardown,
        "host": _host_name(),
        "source": source,
    }
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    server.shell(
        name=f"record[{component}]: {operation}",
        commands=[
            "/opt/taka-ma-env/bin/python -c "
            "\"import base64, json, sys; "
            "sys.path.insert(0, '/opt/taka-ma/lib'); "
            "import install_manifest as m; "
            f"m.record(**json.loads(base64.b64decode('{b64}').decode()))\""
        ],
    )
