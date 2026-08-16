"""共有ガード: 未マージコミットからの配備を停止する（#taka-ma/141）。

WHY: 2026-07-31、#135 未マージ worktree からの pyinfra 配備が、マージ済み #136 修正の
converse.md を実機上で修正前へ巻き戻した
（private/docs/incidents/2026-08-14-wave1-B-regression-report.md 検証1）。
worktree からの配備は「main に無いコミットの内容」を実機へ書き、他タスクの
マージ済み修正を静かに上書きしうる。

対策: 各 deploy の読み込み時（ensure_brew_path() と同位置）に ensure_merged_head() を
呼び、配備元リポジトリの HEAD が main（origin/main またはローカル main のいずれか）に
含まれることを検査する。未マージなら SystemExit で配備全体を停止する。

迂回: 意図して未マージのまま配備する場合のみ、環境変数 TAKA_MA_ALLOW_UNMERGED=1 を
付けて実行する（例: `TAKA_MA_ALLOW_UNMERGED=1 pyinfra -y @local pyinfra/deploys/...`）。
迂回時もその旨を stderr へ明示する。

判定ロジックは evaluate()（純粋関数）に分離してあり、git や pyinfra 無しで
単体テストできる（pyinfra/tests/test_deploy_guard.py）。
"""
import os
import subprocess
import sys

# 迂回フラグ（"1" のときのみ有効）。pyinfra の deploy 実行には任意 CLI 引数を
# 足せないため、_env.py と同様プロセス環境変数で受ける。
ALLOW_UNMERGED_ENV = "TAKA_MA_ALLOW_UNMERGED"

# HEAD の包含を確認する main 側の参照（先勝ちではなく「いずれかに含まれれば可」）。
# origin/main: push 済みの正。ローカル main: オフラインや fetch 前でも検査可能にする。
_MAIN_REFS = ("origin/main", "main")


def _git(args, repo_dir=None):
    """git を実行し (returncode, stdout) を返す。git 不在も returncode!=0 に畳む。"""
    try:
        res = subprocess.run(
            ["git", *args],
            capture_output=True, text=True, cwd=repo_dir,
        )
    except OSError:
        return 127, ""
    return res.returncode, res.stdout.strip()


def collect_state(repo_dir=None):
    """配備元リポジトリから判定材料を集める。

    返り値: (head, merged)
      head:   HEAD のコミットハッシュ。解決不能（git 不在・リポジトリ外）なら None
      merged: {参照名: HEAD がその参照に含まれるか(bool)}。存在しない参照は含めない
    """
    rc, head = _git(["rev-parse", "HEAD"], repo_dir)
    if rc != 0 or not head:
        return None, {}
    merged = {}
    for ref in _MAIN_REFS:
        rc, _ = _git(["rev-parse", "--verify", "--quiet", ref], repo_dir)
        if rc != 0:
            continue  # 参照そのものが無い（remote 未設定等）
        rc, _ = _git(["merge-base", "--is-ancestor", head, ref], repo_dir)
        merged[ref] = (rc == 0)
    return head, merged


def evaluate(head, merged, allow_unmerged):
    """(配備可か, 理由) を返す純粋関数。I/O を持たない。

    head:           HEAD ハッシュ（None = 解決不能）
    merged:         {参照名: 含まれるか}（存在した main 参照のみ）
    allow_unmerged: 迂回フラグ（環境変数 TAKA_MA_ALLOW_UNMERGED=1）
    """
    if allow_unmerged:
        return True, (
            f"{ALLOW_UNMERGED_ENV}=1 により未マージ検査を迂回します"
            "（main 済み修正を巻き戻すおそれがあります）"
        )
    if head is None:
        return False, (
            "配備元の HEAD コミットを解決できません（git 不在またはリポジトリ外）。"
            "リポジトリのルートから実行してください。"
            f"検査不能のまま配備する場合のみ {ALLOW_UNMERGED_ENV}=1 を付けてください。"
        )
    if not merged:
        return False, (
            f"main の参照（{' / '.join(_MAIN_REFS)}）が見つからず、"
            f"HEAD {head[:12]} のマージ済み判定ができません。"
            f"検査不能のまま配備する場合のみ {ALLOW_UNMERGED_ENV}=1 を付けてください。"
        )
    contained = [ref for ref, ok in merged.items() if ok]
    if contained:
        return True, f"HEAD {head[:12]} は {contained[0]} に含まれています（配備可）"
    return False, (
        f"HEAD {head[:12]} は {' / '.join(merged)} のいずれにも含まれていません。"
        "未マージ worktree からの配備は、マージ済み修正を実機で巻き戻すため停止します"
        "（2026-07-31 の #136 巻き戻し事故の再発防止）。"
        "main へマージしてから配備するか、意図的な未マージ配備の場合のみ "
        f"{ALLOW_UNMERGED_ENV}=1 を付けて再実行してください。"
    )


def ensure_merged_head(repo_dir=None):
    """配備元 HEAD が main に含まれることを検査し、未マージなら配備を停止する。

    各 deploy の冒頭（ensure_brew_path() の直後）で呼ぶ。@local 実行では
    リポジトリのルートが cwd（deploy が相対パスで src/ を参照する前提と同じ）。
    """
    allow = os.environ.get(ALLOW_UNMERGED_ENV, "") == "1"
    head, merged = collect_state(repo_dir)
    ok, reason = evaluate(head, merged, allow)
    if not ok:
        raise SystemExit(f"[deploy-guard] {reason}")
    print(f"[deploy-guard] {reason}", file=sys.stderr)
