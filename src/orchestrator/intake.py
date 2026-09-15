"""タスク受付の意図起票とブランチ束縛（設計書 §8.10h）— 依頼を検証系に載せるための前段。

設計適合レビュー（review-verify）の step0 は、依頼の意図を「ブランチ名 `task-<id>`」と
外部の意図記録 `projects/<project>/intents/<id>-<uuid8>.json` の 2 点から引く。この 2 点を
持たない依頼はレビュー系に載らず、`main` 直接作業のまま完了まで進む（2026-09-11 実測）。
従来はこの 2 点の用意を依頼文（「起票して task-<id> で作業しろ」）に書く運用に委ねており、
書き忘れがそのまま抜けになっていた。本モジュールは受付時（契約成立後・着手確認提示前）に
**依頼者への追加要求なしに**同じ 2 点を機械で用意する。

規律は runbook（§8.10g）と同じ:

- 判定は実測のみ（LLM 不関与）。実行手段は `run(command, timeout) -> (rc, output)` で注入し、
  本モジュールは SSH・subprocess を直接持たない（実機・モデルに依存しないテストのため）
- 採番と意図記録の書き出しは台帳 CLI（`ledger.py start`）が唯一の入口。ledger.json や
  intents を自前で書かない（台帳の不変条件「writer は台帳 CLI のみ」を侵さない）
- **ブランチ作成そのものはここでは行わない**。準備系 step（`branch_create` / `switch`）を
  返すだけで、世界を変えるのは人が着手確認で承認した runbook の実行時（= 最初の書き込み
  操作の直前）。受付は「測る・起票する・step を組む」までに留まる
"""

import re
import shlex
from typing import NamedTuple

# 起票とブランチ束縛の対象にする完了条件の kind（§8.10h 適用範囲）。**作業ツリーを変える
# 約束**だけを対象にする:
# - `answered`（回答型）は対象外 — 作業ツリー不変が完了条件そのものであり（§8.10f 工程 (2)）、
#   ブランチを切ると完了条件と矛盾する。読み取り・探索の依頼を受付で止めない
# - `pushed` / `remote_file` / `branch_merged` は ref 操作のみの約束 — 「main へ取り込め」型の
#   依頼に新ブランチを切ると依頼そのものを壊すため対象外
WORKTREE_KINDS = ("file", "file_changed", "file_min_bytes", "head_touches", "diff_limit")

# タスクブランチの命名（`task-<id>`。スラッグ付き `task-168_verify-intake` も同一タスク）。
# 台帳側の id 解決（ledger.py `_BRANCH_ID_RE`）と同一規則にする — ここで「既にタスク
# ブランチ上」と判定した HEAD は、レビュー系でも同じ id へ解決できなければ意味がない
TASK_BRANCH_RE = re.compile(r"(?:^|/)task-(\d+(?:-\d+)*)(?![0-9A-Za-z])")

# `ledger.py start` の出力からブランチ名を引く（1 行目 "…  ブランチ名: task-168  uuid: …"）。
# 引けない出力は起票の成否を確認できない＝不成立として扱う（fail-closed）
_LEDGER_BRANCH_RE = re.compile(r"ブランチ名:\s*(task-[A-Za-z0-9._/\-]+)")

# 台帳へ載せる title / requirement の上限（暴走値の拒否。台帳一覧・意図記録の可読性）
MAX_TITLE_LEN = 120
MAX_REQUIREMENT_LEN = 200
MAX_REQUIREMENTS = 5


class IntakeResult(NamedTuple):
    """受付判定の結果（世界は変えていない — status / branch / step / 証跡のみ）。

    status: 機械可読の判定コード。
      `skip:*`    受付の対象外（変更系でない・branch 明示済み・repo でない 等）
      `bound:*`   既存のタスクブランチへ束縛した（新規起票なし）
      `created:*` 台帳へ起票し、準備系 step でブランチを用意する
      `refuse:*`  変更系だがブランチを用意できない（着手させない・§8.10h fail-closed）
    branch: 契約へ束縛するブランチ名（skip / refuse では None）。
    step: runbook 先頭へ前置する準備系 step（既にブランチが在る場合は None or switch）。
    detail: 判定の根拠（実測コマンドと出力）。拒否文・ログへそのまま載せる。
    """

    status: str
    branch: str | None = None
    step: dict | None = None
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.status.startswith("refuse:")

    @property
    def bound(self) -> bool:
        return self.branch is not None


def needs_task_branch(contract: dict) -> bool:
    """契約が「作る・変える」型（作業ツリーを変更する）かを完了条件の kind から決める。

    判定材料は人が承認する契約フィールドのみ（散文・LLM 判定を使わない・§8.10h）。
    """
    kinds = {a.get("kind") for a in (contract or {}).get("acceptance") or []}
    return bool(kinds & set(WORKTREE_KINDS))


def _runbook_branch_step(contract: dict) -> str | None:
    """runbook に既にブランチを決める step（switch / branch_create）が在ればその名前を返す。

    受付の束縛と計画の switch が別々のブランチを指すと、作業先と完了検査の対象 ref が
    食い違う（ブランチ決定点の二重化）。在れば受付は手を出さない。
    """
    for step in (contract or {}).get("runbook") or []:
        params = (step or {}).get("params") or {}
        if (step or {}).get("kind") == "switch" and params.get("branch"):
            return str(params["branch"])
        if (step or {}).get("kind") == "branch_create" and params.get("name"):
            return str(params["name"])
    return None


def ledger_requirements(contract: dict) -> list[str]:
    """意図記録へ載せる要件列を契約から決定的に導く（逐語命令 → 拘束条件の順）。

    review-verify の要件別判定はこの列を読む。LLM に書かせず、人が着手確認で承認する
    契約フィールドの逐語をそのまま使う（記録と承認面の一致）。
    """
    texts: list[str] = []
    directive = (contract or {}).get("directive")
    if directive:
        texts.append(str(directive))
    for c in (contract or {}).get("constraints") or []:
        text = (c or {}).get("text")
        if not text:
            continue
        texts.append(("（禁止）" if c.get("forbid") else "") + str(text))
    return [_one_line(t, MAX_REQUIREMENT_LEN) for t in texts[:MAX_REQUIREMENTS]]


def build_ledger_start_command(workspace: str, *, ledger_py: str, python_bin: str,
                               title: str, requirements: list[str]) -> str:
    """`ledger.py start`（起票 + 意図記録）の実行コマンドを組み立てる。

    project は台帳 CLI がカレント git リポジトリから決めるため、workspace へ `cd` してから
    呼ぶ（`&&` で cd 失敗時に起票を走らせない）。全引数は shlex.quote で無害化する。
    """
    parts = [f"cd {shlex.quote(workspace)}", "&&", shlex.quote(python_bin),
             shlex.quote(ledger_py), "start",
             "--title", shlex.quote(_one_line(title, MAX_TITLE_LEN)),
             "--note", shlex.quote("sa-ru 受付の自動起票（§8.10h）")]
    for req in requirements:
        parts += ["--requirement", shlex.quote(req)]
    return " ".join(parts)


def parse_ledger_branch(output: str) -> str | None:
    """`ledger.py start` の出力からブランチ名（`task-<id>`）を引く。引けなければ None。"""
    m = _LEDGER_BRANCH_RE.search(output or "")
    return m.group(1) if m else None


def bind_task_branch(run, workspace: str | None, contract: dict, *,
                     ledger_py: str, python_bin: str, timeout: int,
                     title: str) -> IntakeResult:
    """受付時の起票とブランチ束縛を決める（§8.10h）。世界は変えない（測る・起票するのみ）。

    Args:
        run: 実測・起票の実行手段 `run(command, timeout) -> (rc, output)`（非 0 を例外化しない）。
             None は実行手段なし。
        workspace: 作業リポジトリの絶対パス（未解決は None）。
        contract: 成立済みの契約（§8.10f）。読むだけで書き換えない（束縛は呼び出し側）。
        ledger_py / python_bin: 台帳 CLI の絶対パスと実行バイナリ（sa-ru.yaml が唯一の供給元）。
        timeout: 1 コマンドの応答上限（秒）。
        title: 台帳 title に載せる確定要約（人が承認する要約そのもの）。
    """
    if (contract or {}).get("branch"):
        # 依頼者が明示したブランチが最優先（起票済みの継続・main への取り込み等）。
        # 既存の switch 機械付与（§8.10g）が従来どおり束縛を担う
        return IntakeResult("skip:branch_specified", detail="契約に branch が明示されている")
    if not needs_task_branch(contract or {}):
        # 回答型・ref 操作のみ = 作業ツリーを変えない依頼。受付で止めない（§8.10h 適用範囲）
        return IntakeResult("skip:not_mutating",
                            detail="作業ツリーを変更する完了条件を持たない")
    decided = _runbook_branch_step(contract or {})
    if decided:
        # 計画側が既にブランチを決めている（契約化が switch / branch_create を提案した）。
        # ここで別のブランチを起票すると、branch_create task-<id> の後ろで別ブランチへ
        # switch され、作業先（switch 先）と完了検査の対象 ref（契約 branch）が食い違う。
        # 決定点を二重に持たせない — 計画の決定を正とし、受付は起票を行わない
        return IntakeResult("skip:runbook_branch",
                            detail=f"runbook が既にブランチを決めている: {decided}")
    if not workspace or run is None:
        # リポジトリを要する依頼は先行する着手前ブロック（§8.10f needs_repo）が既に止めている。
        # ここへ来るのは repo を使わない生成依頼（使い捨て作業場）で、束縛すべき git が無い
        return IntakeResult("skip:no_workspace", detail="作業リポジトリが未解決（束縛対象なし）")

    git = f"git -C {shlex.quote(workspace)}"
    rc, _ = run(f"{git} rev-parse --is-inside-work-tree", timeout)
    if rc != 0:
        return IntakeResult("skip:not_git",
                            detail=f"workspace が git リポジトリでない: {workspace}")

    rc, out = run(f"{git} rev-parse --abbrev-ref HEAD", timeout)
    head = (out or "").strip()
    if rc != 0 or not head:
        # 変更系なのに現在位置が測れない = ブランチを用意できない（§8.10h fail-closed）
        return IntakeResult(
            "refuse:head_unmeasurable",
            detail=f"$ {git} rev-parse --abbrev-ref HEAD (rc={rc})\n{out or '（出力なし）'}")

    if TASK_BRANCH_RE.search(head):
        # 既にタスクブランチ上 = 起票済みの作業の続き。重複起票せず現 HEAD へ束縛する
        return IntakeResult("bound:head_task_branch", branch=head,
                            detail=f"HEAD は既にタスクブランチ: {head}")

    command = build_ledger_start_command(
        workspace, ledger_py=ledger_py, python_bin=python_bin,
        title=title, requirements=ledger_requirements(contract or {}))
    rc, out = run(command, timeout)
    evidence = f"$ {command} (rc={rc})\n{(out or '').strip() or '（出力なし）'}"
    if rc != 0:
        return IntakeResult("refuse:ledger_failed", detail=evidence)
    branch = parse_ledger_branch(out)
    if not branch:
        return IntakeResult("refuse:ledger_unparsed", detail=evidence)

    rc, _ = run(f"{git} rev-parse --verify refs/heads/{shlex.quote(branch)}", timeout)
    if rc == 0:
        # 既存タスクへの intent 後付け等でブランチが既に在る場合は作らず切り替える
        # （branch_create は既存ブランチを前提不成立で落とすため）
        step = {"kind": "switch", "params": {"branch": branch}}
    else:
        # 基点は現 HEAD（実測値）。作成は着手後の runbook 実行時 = 最初の書き込みの直前
        step = {"kind": "branch_create", "params": {"name": branch, "base": head}}
    return IntakeResult("created:ledger", branch=branch, step=step, detail=evidence)


def _one_line(text: str, limit: int) -> str:
    """改行・連続空白を潰して 1 行にし、上限で切る（台帳 CLI の引数へ載せるため）。"""
    flat = " ".join(str(text or "").split())
    return flat[:limit]
