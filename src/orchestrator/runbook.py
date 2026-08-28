"""git ランブック実行 — 定型 git 操作を worker LLM 抜きで決定的に実行する（設計書 §8.10g）。

2026-08-27 インシデント（worker の独断 stash・無実行ステップの成功宣言・誤基点からの
ブランチ作成と独断 rebase）の恒久対策。原則は「決定的にできる仕事を LLM にさせない」:

- 実行できる操作は固定カタログ（RUNBOOK_KINDS）のみ。コマンド組立はコード側で行い、
  LLM の関与は kind / params の提案（契約化パス）までに限る（acceptance カタログと同じ規律）
- 前提（clean tree・FF 可能等）は実行前に実測し、不成立なら**修復（stash / rebase /
  reset）を試みず**実測つきで失敗として人へ返す（修復の判断は人の権限）
- 事後条件も実測し、満たされて初めて成功とする（無実行の成功宣言を構造的に排除）

実行手段（SSH）は呼び出し側から関数として注入する（grounding.GroundingVerifier と同じ形:
run(command, timeout) -> (rc, output)。非 0 を例外化しないこと）。
"""

import re
import shlex

# 実行コマンドの SSH タイムアウト（秒）。push はネットワークを跨ぐため長めに取る
# （grounding.LS_REMOTE_TIMEOUT_SEC と同じ根拠）
EXEC_TIMEOUT_SEC = 30
PUSH_TIMEOUT_SEC = 60

# 証跡へ載せる 1 コマンド出力の上限文字数（grounding._PROBE_OUTPUT_MAX_CHARS と同じ根拠）
_OUTPUT_MAX_CHARS = 1500

# ランブックの固定カタログ（§8.10g）。kind と params のみを受け、コマンドは本モジュールが
# 組み立てる。LLM に自由なコマンドを書かせない
RUNBOOK_KINDS = {
    "commit_paths": {"required": {"paths", "message"}, "optional": set()},
    "push": {"required": set(), "optional": {"branch"}},
    "merge_ff": {"required": {"source", "target"}, "optional": set()},
    "branch_create": {"required": {"name", "base"}, "optional": set()},
    "switch": {"required": {"branch"}, "optional": set()},
}

# ブランチ名・パスの受理形式。SSH コマンド文字列に乗るため安全文字のみ
# （contract._SAFE_PARAM_RE と同一規則。テストのファイル直ロードのため import で共有しない —
# `grep -n "A-Za-z0-9._/" src/orchestrator/contract.py src/orchestrator/runbook.py` で一致を確認する）
_SAFE_PARAM_RE = re.compile(r"\A[A-Za-z0-9._/\-]+\Z")

# コミットメッセージの上限（暴走値の拒否。組立時は shlex.quote で無害化する）
_MAX_MESSAGE_LEN = 200

# step 数・paths 数の上限。無上限だと巨大提案の逐語提示が Slack 側の表示切り詰め
# （PLAN_MAX_BLOCKS）に掛かり「見えていないコマンド列の承認」が起こり得る（§8.10g の
# 逐語承認原則の空洞化）。人が一覧で確認できる規模に固定で制限する
_MAX_STEPS = 10
_MAX_PATHS = 20


class RunbookError(Exception):
    """ランブック step の前提不成立・実行失敗・事後条件未達。

    cause: 機械可読の失敗原因コード（§8.10g。runbook_precondition:<kind> /
           runbook_failed:<kind>）。_failure_cause_from_error がそのまま採用する。
    """

    def __init__(self, cause: str, message: str):
        super().__init__(message)
        self.cause = cause


def validate_runbook(raw) -> tuple[list[dict] | None, list[str]]:
    """契約化パスの runbook 提案を検証・正規化する。(step 列, 逸脱理由リスト) を返す。

    逸脱が 1 つでもあれば None（fail-closed・contract.validate_contract と同じ規律）。
    """
    problems: list[str] = []
    steps: list[dict] = []
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return None, ["runbook が配列でない"]
    if len(raw) > _MAX_STEPS:
        return None, [f"runbook が {len(raw)} step（上限 {_MAX_STEPS}。"
                      "逐語提示を人が確認できる規模に収めること）"]
    for entry in raw:
        if not isinstance(entry, dict) or entry.get("kind") not in RUNBOOK_KINDS:
            problems.append(
                f"未知の runbook kind: {entry.get('kind') if isinstance(entry, dict) else entry}")
            continue
        kind = entry["kind"]
        spec = RUNBOOK_KINDS[kind]
        params = entry.get("params") or {}
        if not isinstance(params, dict):
            problems.append(f"runbook {kind} の params が dict でない")
            continue
        missing = spec["required"] - set(params)
        unknown = set(params) - spec["required"] - spec["optional"]
        bad = []
        for k, v in params.items():
            if k == "paths":
                if (not isinstance(v, list) or not v or len(v) > _MAX_PATHS
                        or not all(isinstance(p, str) and _SAFE_PARAM_RE.match(p)
                                   and ".." not in p.split("/") and not p.startswith("/")
                                   for p in v)):
                    bad.append(k)
            elif k == "message":
                if (not isinstance(v, str) or not v.strip() or "\n" in v
                        or len(v) > _MAX_MESSAGE_LEN):
                    bad.append(k)
            elif (not isinstance(v, str) or not _SAFE_PARAM_RE.match(v)
                    or ".." in v.split("/")):
                bad.append(k)
        if missing or unknown or bad:
            problems.append(
                f"runbook {kind} のパラメータ不正"
                f"（不足={sorted(missing)} 未知={sorted(unknown)} 不正値={sorted(bad)}）")
            continue
        steps.append({"kind": kind, "params": params})
    if problems:
        return None, problems
    return steps, []


def build_commands(workspace: str, kind: str, params: dict) -> list[str]:
    """1 step の実行コマンド列を組み立てる（着手確認の逐語提示と実行の両方が使う SSOT）。

    提示と実行が同じ組立を通ることで「承認した列と実行される列」が構造的に一致する。
    """
    ws = shlex.quote(workspace)
    git = f"git -C {ws}"
    if kind == "commit_paths":
        paths = " ".join(shlex.quote(p) for p in params["paths"])
        return [f"{git} add {paths}",
                f"{git} commit -m {shlex.quote(params['message'])}"]
    if kind == "push":
        branch = shlex.quote(params["branch"]) if params.get("branch") else "HEAD"
        return [f"{git} push origin {branch}"]
    if kind == "merge_ff":
        return [f"{git} switch {shlex.quote(params['target'])}",
                f"{git} merge --ff-only {shlex.quote(params['source'])}"]
    if kind == "branch_create":
        return [f"{git} switch -c {shlex.quote(params['name'])} "
                f"{shlex.quote(params['base'])}"]
    if kind == "switch":
        return [f"{git} switch {shlex.quote(params['branch'])}"]
    raise ValueError(f"未知の runbook kind: {kind}")


def render_step(workspace: str, kind: str, params: dict) -> str:
    """着手確認へ載せる 1 step の逐語コマンド表示（§8.10g「組立後コマンドの逐語提示」）。"""
    return "\n".join(build_commands(workspace, kind, params))


def step_already_done(run, workspace: str, kind: str, params: dict) -> tuple[bool, str]:
    """step の事後条件が既に成立しているかを実測する（§8.10g 残作業の導出）。

    失敗タスクへの自然な続き（「もう一回やって」）で、済んだ工程を計画に載せず
    **残作業だけ**を提示するための計画時測定。判定は run_step の事後実測と同じ
    観測のみ（LLM 不使用）。判定不能（SSH 不達・取得失敗）は False＝計画に載せる側へ
    倒す（実行時の前提実測が最終防衛。誤スキップより安全）。
    """
    ws = shlex.quote(workspace)
    git = f"git -C {ws}"
    rc, _ = run(f"{git} rev-parse --is-inside-work-tree", EXEC_TIMEOUT_SEC)
    if rc != 0:
        return False, ""
    if kind == "commit_paths":
        paths = " ".join(shlex.quote(p) for p in params["paths"])
        rc, out = run(f"{git} status --porcelain -- {paths}", EXEC_TIMEOUT_SEC)
        if rc == 0 and not (out or "").strip():
            return True, "対象パスに未コミット変更なし（コミット済み）"
    elif kind == "push":
        branch = params.get("branch")
        if not branch:
            rc, out = run(f"{git} rev-parse --abbrev-ref HEAD", EXEC_TIMEOUT_SEC)
            branch = (out or "").strip() if rc == 0 else ""
        if branch:
            rc1, local = run(f"{git} rev-parse {shlex.quote(branch)}", EXEC_TIMEOUT_SEC)
            rc2, remote = run(f"{git} ls-remote origin {shlex.quote(branch)}",
                              PUSH_TIMEOUT_SEC)
            loc = (local or "").split()[0] if rc1 == 0 and (local or "").strip() else ""
            rem = (remote or "").split()[0] if rc2 == 0 and (remote or "").strip() else ""
            if loc and rem and loc == rem:
                return True, f"{branch} の先端は remote と一致（push 済み）"
    elif kind == "merge_ff":
        rc1, tgt = run(f"{git} rev-parse {shlex.quote(params['target'])}", EXEC_TIMEOUT_SEC)
        rc2, src = run(f"{git} rev-parse {shlex.quote(params['source'])}", EXEC_TIMEOUT_SEC)
        if (rc1 == 0 and rc2 == 0 and (tgt or "").strip()
                and (tgt or "").strip() == (src or "").strip()):
            return True, f"{params['target']} は {params['source']} と同一先端（マージ済み）"
    elif kind == "switch":
        rc, out = run(f"{git} rev-parse --abbrev-ref HEAD", EXEC_TIMEOUT_SEC)
        if rc == 0 and (out or "").strip() == params["branch"]:
            return True, f"HEAD は既に {params['branch']}"
    elif kind == "branch_create":
        rc1, _ = run(f"{git} rev-parse --verify refs/heads/{shlex.quote(params['name'])}",
                     EXEC_TIMEOUT_SEC)
        rc2, out = run(f"{git} rev-parse --abbrev-ref HEAD", EXEC_TIMEOUT_SEC)
        if rc1 == 0 and rc2 == 0 and (out or "").strip() == params["name"]:
            return True, f"ブランチ {params['name']} は作成済みで HEAD"
    return False, ""


class _Runner:
    """証跡（$ cmd / rc / 出力）を積みながらコマンドを流す内部ヘルパ。"""

    def __init__(self, run):
        self._run = run
        self.lines: list[str] = []
        self.executed: list[str] = []

    def probe(self, command: str, timeout: int = EXEC_TIMEOUT_SEC) -> tuple[int, str]:
        """読み取り検査（前提・事後測定）。証跡に残すが遵守照合の実行記録には積まない。"""
        rc, output = self._run(command, timeout)
        out = (output or "").strip()
        if len(out) > _OUTPUT_MAX_CHARS:
            out = out[:_OUTPUT_MAX_CHARS] + "\n…（以降略）"
        self.lines.append(f"$ {command} (rc={rc})")
        self.lines.append(out if out else "（出力なし）")
        return rc, (output or "").strip()

    def exec(self, command: str, timeout: int = EXEC_TIMEOUT_SEC) -> tuple[int, str]:
        """状態変更コマンド。遵守照合（§8.10f _executed）用の実行記録にも積む。"""
        self.executed.append(command)
        return self.probe(command, timeout)


def _fail(kind: str, phase: str, message: str, runner: _Runner) -> RunbookError:
    """失敗原因コード付きの RunbookError を組み立てる（証跡を本文へ同梱）。"""
    cause = f"runbook_{phase}:{kind}"
    return RunbookError(cause, f"{message}\n" + "\n".join(runner.lines))


def run_step(run, workspace: str, kind: str, params: dict) -> tuple[list[str], str]:
    """1 つのランブック step を「前提実測 → 実行 → 事後実測」で走らせる（§8.10g）。

    返り値は (実行した状態変更コマンド列, 証跡テキスト)。前提不成立・実行失敗・事後
    未達は RunbookError（cause 付き）。**いかなる場合も修復（stash / rebase / reset /
    checkout juggling）を行わない** — 前提が崩れていたら実測を添えて人へ返す。
    """
    ws = shlex.quote(workspace)
    git = f"git -C {ws}"
    r = _Runner(run)
    r.lines.append(f"【runbook {kind}】workspace: {workspace}")

    # ── 共通前提: git リポジトリであること ──
    rc, _ = r.probe(f"{git} rev-parse --is-inside-work-tree")
    if rc != 0:
        raise _fail(kind, "precondition", "workspace が git リポジトリでない", r)

    # ── kind 別の前提実測（不成立は修復せず失敗） ──
    if kind == "commit_paths":
        paths = " ".join(shlex.quote(p) for p in params["paths"])
        rc, out = r.probe(f"{git} status --porcelain -- {paths}")
        if rc != 0 or not out:
            raise _fail(kind, "precondition",
                        "コミット対象パスに実変更がない（コミットするものが無い）", r)
    elif kind == "push":
        if params.get("branch"):
            rc, _ = r.probe(
                f"{git} rev-parse --verify refs/heads/{shlex.quote(params['branch'])}")
            if rc != 0:
                raise _fail(kind, "precondition",
                            f"push 対象ブランチ {params['branch']} が存在しない", r)
    elif kind in ("merge_ff", "switch"):
        rc, out = r.probe(f"{git} status --porcelain")
        if rc != 0 or out:
            n = len(out.splitlines()) if out else "不明"
            raise _fail(kind, "precondition",
                        f"作業ツリーが clean でない（未コミット変更 {n} 件。"
                        "退避・破棄はしません — 人の判断が必要です）", r)
        if kind == "merge_ff":
            src, tgt = shlex.quote(params["source"]), shlex.quote(params["target"])
            for ref, label in ((src, params["source"]), (tgt, params["target"])):
                rc, _ = r.probe(f"{git} rev-parse --verify refs/heads/{ref}")
                if rc != 0:
                    raise _fail(kind, "precondition", f"ブランチ {label} が存在しない", r)
            rc, _ = r.probe(f"{git} merge-base --is-ancestor {tgt} {src}")
            if rc != 0:
                raise _fail(kind, "precondition",
                            f"fast-forward 不可（{params['target']} は {params['source']} の"
                            "祖先でない）。マージ方法の判断が必要です", r)
        else:
            rc, _ = r.probe(
                f"{git} rev-parse --verify refs/heads/{shlex.quote(params['branch'])}")
            if rc != 0:
                raise _fail(kind, "precondition",
                            f"ブランチ {params['branch']} が存在しない", r)
    elif kind == "branch_create":
        rc, _ = r.probe(
            f"{git} rev-parse --verify refs/heads/{shlex.quote(params['name'])}")
        if rc == 0:
            raise _fail(kind, "precondition",
                        f"ブランチ {params['name']} は既に存在する", r)
        rc, _ = r.probe(f"{git} rev-parse --verify {shlex.quote(params['base'])}")
        if rc != 0:
            raise _fail(kind, "precondition", f"基点 {params['base']} が存在しない", r)

    # ── 実行（承認済みの組立コマンドのみ。失敗は即時停止・修復しない） ──
    timeout = PUSH_TIMEOUT_SEC if kind == "push" else EXEC_TIMEOUT_SEC
    for command in build_commands(workspace, kind, params):
        rc, _ = r.exec(command, timeout)
        if rc != 0:
            raise _fail(kind, "failed", f"コマンドが失敗した（rc={rc}）", r)

    # ── 事後実測（満たされて初めて成功 — 無実行の成功宣言を構造的に排除） ──
    if kind == "commit_paths":
        paths = " ".join(shlex.quote(p) for p in params["paths"])
        rc, out = r.probe(f"{git} status --porcelain -- {paths}")
        if rc != 0 or out:
            raise _fail(kind, "failed", "コミット後も対象パスに未コミット変更が残っている", r)
    elif kind == "push":
        branch = params.get("branch")
        if not branch:
            rc, out = r.probe(f"{git} rev-parse --abbrev-ref HEAD")
            branch = out if rc == 0 and out else None
        local = remote = ""
        if branch:
            rc, out = r.probe(f"{git} rev-parse {shlex.quote(branch)}")
            local = out.split()[0] if rc == 0 and out else ""
            rc, out = r.probe(f"{git} ls-remote origin {shlex.quote(branch)}",
                              timeout=PUSH_TIMEOUT_SEC)
            remote = out.split()[0] if rc == 0 and out else ""
        if not (local and remote and local == remote):
            raise _fail(kind, "failed",
                        f"push を remote 実出力で確認できない"
                        f"（remote={remote[:12] or '取得不能'} local={local[:12] or '取得不能'}）", r)
    elif kind == "merge_ff":
        rc1, head = r.probe(f"{git} rev-parse --abbrev-ref HEAD")
        rc2, tgt_hash = r.probe(f"{git} rev-parse {shlex.quote(params['target'])}")
        rc3, src_hash = r.probe(f"{git} rev-parse {shlex.quote(params['source'])}")
        if not (rc1 == 0 and head == params["target"]
                and rc2 == 0 and rc3 == 0 and tgt_hash == src_hash):
            raise _fail(kind, "failed",
                        f"マージ後の実測が不一致（HEAD={head} target先端と source 先端の一致を"
                        "確認できない）", r)
    elif kind == "branch_create":
        rc1, head = r.probe(f"{git} rev-parse --abbrev-ref HEAD")
        rc2, head_hash = r.probe(f"{git} rev-parse HEAD")
        rc3, base_hash = r.probe(f"{git} rev-parse {shlex.quote(params['base'])}")
        if not (rc1 == 0 and head == params["name"]
                and rc2 == 0 and rc3 == 0 and head_hash == base_hash):
            raise _fail(kind, "failed",
                        f"ブランチ作成後の実測が不一致（HEAD={head}・基点一致を確認できない）", r)
    elif kind == "switch":
        rc, head = r.probe(f"{git} rev-parse --abbrev-ref HEAD")
        if rc != 0 or head != params["branch"]:
            raise _fail(kind, "failed", f"切替後の HEAD が {params['branch']} でない（{head}）", r)

    r.lines.append(f"判定: runbook {kind} 成功（前提・事後とも実測 PASS）")
    return r.executed, "\n".join(r.lines)
