"""decide クライアント — PreToolUse フックの薄い入口（Mac mini 上で SSH 経由起動）。

headless アダプタのフックコマンド（MBP 側）が

    ssh <mac-mini> python3 decide_client.py --socket ... --task-id ... [...]

で起動する。標準ライブラリのみに依存し（venv / PYTHONPATH / 判定系 import に非依存）、
フック stdin の JSON とタスク文脈（argv）を decide デーモン（decide_daemon.py・launchd
常駐）の Unix ドメインソケットへ渡し、{"allow", "reason"} を受けてフックの出力契約へ
変換する。判定依存の import をここに持たないことで、依存解決の失敗でフック自体が壊れる
事故（旧 decide_cli の import 断線）を構造的に無くす（設計 Appendix §2.1）。

出力契約（Claude Code の PreToolUse フック・実機検証で確定）:
  - allow: stdout に permissionDecision:"allow" の JSON を出し exit 0（default-deny を上書き許可）
  - deny / 全異常（デーモン到達不可・応答タイムアウト・例外）: stderr に理由を出し exit 2
    （fail-closed。exit 2 以外の非 0 はフックエラーとして Claude Code の既定権限評価に落ち、
    read 系ツールが承認を素通りし得るため、exit 0/2 以外で終わる経路を持たない）

構築手順書: docs/procedures/08-approval-pipeline.md
"""

import argparse
import json
import socket
import sys

# デーモン応答の待ち上限（秒）。デーモンの 1 判定上限 305 秒より外側・フック timeout
# 310 秒より内側に置き、フック timeout 経路（挙動が Claude 側実装依存）に委ねず自前の
# exit 2 で確定させる（Appendix §2.1 タイムアウト設計）。
_RESPONSE_TIMEOUT_SEC = 308


def _emit_allow(reason: str):
    """フックへ allow を返す（permissionDecision:"allow" で default-deny を上書き）。"""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": reason or "approved",
    }}))
    sys.exit(0)


def _emit_deny(reason: str):
    """フックへ deny を返す（exit 2 でツール実行をブロック）。"""
    print(reason or "denied", file=sys.stderr)
    sys.exit(2)


def _request(args, payload_raw: str) -> dict:
    """デーモンへ判定リクエスト 1 行 JSON を送り、応答 dict を返す（失敗は例外）。"""
    payload = json.loads(payload_raw or "{}")
    if not isinstance(payload, dict):
        # フックは常に JSON オブジェクトを渡す（実機確認）。非オブジェクトは想定外＝安全側で拒否。
        raise ValueError("hook payload is not a JSON object")
    request = {
        "payload": payload,
        "task_id": args.task_id,
        "team_id": args.team_id,
        "channel": args.channel,
        "thread_ts": args.thread_ts,
        "instance_id": args.instance_id,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        # settimeout はソケット操作ごとの上限。UDS の connect / send はローカルで即時のため、
        # 実質の待ちは応答 recv（Tier3 人間待ちを含む）1 回に集中する。
        sock.settimeout(_RESPONSE_TIMEOUT_SEC)
        sock.connect(args.socket)
        sock.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break  # デーモン側クローズ。受信済み分の解釈は下の json.loads に委ねる
            buf += chunk
    response = json.loads(buf.decode("utf-8"))
    if not isinstance(response, dict):
        raise ValueError("daemon response is not a JSON object")
    return response


def main():
    """フック 1 回分の中継を行う: stdin + argv → デーモン → allow(exit 0) / deny(exit 2)。"""
    # sa-ru が settings 生成時に焼き込む task 文脈（Tier3 承認リクエストの応答先特定に使う）。
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/opt/taka-ma/data/decide.sock")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--team-id", default=None)         # 送信元ワークスペース（§8.10）
    parser.add_argument("--channel", default=None)         # 承認リクエストの投稿先チャンネル
    parser.add_argument("--thread-ts", default=None)       # 同一スレッドへ返すための起点 ts
    parser.add_argument("--instance-id", default="")       # 監査ログ用の worker 識別子
    args = parser.parse_args()

    try:
        response = _request(args, sys.stdin.read())
    except Exception as e:
        # デーモン到達不可・タイムアウト・payload 不正など、判定不能は全て deny（fail-closed）。
        _emit_deny(f"decide_client error (fail-safe deny): {e}")
    if response.get("allow") is True:
        _emit_allow(response.get("reason", ""))
    _emit_deny(response.get("reason", ""))


if __name__ == "__main__":
    main()
