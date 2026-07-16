"""qu-e コミット前監査ゲート CLI（設計書 §8.12 コミット前ゲート）。

git pre-commit フック（sentinel/hooks/pre-commit）から 1 ショットで呼ばれ、
staged diff（git diff --cached）を qu-e LLM が審査する。approve のみ exit 0
（コミット続行）。deny / escalate / 判定不能・LLM 不達は exit 1（コミット中断、
fail-closed）。結果は file_audit と同じ監査 jsonl に event="commit" で追記する。

Usage（review_cli.py と同型。SSH 非ログインシェル・フック環境でも動くよう venv 絶対パスで呼ぶ）:
    cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e \
        /opt/taka-ma-env/bin/python sentinel/commit_audit_cli.py --repo /abs/path/to/repo
"""

import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys
import uuid

import yaml

from sentinel.file_auditor import _DIFF_MAX_CHARS
from sentinel.reviewer import QueReviewer

# コミット監査判定の有効値。正規化できないものは escalate（＝コミット中断）に倒す
_VALID_DECISIONS = ("approve", "deny", "escalate")


def _load_config() -> dict:
    """config/qu-e.yaml をロードして dict で返す（cwd=/opt/taka-ma/qu-e 前提。review_cli と同じ）。"""
    with open("config/qu-e.yaml") as f:
        return yaml.safe_load(f)


def _build_reviewer(config: dict) -> QueReviewer:
    """設定から QueReviewer を構築する（file_audit 常駐・Tier2 と推論直列化ロックを共有）。"""
    return QueReviewer(
        model=config["qu-e"]["model"],
        ollama_host=config["qu-e"]["ollama_url"],
        prompts_dir=config["qu-e"]["prompts_dir"],
        # ロックパス・審査タイムアウトとも qu-e.yaml が唯一の源（コード既定値なし）
        inference_lock=config["qu-e"]["inference_lock"],
        review_timeout_sec=config["qu-e"]["review_timeout_sec"],
    )


def _staged_changes(repo: str) -> tuple[list[str], str]:
    """staged なファイル一覧と diff 本文を返す。git 失敗は例外送出（呼び出し側で fail-closed）。"""
    files = subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "--name-only"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout.split()
    diff = subprocess.run(
        ["git", "-C", repo, "diff", "--cached"],
        capture_output=True, text=True, timeout=15, check=True,
    ).stdout
    # 巨大 diff はプロンプト肥大（コンテキスト溢れ・判定劣化）を防ぐため file_audit と
    # 同じ上限で切り詰める（§8.12 diff 要約）
    if len(diff) > _DIFF_MAX_CHARS:
        diff = diff[:_DIFF_MAX_CHARS] + "\n...(truncated)"
    return files, diff


def _normalize_decision(result) -> tuple[str, str]:
    """LLM 応答から (decision, reason) を fail-closed で正規化する。

    reviewer.review_diff の応答形式（decision/issues/severity）は LLM 由来で崩れうる。
    approve と確定的に言えないもの（非 dict・未知値・キー欠落）はすべて escalate に倒し、
    「approve のみコミット続行」を唯一の分岐基準にする（§8.12 fail-closed 原則）。
    """
    if not isinstance(result, dict):
        return "escalate", f"qu-e 応答が JSON オブジェクトでない（{type(result).__name__}）"
    decision = result.get("decision")
    norm = decision.strip().lower() if isinstance(decision, str) else None
    if norm not in _VALID_DECISIONS:
        return "escalate", f"qu-e 判定が不正（decision={decision!r}）"
    issues = result.get("issues")
    reason = result.get("reason") or (
        "; ".join(str(i) for i in issues) if isinstance(issues, list) and issues else "")
    return norm, reason


def _append_jsonl(log_dir: str, record: dict):
    """監査レコードを file_audit と同じ日付別 jsonl に追記する（同一 log_dir・retention 共通）。"""
    os.makedirs(log_dir, exist_ok=True)
    date = datetime.date.today().isoformat()
    path = os.path.join(log_dir, f"file-audit-{date}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def audit_commit(config: dict, repo: str, reviewer=None) -> dict:
    """staged diff を qu-e に審査させ、監査レコード dict を返す（本体・テスト可能な単位）。

    戻り値 record の decision が approve のときのみコミット続行。staged 変更が無い場合は
    審査せず approve（監査対象が無い）。LLM 呼び出し・git 取得の例外は呼び出し側（main）で
    escalate に倒す。
    """
    files, diff = _staged_changes(repo)
    if not files:
        # 監査対象の変更が無い（通常 git 側が空コミットを弾くため稀）。記録せず素通し
        return {"decision": "approve", "reason": "staged 変更なし", "files": []}

    if reviewer is None:
        reviewer = _build_reviewer(config)
    result = asyncio.run(reviewer.review_diff(diff, ", ".join(files)))
    decision, reason = _normalize_decision(result)

    record = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "commit",
        "path": repo,
        "files": files,
        "decision": decision,
        "reason": reason,
        "severity": result.get("severity", "") if isinstance(result, dict) else "",
        "task_id": "",
        "command": "",
        "status": "none",
    }
    # 記録失敗で判定結果（コミット可否）を変えない。証跡欠落は stderr に残す
    try:
        _append_jsonl(config["file_audit"]["log_dir"], record)
    except OSError as e:
        print(f"[qu-e] 監査 jsonl 追記失敗（判定は有効）: {e}", file=sys.stderr)
    return record


def main():
    """argparse → 設定ロード → staged diff 審査 → 判定を表示し exit code で返す。

    approve → exit 0（コミット続行）。それ以外・例外 → exit 1（コミット中断、fail-closed）。
    """
    parser = argparse.ArgumentParser(description="qu-e commit audit gate CLI")
    parser.add_argument("--repo", required=True, help="監査対象リポジトリの絶対パス")
    args = parser.parse_args()

    try:
        config = _load_config()
        record = audit_commit(config, args.repo)
    except Exception as e:
        # git 不達・ollama 不達・設定破損など判定不能はすべてコミット中断（fail-closed）
        print(f"[qu-e] コミット監査を実行できません（fail-closed で中断）: {e}", file=sys.stderr)
        sys.exit(1)

    decision = record["decision"]
    if decision == "approve":
        print("[qu-e] コミット監査: approve")
        sys.exit(0)
    print(f"[qu-e] コミット監査: {decision} — {record.get('reason', '')}", file=sys.stderr)
    print("[qu-e] コミットを中断しました。指摘を確認・修正して再コミットしてください。",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
