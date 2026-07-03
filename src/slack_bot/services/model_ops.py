"""モデル導入の副作用 — worker ホストでの ollama pull/rm・来歴記録、ローカルのサービス再起動。

/taka-ma-model install / uninstall が使う。ya-ta.yaml の編集（純粋部分）は model_store が担い、
ここは「実体のダウンロード/削除」と「設定を反映するためのサービス再起動」を担当する。

ホスト分担（設計書 §1.3）:
  - ローカルモデル（type: local）の実体は MBP（execution_hub）上の ollama にある。
    pull/rm は SSH 越しに MBP で実行する（既存の run_model_subprocess / blender と同じ ssh 先）。
  - 来歴マニフェストは MBP 上の /opt/taka-ma/data/install-manifest.jsonl にあり、
    pyinfra の _manifest と同じく base64(JSON) を SSH 越しに on-host install_manifest.record へ渡す。
    こうして slack 経由で入れたモデルも全体アンインストール（LIFO 再生）の対象に含まれる。
  - sa-ru / ya-ta は u-zu と同居（Mac mini）するため、再起動はローカル launchctl で行う。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
"""

import base64
import json
import logging
import os
import shlex
import subprocess

logger = logging.getLogger("u-zu.model_ops")

# worker（ローカルモデルの実体がある execution_hub）。テストは TAKA_MA_WORKER_HOST で差し替える。
_WORKER_HOST = os.environ.get("TAKA_MA_WORKER_HOST", "mbp")
# 来歴マニフェストの host 値は pyinfra の _manifest._host_name と一致させる（execution_hub → "mbp"）。
_MANIFEST_HOST = "mbp"


def _ssh(remote: str, timeout: int = 600) -> str:
    """worker ホストで SSH コマンドを実行し stdout を返す。非ゼロ終了は RuntimeError。"""
    result = subprocess.run(
        ["ssh", _WORKER_HOST, remote],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"SSH 失敗: {remote}")
    return result.stdout


def pull_model(model_id: str) -> None:
    """worker ホストでローカルモデルをダウンロードする（ollama pull）。

    model_id は最終的に worker のログインシェルへ渡るため、シェルメタ文字による
    コマンドインジェクションを防ぐべく shlex.quote で 1 引数に確定させる。
    """
    logger.info("ollama pull %s on %s", model_id, _WORKER_HOST)
    _ssh(f"ollama pull {shlex.quote(model_id)}")


def remove_worker_model(model_id: str) -> None:
    """worker ホストからローカルモデルの実体を削除する（ollama rm）。

    yaml 登録の削除（model_store.remove_model）と区別するため worker 限定名にする。
    model_id は pull_model 同様 shlex.quote で 1 引数に確定させる（インジェクション防止）。
    """
    logger.info("ollama rm %s on %s", model_id, _WORKER_HOST)
    _ssh(f"ollama rm {shlex.quote(model_id)}")


def record_manifest(model_id: str) -> None:
    """来歴マニフェストへ models ステップを upsert する（best-effort）。

    pyinfra/deploys/_manifest.record と同じ自然キー（host, component="models",
    operation="ollama pull <id>"）で記録する。pull 済みモデルを全体アンインストールの
    LIFO 再生対象に含めるのが目的。記録失敗は install 自体の失敗にしない（実体は既に入っている）。
    """
    payload = {
        "component": "models",
        "operation": f"ollama pull {model_id}",
        "target": model_id,
        "teardown": {"op": "ollama.rm", "model": model_id},
        "host": _MANIFEST_HOST,
        "source": "slack",
    }
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    remote = (
        "/opt/taka-ma-env/bin/python -c "
        "\"import base64, json, sys; "
        "sys.path.insert(0, '/opt/taka-ma/lib'); "
        "import install_manifest as m; "
        f"m.record(**json.loads(base64.b64decode('{b64}').decode()))\""
    )
    try:
        _ssh(remote, timeout=30)
    except Exception as e:  # noqa: BLE001 — 記録は補助。失敗しても install は成功扱い。
        logger.warning("来歴記録に失敗（install は継続）: %s", e)
