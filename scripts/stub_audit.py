#!/usr/bin/env python3
"""機械的スタブ監査 — AST ベース（解釈なし・import なし）。

実装の「在るけど何もしない／未定義」を機械的に列挙する。設計書とコードの目視突き合わせ
では取りこぼす空実装・足場プレースホルダ・未定義メソッド呼出を、AST を権威として検出する
（人間／LLM の判断に依存しない）。対象を import せず構文解析のみ行うため 3rd-party 依存は不要。

検出カテゴリ:
  A   : 本体が空（docstring / pass / ... のみ）の関数
  A1b : 本体のどこかに文レベル ``...``（Ellipsis）を含む関数（足場＋未実装）
  A2  : ``raise NotImplementedError`` を含む関数
  B   : ``self.<attr>.<method>`` で attr が ``self.attr = ClassName(...)`` により repo 内クラスへ
        束縛され、その method が当該クラスに未定義（呼べば AttributeError。例: run_model_subprocess）
  C   : 本体が logging 呼び出しのみ・return 無しのプレースホルダ（ヒューリスティック、要目視）
  D   : ``self.<method>()`` を呼ぶが自クラス（＋ repo 内基底）に未定義（例: _call_sentinel）。
        外部基底クラスを継承する class は継承メソッドを検証できないため対象外。

限界（このツールでは検出できない）:
  - 実装済みだがロジックが誤り／不完全な関数
  - どこからも呼ばれずコードごと存在しない機能
  - repo クラス以外（dict / フレームワーク物）へのメソッド呼出
  - SSH／プロセス間／実機統合の正しさ
  → これらは end-to-end スモークテストで確認すること。

使い方:  python3 scripts/stub_audit.py [src_dir]   # 既定 src
終了コード: 検出 0 件なら 0、1 件以上なら 1（CI ガードに利用可能）。
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

# logging メソッド名（logger.info(...) 等）。カテゴリ C の判定に使う。
LOG_METHODS = frozenset({"info", "debug", "warning", "error", "exception", "critical"})

# 出力順とラベル。カテゴリ追加時はここに 1 行足す。
CATEGORIES: list[tuple[str, str]] = [
    ("A", "空スタブ（docstring / pass / ... のみ）"),
    ("A1b", "本体に文レベル `...` を含む（足場＋未実装）※A と重複あり"),
    ("A2", "raise NotImplementedError"),
    ("B", "self.<attr>.<method> が束縛クラスに未定義（呼べば／参照で AttributeError）"),
    ("C", "ログ出力のみ・return 無しのプレースホルダ（ヒューリスティック、要目視）"),
    ("D", "self.<method>() が自クラス（＋repo 内基底）に未定義"),
]

FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Finding:
    """検出 1 件。location は表示用の場所、detail は補足（任意）。"""

    category: str
    location: str
    detail: str = ""


# ──────────────────────────────────────────────────────────────────────────
# ソース読み込み・索引
# ──────────────────────────────────────────────────────────────────────────

def parse_sources(root: Path) -> tuple[dict[Path, ast.Module], list[str]]:
    """root 配下の .py を AST へパースする。(tree マップ, 構文エラー一覧) を返す。

    import は行わず構文解析のみ。構文エラーのファイルは解析対象から除外して報告する。
    """
    trees: dict[Path, ast.Module] = {}
    errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            trees[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: {exc}")
    return trees, errors


def index_class_methods(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """クラス名 → 定義メソッド名集合（全ファイル横断）。B / D の「定義済みか」判定に使う。"""
    index: dict[str, set[str]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = {n.name for n in node.body if isinstance(n, FuncDef)}
                index.setdefault(node.name, set()).update(methods)
    return index


def iter_functions(trees: dict[Path, ast.Module]):
    """全ファイルの全関数（同期・非同期）を (path, 関数ノード) で列挙する。"""
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, FuncDef):
                yield path, node


def iter_classes(trees: dict[Path, ast.Module]):
    """全ファイルの全クラス定義を (path, ClassDef) で列挙する。"""
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield path, node


# ──────────────────────────────────────────────────────────────────────────
# 小さな AST 述語
# ──────────────────────────────────────────────────────────────────────────

def _body_without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """先頭が docstring（文字列リテラル式）ならそれを除いた本体を返す。"""
    head = body[0] if body else None
    if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)):
        return body[1:]
    return body


def _is_ellipsis(stmt: ast.stmt) -> bool:
    """文 stmt が文レベルの `...`（Ellipsis 式文）か。"""
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis


def _self_attr_name(node: ast.AST) -> str | None:
    """node が `self.<name>` なら <name>、違えば None。"""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr
    return None


# ──────────────────────────────────────────────────────────────────────────
# 関数単位の検出（A / A1b / A2 / C）
# ──────────────────────────────────────────────────────────────────────────

def is_empty_stub(fn: FuncDef) -> bool:
    """カテゴリ A: docstring を除いた本体が 無し／全文 pass／全文 `...`。"""
    body = _body_without_docstring(fn.body)
    return not body or all(isinstance(s, ast.Pass) for s in body) or all(_is_ellipsis(s) for s in body)


def has_inline_ellipsis(fn: FuncDef) -> bool:
    """カテゴリ A1b: 本体のどこかに文レベル `...` を含む（足場＋未実装）。"""
    return any(_is_ellipsis(s) for s in ast.walk(fn))


def raises_not_implemented(fn: FuncDef) -> bool:
    """カテゴリ A2: `raise NotImplementedError`（呼出形・名前形の両方）を含む。"""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Raise):
            continue
        exc = node.exc
        name = exc.func if isinstance(exc, ast.Call) else exc
        if isinstance(name, ast.Name) and name.id == "NotImplementedError":
            return True
    return False


def is_log_only(fn: FuncDef) -> bool:
    """カテゴリ C: docstring を除く本体が logging 呼び出しのみ（return も他処理も無い）。"""
    body = _body_without_docstring(fn.body)
    if not body:
        return False
    return all(
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
        and isinstance(s.value.func, ast.Attribute) and s.value.func.attr in LOG_METHODS
        for s in body
    )


# ──────────────────────────────────────────────────────────────────────────
# クラス単位の検出（B / D）
# ──────────────────────────────────────────────────────────────────────────

def _resolve_self_attr_types(cls: ast.ClassDef, class_methods: dict[str, set[str]]) -> dict[str, str]:
    """`self.attr = ClassName(...)` を解析し attr → repo 内クラス名 を返す（B の前処理）。

    右辺が repo 内で定義されたクラスの生成（class_methods に在る）に限る。
    """
    types: dict[str, str] = {}
    for node in ast.walk(cls):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        attr = _self_attr_name(node.targets[0])
        value = node.value
        if attr and isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in class_methods:
            types[attr] = value.func.id
    return types


def find_undefined_attr_calls(cls: ast.ClassDef, class_methods: dict[str, set[str]]) -> list[tuple[str, str, str]]:
    """カテゴリ B: `self.<attr>.<method>` で method が束縛クラスに未定義のもの。

    Returns: (attr, クラス名, method) のリスト（呼出・参照の重複は畳む）。
    """
    attr_types = _resolve_self_attr_types(cls, class_methods)
    found: dict[tuple[str, str], str] = {}
    for node in ast.walk(cls):
        if not isinstance(node, ast.Attribute):
            continue
        attr = _self_attr_name(node.value)            # node.value が `self.attr`、node.attr が method
        if attr is None or attr not in attr_types:
            continue
        klass = attr_types[attr]
        if node.attr not in class_methods.get(klass, set()):
            found[(attr, node.attr)] = klass
    return [(attr, klass, method) for (attr, method), klass in found.items()]


def _allowed_self_names(cls: ast.ClassDef, class_methods: dict[str, set[str]]) -> set[str] | None:
    """`self.<name>()` で呼んでよい名前集合（D の前処理）。

    自クラス＋ repo 内基底のメソッド名、および self に代入された属性名を許容する。
    外部基底（FileSystemEventHandler 等）を継承する class は継承メソッドを検証できないため
    None を返す（＝検証対象外）。
    """
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    if any(b not in class_methods for b in bases):
        return None
    allowed = set(class_methods.get(cls.name, set()))
    for base in bases:
        allowed |= class_methods.get(base, set())
    for node in ast.walk(cls):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            name = _self_attr_name(target)
            if name:
                allowed.add(name)
    return allowed


def find_undefined_self_calls(cls: ast.ClassDef, class_methods: dict[str, set[str]]) -> list[str]:
    """カテゴリ D: `self.<method>()` で method が自クラス（＋repo 内基底）に未定義のもの。"""
    allowed = _allowed_self_names(cls, class_methods)
    if allowed is None:
        return []
    found: set[str] = set()
    for node in ast.walk(cls):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name = _self_attr_name(node.func)             # `self.method(...)` の method 名
        if name and name not in allowed:
            found.add(name)
    return sorted(found)


# ──────────────────────────────────────────────────────────────────────────
# 集計・出力
# ──────────────────────────────────────────────────────────────────────────

def collect_findings(trees: dict[Path, ast.Module], class_methods: dict[str, set[str]]) -> list[Finding]:
    """全カテゴリの検出を 1 つの Finding リストに集約する。"""
    findings: list[Finding] = []

    for path, fn in iter_functions(trees):
        location = f"{path} :: {fn.name}()"
        if is_empty_stub(fn):
            findings.append(Finding("A", location))
        if has_inline_ellipsis(fn):
            findings.append(Finding("A1b", location))
        if raises_not_implemented(fn):
            findings.append(Finding("A2", location))
        if is_log_only(fn):
            findings.append(Finding("C", location))

    for path, cls in iter_classes(trees):
        for attr, klass, method in find_undefined_attr_calls(cls, class_methods):
            findings.append(Finding(
                "B", f"{path} :: {cls.name}.{attr}({klass}).{method}", f"{klass} に {method} 未定義"))
        for method in find_undefined_self_calls(cls, class_methods):
            findings.append(Finding(
                "D", f"{path} :: {cls.name}.{method}()", f"{cls.name} に {method} 未定義"))

    return findings


def print_report(findings: list[Finding]) -> None:
    """カテゴリごとに区切って検出結果を出力する。"""
    by_category: dict[str, list[Finding]] = {key: [] for key, _ in CATEGORIES}
    for finding in findings:
        by_category[finding.category].append(finding)

    for key, title in CATEGORIES:
        print(f"\n{'=' * 70}\n{key}) {title}\n{'=' * 70}")
        items = by_category[key]
        if not items:
            print("  (なし)")
        for item in items:
            line = f"  {item.location}"
            if item.detail:
                line += f"  ← {item.detail}"
            print(line)

    summary = " / ".join(f"{key} {len(by_category[key])}" for key, _ in CATEGORIES)
    print(f"\n[要約] {summary}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="機械的スタブ監査（AST ベース）")
    parser.add_argument("src", nargs="?", default="src", help="監査対象ディレクトリ（既定: src）")
    args = parser.parse_args(argv)

    trees, errors = parse_sources(Path(args.src))
    for err in errors:
        print(f"[PARSE-ERROR] {err}")

    class_methods = index_class_methods(trees)
    findings = collect_findings(trees, class_methods)
    print_report(findings)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
