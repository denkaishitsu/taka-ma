"""承認対象の実体採取・TOCTOU 照合（設計書 §3.3 (5)・#172）。

Tier3 承認リクエストの操作が指すファイルの実体（行番号付き・上限つき）を機械採取して
承認リクエストと承認レコードへ添付し、承認時の内容ハッシュを固定して実行直前に再照合する。

規律:
- 対象の特定は決定的 — tool_input のコマンド文字列をトークン分割し、worker ホスト上に
  **実在するファイル**だけを対象にする（実在確認は test -f の実測のみ。散文推測・LLM 不関与）
- 採取はコード側の固定コマンド（cat -n / shasum）のみ。LLM 出力をコマンドに接続しない
- 採取できない対象は「未添付」を明示する（未添付を無印で通さない）
- 照合不一致は fail-closed（allow を発行しない）。通知には差分の要約と正本ファイルを添える
"""

import difflib
import logging
import os
import re
import shlex
import subprocess

logger = logging.getLogger(__name__)

# パスの受理形式（SSH コマンド文字列に乗るため防御的に検証。exit_gate._SAFE_PATH_RE と
# 同一規則 — `grep -n "A-Za-z0-9._/" src/orchestrator/exit_gate.py src/approval-pipeline/evidence.py`）
_SAFE_PATH_RE = re.compile(r"\A[A-Za-z0-9._/\-]+\Z")

_PROBE_TIMEOUT_SEC = 15   # 実在確認・ハッシュ 1 回の上限
_CAT_TIMEOUT_SEC = 30     # 内容採取 1 回の上限
_TOKEN_MAX = 20           # コマンドから調べるトークン数の上限
_MAX_FILES = 5            # 添付対象ファイル数の上限
_FILE_CAP = 8000          # 1 ファイルの採取上限（文字）
_TOTAL_CAP = 24000        # 採取合計の上限（文字）
_SLACK_CAP = 1500         # Slack 承認リクエストへ載せる実体ブロックの上限（文字）
_DIFF_CAP = 1500          # 不一致通知へ載せる差分の上限（文字）


def ssh_probe_factory(host: str):
    """worker ホストへの読み取り専用 probe（rc, stdout）を返す。

    Tier2（qu-e 審査）と同じ SSH 1 ショット。非 0 終了は例外にせず rc で返す。
    """
    def run_probe(command: str, timeout: int = _PROBE_TIMEOUT_SEC):
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             host, command],
            capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    return run_probe


def candidate_tokens(pending) -> list:
    """承認対象からパス候補トークンを決定的に取り出す（LLM 不関与）。"""
    ti = getattr(pending, "tool_input", None) or {}
    name = getattr(pending, "tool_name", "")
    if name in ("", "Bash"):
        cmd = ti.get("command") or ""
        try:
            toks = shlex.split(cmd)
        except ValueError:
            toks = cmd.split()
        return toks[:_TOKEN_MAX]
    # 書き込み系ツールは対象パスそのもの（既存ファイルの現状が判断材料になる）
    fp = ti.get("file_path")
    return [fp] if isinstance(fp, str) else []


def resolve_existing(run_probe, cwd: str, tokens: list) -> list:
    """トークンのうち worker ホスト上に実在するファイルの絶対パスだけを返す。

    実在確認は `test -f` の実測のみ（拡張子・語形の推測はしない — 推測ベースの判定は
    P2 で廃した型）。オプション風トークン（- 始まり）・不正文字・`..` は対象外。
    """
    found: list = []
    seen = set()
    for t in tokens:
        if not isinstance(t, str) or not t or t.startswith("-"):
            continue
        if not _SAFE_PATH_RE.match(t) or ".." in t.split("/"):
            continue
        if t.startswith("/"):
            path = t
        elif cwd:
            path = os.path.join(cwd, t)
        else:
            continue
        if path in seen:
            continue
        seen.add(path)
        try:
            rc, _ = run_probe(f"test -f {shlex.quote(path)}", _PROBE_TIMEOUT_SEC)
        except Exception:
            continue
        if rc == 0:
            found.append(path)
            if len(found) >= _MAX_FILES:
                break
    return found


def _sha(run_probe, path: str):
    """(sha256 | None)。読めなければ None。"""
    try:
        rc, out = run_probe(f"shasum -a 256 {shlex.quote(path)}", _PROBE_TIMEOUT_SEC)
    except Exception:
        return None
    parts = (out or "").split()
    return parts[0] if rc == 0 and parts else None


def collect(run_probe, paths: list) -> tuple:
    """実体（ハッシュ＋行番号付き抜粋）を採取する。戻り値は (files, notes)。

    files: [{"path", "sha256", "excerpt", "truncated"}]（承認レコードへ保存）。
    notes: 採取できなかった対象の明示行（未添付を無印で通さない）。
    """
    files: list = []
    notes: list = []
    total = 0
    for path in paths:
        sha = _sha(run_probe, path)
        if sha is None:
            notes.append(f"{path}: ハッシュを採取できません — 実体未添付・照合対象外")
            continue
        excerpt = ""
        truncated = False
        try:
            rc, out = run_probe(f"cat -n {shlex.quote(path)}", _CAT_TIMEOUT_SEC)
        except Exception:
            rc, out = 1, ""
        if rc != 0 or not (out or "").strip():
            notes.append(f"{path}: 内容を読めません（バイナリ等）— 抜粋未添付（ハッシュのみ照合）")
        else:
            excerpt = out
            if len(excerpt) > _FILE_CAP:
                excerpt = excerpt[:_FILE_CAP]
                truncated = True
            room = _TOTAL_CAP - total
            if len(excerpt) > room:
                excerpt = excerpt[:max(room, 0)]
                truncated = True
            total += len(excerpt)
            if truncated:
                notes.append(f"{path}: 抜粋は上限で切り詰め（照合はファイル全体のハッシュで行う）")
        files.append({"path": path, "sha256": sha,
                      "excerpt": excerpt, "truncated": truncated})
    return files, notes


def slack_text(files: list, notes: list) -> str:
    """Slack 承認リクエストへ載せる実体ブロック（上限つき・コード組立のみ）。"""
    if not files and not notes:
        return ""
    lines = ["承認対象の実体（機械採取・実行時に同一性を照合します）:"]
    for f in files:
        lines.append(f"--- {f['path']} (sha256 {f['sha256'][:12]}…) ---")
        if f.get("excerpt"):
            lines.append(f["excerpt"].rstrip("\n"))
        if f.get("truncated"):
            lines.append("…（以降略・全文は承認レコードを参照）")
    for n in notes:
        lines.append(f"⚠ {n}")
    text = "\n".join(lines)
    if len(text) > _SLACK_CAP:
        text = text[:_SLACK_CAP] + "\n…（以降略・全文は承認レコードを参照）"
    return text


def verify(run_probe, files: list) -> list:
    """承認時に固定したハッシュと現在の実測を照合し、不一致の一覧を返す（空 = 一致）。

    再計測できない（ファイル消失・probe 失敗）も不一致として返す — fail-closed。
    """
    mismatches: list = []
    for f in files:
        current = _sha(run_probe, f["path"])
        if current != f["sha256"]:
            mismatches.append({"path": f["path"],
                               "approved_sha256": f["sha256"],
                               "current_sha256": current or "（再計測不能・不在）"})
    return mismatches


def mismatch_report(run_probe, files: list, mismatches: list) -> tuple:
    """不一致通知の (要約テキスト, 差分全文) を機械生成する。

    差分は「承認時に採取した抜粋 ⇄ 現在の実測抜粋」の unified diff（抜粋の範囲での
    近似 — 照合の正はハッシュであり、差分は人の判断材料）。
    """
    by_path = {f["path"]: f for f in files}
    summary_lines = []
    full_lines = []
    for m in mismatches:
        path = m["path"]
        stored = (by_path.get(path, {}).get("excerpt") or "").splitlines()
        try:
            rc, out = run_probe(f"cat -n {shlex.quote(path)}", _CAT_TIMEOUT_SEC)
            current = out.splitlines() if rc == 0 else []
        except Exception:
            current = []
        diff = list(difflib.unified_diff(stored, current,
                                         fromfile=f"{path}（承認時）",
                                         tofile=f"{path}（現在）", lineterm=""))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        summary_lines.append(f"- {path}: +{added}/−{removed} 行（抜粋範囲の実測差分）")
        full_lines.append("\n".join(diff) if diff else f"{path}: 差分を生成できません（抜粋外の変更の可能性）")
    summary = "\n".join(summary_lines)
    full = "\n\n".join(full_lines)
    return summary, full
