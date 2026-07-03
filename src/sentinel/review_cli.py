"""qu-e Tier 2 レビュー CLI エントリポイント（設計書 §8.8）。

sa-ru が SSH + subprocess で本スクリプトを起動し、JSON 1 行で結果を受け取る。
プロセス常駐ではなく、呼び出しごとに 1 ショット実行する。

Usage（役割パッケージとして実行。PYTHONPATH に component 直下 /opt/taka-ma/qu-e を通す）:
    cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e python sentinel/review_cli.py --mode command --input '<command>' --context '<json>'
    cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e python sentinel/review_cli.py --mode diff --input '<diff>' --file-path '<path>'
"""

import argparse
import asyncio
import json

import yaml

from sentinel.reviewer import QueReviewer


def _load_config() -> dict:
    """config/qu-e.yaml をロードして dict で返す。"""
    with open("config/qu-e.yaml") as f:
        return yaml.safe_load(f)


def _build_reviewer(config: dict) -> QueReviewer:
    """設定から QueReviewer インスタンスを構築する。"""
    return QueReviewer(
        model=config["qu-e"]["model"],
        ollama_host=config["qu-e"]["ollama_url"],
        prompts_dir=config["qu-e"]["prompts_dir"],
    )


async def _run_command(reviewer: QueReviewer, command: str, context_json: str) -> dict:
    """command モード: context を JSON パースして review_command() を呼出。"""
    context = json.loads(context_json) if context_json else {}
    return await reviewer.review_command(command, context)


async def _run_diff(reviewer: QueReviewer, diff: str, file_path: str) -> dict:
    """diff モード: review_diff() を呼出。"""
    return await reviewer.review_diff(diff, file_path)


def main():
    """argparse → 設定ロード → reviewer 実行 → JSON 1 行を stdout へ。

    例外時は安全側（escalate）に倒して Tier 3 にフォールバックさせる(設計書 §8.8)。
    """
    parser = argparse.ArgumentParser(description="qu-e Tier 2 review CLI")
    parser.add_argument("--mode", choices=["command", "diff"], required=True)
    parser.add_argument("--input", required=True, help="command string or diff text")
    parser.add_argument("--context", default="{}", help="(command mode) JSON context")
    parser.add_argument("--file-path", default="", help="(diff mode) target file path")
    args = parser.parse_args()

    config = _load_config()
    reviewer = _build_reviewer(config)

    try:
        if args.mode == "command":
            result = asyncio.run(_run_command(reviewer, args.input, args.context))
        else:
            result = asyncio.run(_run_diff(reviewer, args.input, args.file_path))
    except json.JSONDecodeError as e:
        # 設計書 §8.8: JSON パースエラー → Tier 3 にエスカレート（安全側）
        result = {"decision": "escalate", "reason": f"JSON parse error: {e}", "risk_score": 0.0}
    except Exception as e:
        # ollama 接続失敗・タイムアウト等の予期しない例外も安全側に倒す（Tier 3 フォールバック）
        result = {"decision": "escalate", "reason": f"qu-e review error: {e}", "risk_score": 0.0}

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
