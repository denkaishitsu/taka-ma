"""会話⇄実行の受け渡し契約（設計書 §8.10f）— 契約の検証・機械補助の純粋ロジック。

契約化パス（脳 LLM の第 2 呼び出し）の出力は「提案」であり、ここでの検証を通った値だけが
契約フィールドとして効力を持つ。LLM 呼び出し・ファイル I/O は置かない（conversation.py /
orchestrator が担う）。テストが実機・モデルに依存しないようにするための分離。
"""

import re

# 完了条件の検査カタログ（§8.10f）。LLM に自由な検査コマンドを書かせず、検査はこの
# kind とパラメータの組に限定する（probe と同じ「LLM 出力を任意コマンド実行に接続しない」規律）。
# コマンド組立は grounding.GroundingVerifier.verify_acceptance（コード側）が行う。
ACCEPTANCE_KINDS = {
    "pushed": {"required": set(), "optional": {"branch"}},
    "remote_file": {"required": {"branch", "path"}, "optional": set()},
    "file": {"required": {"path"}, "optional": set()},
    "head_touches": {"required": {"path"}, "optional": set()},
}

# リポジトリ系の検査（§8.10f needs_repo の機械補助: これを含む契約は実リポジトリを要する）
REPO_KINDS = {"pushed", "remote_file", "head_touches"}

# 検査パラメータ（branch / path）の受理形式。SSH コマンド文字列に乗るため安全文字のみに
# 制限する（workspace の _SAFE_WORKSPACE_RE と同じ発想。`..` 成分も拒否）
_SAFE_PARAM_RE = re.compile(r"\A[A-Za-z0-9._/\-]+\Z")

# directive 内の git コマンド検出（needs_repo の機械補助）
_GIT_CMD_RE = re.compile(r"(?<![\w/\-])git\s+[a-z]", re.IGNORECASE)

# 禁止型拘束の deny パターンの最小長。短すぎる部分文字列（"git" 等）は正当な操作まで
# 巻き添えで塞ぐため拒否する（deny は安全側だが、無差別ブロックは運用不能になる）
_MIN_DENY_PATTERN_LEN = 4

# 失敗原因コード → 人へ報告する「必要な入力」（§8.10f 対応表）
_REQUIRED_INPUT = {
    "workspace_not_repo": "`repo:/絶対パス` で実開発リポジトリを指定してください",
    "git_remote_unreachable": "worker ホスト側の remote 設定・鍵を確認してください",
    "anthropic_auth": "worker CLI の認証を更新してください",
    "approval_rejected": "依頼内容を見直してください（却下された操作は自動再試行しません)",
    "ssh_unreachable": "sa-ru → worker ホストの SSH 接続を確認してください",
    "push_unverified": "remote へ push が届いていません。remote 設定・ブランチ名を確認してください",
    "directive_not_followed": "命令どおりのコマンドが実行されていません。命令内容を確認してください",
    "worker_timeout": "処理が時間内に終わりません。依頼の分割を検討してください",
}


def _norm_ws(text: str) -> str:
    """空白正規化（逐語照合・遵守照合用）。改行・連続空白を単一空白へ畳む。"""
    return " ".join((text or "").split())


def is_verbatim(candidate: str, source_text: str) -> bool:
    """candidate が source_text（会話のユーザー発話連結）に逐語で現れるか（空白差は無視）。

    契約化パスの directive / constraints.text は発話からの逐語引用に限る（§8.10f。
    原文に無い命令・拘束を LLM が生成することを構造的に拒否する）。
    """
    if not candidate or not candidate.strip():
        return False
    return _norm_ws(candidate) in _norm_ws(source_text)


def validate_contract(raw, source_text: str) -> tuple[dict | None, list[str]]:
    """契約化パスの LLM 出力を検証・正規化する。(契約 dict, 逸脱理由リスト) を返す。

    逸脱が 1 つでもあれば契約は None（fail-closed。呼び出し側がリトライ / 人へ差し戻す）。
    正規化済み契約は {directive, constraints, acceptance, workspace, needs_repo}。
    workspace はここでは文字列のまま返す（絶対パス検証・~ 展開は conversation.parse_workspace
    と同一規則に通す責務が呼び出し側にある。検証規則を二重に持たない）。
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["契約出力が JSON オブジェクトでない"]

    # directive: null または発話からの逐語引用。機械が判定するのは逐語性のみで、
    # 「実行可能なコマンドか」は判定しない（言語・形での機械判別は場当たりになる。
    # 命令かどうかの最終判断は着手確認の提示で人間が行う・§8.10f）
    directive = raw.get("directive")
    if directive is not None:
        if not isinstance(directive, str) or not directive.strip():
            problems.append("directive が文字列でない")
            directive = None
        elif not is_verbatim(directive, source_text):
            problems.append("directive が発話の逐語引用でない（原文に無い命令の生成は禁止）")
            directive = None
        else:
            directive = directive.strip()

    # constraints: [{text, forbid, patterns?}] — text は逐語引用のみ
    constraints: list[dict] = []
    for c in raw.get("constraints") or []:
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not c["text"].strip():
            problems.append("constraints の要素が {text, forbid} 形式でない")
            continue
        text = c["text"].strip()
        if not is_verbatim(text, source_text):
            problems.append(f"拘束条件が発話の逐語引用でない: {text[:40]}")
            continue
        forbid = bool(c.get("forbid"))
        patterns = []
        if forbid:
            for p in c.get("patterns") or []:
                # deny パターンは禁止型のみ・単一行・最小長以上（短すぎる語は無差別ブロック）
                if (isinstance(p, str) and "\n" not in p
                        and len(p.strip()) >= _MIN_DENY_PATTERN_LEN):
                    patterns.append(p.strip())
        constraints.append({"text": text, "forbid": forbid, "patterns": patterns})

    # acceptance: カタログ検証（未知 kind・パラメータ不正は逸脱）
    acceptance: list[dict] = []
    for a in raw.get("acceptance") or []:
        if not isinstance(a, dict) or a.get("kind") not in ACCEPTANCE_KINDS:
            problems.append(f"未知の検査 kind: {a.get('kind') if isinstance(a, dict) else a}")
            continue
        kind = a["kind"]
        spec = ACCEPTANCE_KINDS[kind]
        params = a.get("params") or {}
        if not isinstance(params, dict):
            problems.append(f"検査 {kind} の params が dict でない")
            continue
        missing = spec["required"] - set(params)
        unknown = set(params) - spec["required"] - spec["optional"]
        bad = [k for k, v in params.items()
               if not isinstance(v, str) or not _SAFE_PARAM_RE.match(v)
               or ".." in v.split("/")]
        if missing or unknown or bad:
            problems.append(
                f"検査 {kind} のパラメータ不正"
                f"（不足={sorted(missing)} 未知={sorted(unknown)} 不正値={sorted(bad)}）")
            continue
        acceptance.append({"kind": kind, "params": params})

    if problems:
        return None, problems

    # needs_repo: 脳の判定に機械補助を重ねる（§8.10f。directive の git コマンド・
    # リポジトリ系検査があれば強制 true。脳が false と言っても機械判定が勝つ）
    needs_repo = bool(raw.get("needs_repo"))
    if directive and _GIT_CMD_RE.search(directive):
        needs_repo = True
    if any(a["kind"] in REPO_KINDS for a in acceptance):
        needs_repo = True

    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        workspace = None

    return {
        "directive": directive,
        "constraints": constraints,
        "acceptance": acceptance,
        "workspace": workspace,
        "needs_repo": needs_repo,
    }, []


# 依頼が push を含むかの検出（既定検査の自動付与用）。語彙は grounding.py の主張検出
# _PUSH_CLAIM_RE と同一に保つ（`grep -n "プッシュ" src/orchestrator/*.py` で一致を確認する）
_PUSH_WORD_RE = re.compile(r"push(?:ed)?|プッシュ", re.IGNORECASE)


def apply_default_acceptance(contract: dict, summary: str) -> dict:
    """git push を含む依頼で acceptance が空なら既定検査（push 整合）を付与する（§8.10f）。

    脳が完了条件を立て損ねても「push の実測検査なしに完了と言える」状態を作らないための
    機械補助。判定は決定的（push 語の検出のみ・LLM 不要）。push を含まない依頼には
    付与しない（編集のみのタスクに pushed 検査を課すと誤未達になる）。
    """
    if contract.get("acceptance"):
        return contract
    text = f"{summary}\n{contract.get('directive') or ''}"
    if _PUSH_WORD_RE.search(text):
        contract["acceptance"] = [{"kind": "pushed", "params": {}}]
    return contract


def directive_command(directive: str) -> str:
    """逐語命令の worker 指示文（定型・§8.10f）。分解・言い換えを通さず原文を運ぶ。"""
    return ("以下のコマンドを記載どおり逐語実行し、実行したコマンドと実出力のみを報告せよ。"
            "別の手順への置き換え・追加の作業を禁ずる。\n\n" + directive)


def build_constraints_block(constraints: list[dict]) -> str:
    """worker プロンプト先頭に付ける拘束ブロック（短い定型・§8.10f 配布規則）。

    禁止型は deny 規則として機械側でも強制されるため、ここは worker への通知に留める。
    拘束が無ければ空文字列。
    """
    if not constraints:
        return ""
    lines = ["【拘束条件（厳守）】"]
    for c in constraints:
        prefix = "禁止: " if c.get("forbid") else ""
        lines.append(f"- {prefix}{c['text']}")
    return "\n".join(lines) + "\n\n"


def compare_directive(directive: str, executed: list[str]) -> tuple[bool, str]:
    """遵守照合（§8.10f）: 命令されたコマンド列 ⇄ 実行されたコマンド列の機械照合。

    命令の各非空行について、実行コマンドのいずれかに（空白正規化のうえ）含まれていれば一致。
    返り値は (全行一致か, 人へ併記する照合ブロック文字列)。判定は文字列照合のみで
    LLM を使わない。
    """
    normalized_exec = [_norm_ws(cmd) for cmd in executed]
    lines = ["【遵守照合】命令されたコマンド ⇄ 実行されたコマンド（機械照合）"]
    all_ok = True
    for line in (directive or "").splitlines():
        line = line.strip()
        if not line:
            continue
        norm = _norm_ws(line)
        hit = any(norm in cmd for cmd in normalized_exec)
        lines.append(f"{'✓' if hit else '✗'} {line}")
        if not hit:
            all_ok = False
    lines.append("判定: " + ("一致（全コマンド実行を確認）" if all_ok
                             else "不一致（未実行の命令コマンドあり）"))
    return all_ok, "\n".join(lines)


def required_input_for(cause: str) -> str:
    """失敗原因コード → 人へ求める入力（§8.10f 対応表。未知コードは汎用文言）。"""
    base = (cause or "").split(":")[0]
    if cause in _REQUIRED_INPUT:
        return _REQUIRED_INPUT[cause]
    if base == "acceptance_failed":
        return "完了条件が満たせていません。条件または依頼内容を見直してください"
    return _REQUIRED_INPUT.get(base, "失敗原因を確認のうえ、依頼を修正してください")


def is_repeated_cause(causes: list[str] | None) -> bool:
    """連続失敗が 2 回に達しているか（§8.10f 自動再計画の停止条件）。

    当初は「同一コード 2 回連続」だったが、E2E 実測（2026-08-24）で worker が勝手な回避
    （push 不能に対する git init）で環境を変えると原因コードがズレ、実質同じ袋小路の反復を
    すり抜けることを確認した。原因が何であれ連続 2 回失敗したら再計画をやめて人に返す
    （成功すれば列はリセットされる — record_task_outcome）。
    """
    if not causes or len(causes) < 2:
        return False
    return bool(causes[-1]) and bool(causes[-2])
