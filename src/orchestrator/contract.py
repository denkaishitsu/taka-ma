"""会話⇄実行の受け渡し契約（設計書 §8.10f）— 契約の検証・機械補助の純粋ロジック。

契約化パス（脳 LLM の第 2 呼び出し）の出力は「提案」であり、ここでの検証を通った値だけが
契約フィールドとして効力を持つ。LLM 呼び出し・ファイル I/O は置かない（conversation.py /
orchestrator が担う）。テストが実機・モデルに依存しないようにするための分離。
"""

import re

from orchestrator import runbook as runbook_rules

# 完了条件の検査カタログ（§8.10f）。LLM に自由な検査コマンドを書かせず、検査はこの
# kind とパラメータの組に限定する（probe と同じ「LLM 出力を任意コマンド実行に接続しない」規律）。
# コマンド組立は grounding.GroundingVerifier.verify_acceptance（コード側）が行う。
ACCEPTANCE_KINDS = {
    "pushed": {"required": set(), "optional": {"branch"}},
    "remote_file": {"required": {"branch", "path"}, "optional": set()},
    "file": {"required": {"path"}, "optional": set()},
    "head_touches": {"required": {"path"}, "optional": set()},
    "diff_limit": {"required": {"max_lines"}, "optional": {"path"}},
    # マージ完了の検査（§8.10g）。「source が target に取り込まれたか」を merge-base で
    # 実測する。2026-08-28 E2E 実測: この語彙が無く、マージを含む依頼の完了条件が
    # commit/push しか表せなかった（脳が操作名 merge_ff で代用しようとする誘因にもなった)
    "branch_merged": {"required": {"source", "target"}, "optional": set()},
}

# リポジトリ系の検査（§8.10f needs_repo の機械補助: これを含む契約は実リポジトリを要する）
REPO_KINDS = {"pushed", "remote_file", "head_touches", "diff_limit", "branch_merged"}

# 整数パラメータ（検査コマンド文字列には乗せず比較にのみ使う）。上限は暴走値の拒否
_INT_PARAMS = {"max_lines"}
_MAX_INT_PARAM = 100000

# 環境改変コマンドの既定 deny（§8.10f 事前予防）。worker の勝手な環境改変（push 不能への
# git init 等）は失敗原因コードをズラし反復停止の検出を撹乱するため、decide デーモンが
# 全タスクで既定 deny する。契約 directive（人が着手確認で承認した逐語命令）に逐語で
# 含まれる場合のみタスク別 allow（env_mutation_allows）が優先する。
# decide_daemon.py の _ENV_MUTATION_DENY と同一リストであること（grep で両端一致を確認する）
ENV_MUTATION_COMMANDS = ("git init", "git remote add", "git remote set-url",
                         "git remote remove", "git config")

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


def env_mutation_allows(directive: str | None) -> list[str]:
    """契約 directive に逐語で含まれる環境改変コマンドを返す（§8.10f 既定 deny の許可経路）。

    directive は着手確認で人が承認した逐語命令のみ（validate_contract が逐語性を保証済み）。
    ここに現れた環境改変コマンドは「人間が明示して承認した」ものなので、decide デーモンの
    既定 deny より優先するタスク別 allow として task-deny 規則へ刻む。照合は大文字小文字を
    無視した空白正規化ずみ部分一致（decide 側の deny 照合と同じ決定的判定・LLM 不使用）。
    """
    if not directive:
        return []
    normalized = _norm_ws(directive).lower()
    return [cmd for cmd in ENV_MUTATION_COMMANDS if cmd in normalized]


def validate_contract(raw, source_text: str) -> tuple[dict | None, list[str]]:
    """契約化パスの LLM 出力を検証・正規化する。(契約 dict, 逸脱理由リスト) を返す。

    逸脱が 1 つでもあれば契約は None（fail-closed。呼び出し側がリトライ・昇格 /
    人へ差し戻す）。正規化済み契約は {directive, constraints, acceptance, runbook,
    workspace, needs_repo(, rest_summary)}。rest_summary キーは入力に在って型が
    正しいときのみ載る（欠落は「従来どおり確定要約を分解」の縮退印・§8.10g）。
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

    # 脳が runbook の kind（merge_ff 等）を完了条件欄に置く誤りを機械補正する（§8.10g。
    # カタログ名の完全一致のみで判定＝決定的・LLM 不使用。2026-08-28 E2E 実測: qwen が
    # この取り違えを高頻度で再発し、fail-closed が契約ごと弾いて依頼が進まなくなる。
    # 移送先の runbook 検証は通常どおり掛かる — params 不正なら従来どおり契約不成立）
    raw_acceptance = [a for a in (raw.get("acceptance") or [])]
    misplaced_runbook = [a for a in raw_acceptance
                         if isinstance(a, dict)
                         and a.get("kind") in runbook_rules.RUNBOOK_KINDS]
    raw_acceptance = [a for a in raw_acceptance if a not in misplaced_runbook]

    # acceptance: カタログ検証（未知 kind・パラメータ不正は逸脱）
    acceptance: list[dict] = []
    for a in raw_acceptance:
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
        # 整数パラメータ（max_lines）は正の bool でない int のみ受理（数字文字列は int へ
        # 正規化）。文字列パラメータは従来どおり安全文字のみ（コマンド文字列に乗るため）
        bad = []
        for k, v in params.items():
            if k in _INT_PARAMS:
                # isascii を併課する: 上付き数字「²」等は isdigit=True だが int() が
                # ValueError になる（未捕捉例外で契約化パスを落とさない・fail-closed）
                if isinstance(v, str) and v.isascii() and v.isdigit():
                    v = params[k] = int(v)
                if (not isinstance(v, int) or isinstance(v, bool)
                        or not 0 < v <= _MAX_INT_PARAM):
                    bad.append(k)
            elif (not isinstance(v, str) or not _SAFE_PARAM_RE.match(v)
                    or ".." in v.split("/")):
                bad.append(k)
        if missing or unknown or bad:
            problems.append(
                f"検査 {kind} のパラメータ不正"
                f"（不足={sorted(missing)} 未知={sorted(unknown)} 不正値={sorted(bad)}）")
            continue
        acceptance.append({"kind": kind, "params": params})

    # runbook: 定型 git 操作の決定的実行列（§8.10g）。カタログ・パラメータ検証は
    # runbook 側（コマンド組立と同居する SSOT）に委ねる。完了条件欄から移送された
    # 誤配置分（上記）は既存の runbook 列の後ろへ足す。非配列の runbook は
    # 従来どおり逸脱として弾かせる（移送で型エラーを覆い隠さない）
    base_runbook = raw.get("runbook")
    if base_runbook is None:
        base_runbook = []
    raw_runbook = (base_runbook + misplaced_runbook
                   if isinstance(base_runbook, list) else base_runbook)
    runbook, rb_problems = runbook_rules.validate_runbook(raw_runbook)
    if rb_problems:
        problems.extend(rb_problems)

    if problems:
        return None, problems

    # needs_repo: 脳の判定に機械補助を重ねる（§8.10f。directive の git コマンド・
    # リポジトリ系検査・runbook 列があれば強制 true。脳が false と言っても機械判定が勝つ）
    needs_repo = bool(raw.get("needs_repo"))
    if directive and _GIT_CMD_RE.search(directive):
        needs_repo = True
    if any(a["kind"] in REPO_KINDS for a in acceptance):
        needs_repo = True
    if runbook:
        needs_repo = True

    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        workspace = None

    result = {
        "directive": directive,
        "constraints": constraints,
        "acceptance": acceptance,
        "runbook": runbook,
        "workspace": workspace,
        "needs_repo": needs_repo,
    }
    # rest_summary: runbook 外の残り作業の要約（§8.10f。計画組立の分解入力専用・
    # worker / 完了検査へは渡さない）。null は「残り作業なし＝agent 分解を省略」の
    # 明示。キー欠落・型不正は契約不成立にせずキーを載せない（縮退＝従来どおり
    # 確定要約を分解する。§8.10g 分解の二重実行抑止）
    if "rest_summary" in raw:
        rest = raw["rest_summary"]
        if rest is None:
            result["rest_summary"] = None
        elif isinstance(rest, str) and rest.strip():
            result["rest_summary"] = rest.strip()
    return result, []


# 依頼が push を含むかの検出（既定検査の自動付与用）。語彙は grounding.py の主張検出
# _PUSH_CLAIM_RE と同一に保つ（`grep -n "プッシュ" src/orchestrator/*.py` で一致を確認する）
_PUSH_WORD_RE = re.compile(r"push(?:ed)?|プッシュ", re.IGNORECASE)

# 編集系の語の検出（成果物 file 既定・§8.10f）。削除系（削除 / delete / remove）は
# 含めない — 削除依頼へ file 検査を課すと成功したほど誤未達になる
_EDIT_WORD_RE = re.compile(
    r"作成|作って|生成|追記|追加|編集|修正|更新|書い|書き|保存|反映|直し|直す|"
    r"create|write|edit|update|append", re.IGNORECASE)

# 成果物パスのトークン検出（成果物 file 既定・§8.10f）。英字始まりの拡張子を持つ
# 相対パスのみを拾う。前後の ASCII 語文字・`/` を境界で拒否することで、絶対パス
# （`/opt/...` の内側）や URL 埋め込み（`https://.../a.md`）を対象外にする。拡張子の
# 先頭を英字に限ることでバージョン番号（0.6.0 等）を対象外にする。文字集合は
# _SAFE_PARAM_RE の受理形式に閉じる（検出即 file 検査のパラメータになるため）
_DELIVERABLE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9._/\-])"
    r"((?:[A-Za-z0-9._\-]+/)*[A-Za-z0-9._\-]+\.[A-Za-z][A-Za-z0-9]{0,7})"
    r"(?![A-Za-z0-9_/])")

# 成果物 file 既定の付与上限（列挙の暴走で契約を肥大させない）
_MAX_DEFAULT_FILE_CHECKS = 5

# 量指定の検出（diff_limit 既定・§8.10f）。「一行だけ追記しろ」等の明示の行数指定を拾う。
# 「行目」（位置参照）・「改行」（文字の話）は量指定でないため除外する
_LINE_LIMIT_RE = re.compile(r"(?<!改)([0-9０-９]+|[一二三四五六七八九十])\s*行(?!目)")
_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")
# これを超える行数指定は拘束の意図が薄い（規模の説明）ため既定付与しない
_MAX_DEFAULT_LINE_LIMIT = 20
# 指示 N 行への余白（末尾改行・体裁差を誤未達にしない。contract.md の N+2 と同値）
_LINE_LIMIT_MARGIN = 2


def apply_default_acceptance(contract: dict, summary: str) -> dict:
    """脳が完了条件を立て損ねた契約へ既定検査を付与する（§8.10f 既定完了検査の自動付与）。

    「実測検査なしに完了と言える」契約を成立させないための機械補助。判定は決定的
    （語・パスの検出のみ・LLM 不要）で、脳が立てた検査は上書きしない:

    - push 既定: push 語を含み acceptance が空なら pushed 検査を付与（push を含まない
      依頼には付与しない — 編集のみのタスクに pushed を課すと誤未達になる）
    - 成果物 file 既定: 編集系の語と成果物パス（相対・拡張子付き）を含む依頼には、
      当該パスの file 検査を必ず載せる（既存 acceptance に同一パスの検査があれば重複
      させない。#153 — 2026-08-25 実障害: 編集タスクが完了検査なしで「完了」と報告）
    """
    text = f"{summary}\n{contract.get('directive') or ''}"
    acceptance = contract.get("acceptance") or []
    if not acceptance and _PUSH_WORD_RE.search(text):
        acceptance = [{"kind": "pushed", "params": {}}]
    if _EDIT_WORD_RE.search(text):
        covered = {(a.get("params") or {}).get("path") for a in acceptance}
        added = 0
        for m in _DELIVERABLE_PATH_RE.finditer(text):
            path = m.group(1)
            if path in covered:
                continue
            # 防御的再検証（検出文字集合は受理形式に閉じているが、規則の独立変更に耐える）
            if not _SAFE_PARAM_RE.match(path) or ".." in path.split("/"):
                continue
            acceptance.append({"kind": "file", "params": {"path": path}})
            covered.add(path)
            added += 1
            if added >= _MAX_DEFAULT_FILE_CHECKS:
                break
    # 量指定既定（diff_limit・§8.10f）: 明示の行数指定がある編集依頼には変更量の上限を
    # 必ず載せる。脳（contract.md）の抽出は分離実測 3/5 と不安定（2026-08-26）なため、
    # 決定的な検出で立て損ねを補う。脳が立てた diff_limit は上書きしない
    if (_EDIT_WORD_RE.search(text)
            and not any(a.get("kind") == "diff_limit" for a in acceptance)):
        m = _LINE_LIMIT_RE.search(text)
        if m:
            raw = m.group(1)
            n = _KANJI_DIGITS.get(raw)
            if n is None:
                try:
                    n = int(raw.translate(_ZEN2HAN))
                except ValueError:
                    n = None
            if n and 0 < n <= _MAX_DEFAULT_LINE_LIMIT:
                acceptance.append({"kind": "diff_limit",
                                   "params": {"max_lines": n + _LINE_LIMIT_MARGIN}})
    contract["acceptance"] = acceptance
    return contract


# git 操作語の検出（§8.10g 警告補助）。push は _PUSH_WORD_RE、マージ/merge・コミット/commit
# を加える。用途は**着手確認への注意行の付与のみ**（自動変換には使わない — 語列挙による
# 言語理解は §8.3 probe 規律導入時に廃した方式。人が見る警告に限って許容する）
_GIT_OP_WORD_RE = re.compile(
    r"push(?:ed)?|プッシュ|merge|マージ|commit(?:ted)?|コミット", re.IGNORECASE)


def runbook_warning(contract: dict, summary: str) -> str | None:
    """git 操作を含みそうな依頼が runbook なしで agent 実行になるときの注意文（§8.10g）。

    判定は決定的（語検出のみ）で、返すのは人向けの警告 1 行。変換・ブロックはしない
    （判断は着手確認を見る人が行う）。runbook がある・git 操作語が無いなら None。
    """
    if contract.get("runbook"):
        return None
    text = f"{summary}\n{contract.get('directive') or ''}"
    if _GIT_OP_WORD_RE.search(text):
        return ("注意: git 操作を含む依頼ですが runbook（決定的実行）が未使用です。"
                "git 操作は worker LLM の裁量実行になります")
    return None


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
    if base == "runbook_precondition":
        return ("git 操作の前提が満たされていません（実測は失敗報告に併記）。"
                "リポジトリ状態を確認し、対処を指示してください")
    if base == "runbook_failed":
        return ("git 操作が失敗しました（実測は失敗報告に併記）。"
                "リポジトリ状態を確認し、対処を指示してください")
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
