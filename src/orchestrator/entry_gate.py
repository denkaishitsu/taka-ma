"""入口ゲート — 依頼⇄契約の独立突合（設計書 §8.10f「入口ゲート」）。

契約化（Contractor）が成立させた契約は、着手確認を出す前に、契約化と別系統の
突合エージェント（書き込み不能の LLM 単発呼び出し）が「依頼の各要求 ⇄ 契約フィールド」の
対応表を作る。スキーマ閉包（unmapped）は契約化脳の自己申告であり、脳が指定を黙って
落とすと unmapped も空になる — 落とし物の検出の正は本段の対応表（2026-09-14 実測:
依頼の成果物 2 件が契約のどこにも載らず成立し、出口で板挟み未達になった）。

合否（各行の covered / uncovered）はコード側が「参照が契約の実フィールドへ解決できるか」
から機械導出し、LLM 自身の対応宣言は採らない（参照先が実在しなければ捏造対応は通らない）。
検証・レビュー系の要求は is_verification_text（§10.2 の SSOT 語彙）で決定的に
「出口ゲートが担当」へ対応づける（契約に載せる物ではない — #169 の一本化）。

未対応が残っても着手確認は止めない — 対応表を着手確認に載せ、人が裁く（P5: 見えない
契約は承認されない）。突合を実行できなかったときは「未検査」の注記を機械付与し、
未検査を無印（突合済みの顔）で通さない。

突合 LLM の実行手段は呼び出し側から注入する
（run_llm: ConversationManager._run_entry_gate_llm。単体テストでは偽物に差し替える）。
"""

import json
import logging
import re

from ai_gateway.llm import extract_json, repair_json_escapes
from orchestrator.plan import is_verification_text

logger = logging.getLogger("sa-ru.entry_gate")

# 依頼原文・契約の LLM 向けブロック上限（SSH stdin と LLM コンテキストを守る。
# exit_gate の証跡上限と同趣旨）
_REQUEST_MAX_CHARS = 30000
_CONTRACT_MAX_CHARS = 12000
# 対応表の逐語引用 1 件の表示上限（着手確認の可読性）
_REQ_DISPLAY_MAX_CHARS = 120
# 受注不備注記に列挙する要求・パスの上限（Slack ヘッダの肥大防止。超過分は件数で示す）
_NOTICE_MAX_ROWS = 10


def _display(req: str, cap: int = _REQ_DISPLAY_MAX_CHARS) -> str:
    """表示行用に逐語引用を 1 行へ潰す（改行・連続空白は表組みを崩すため空白 1 つへ）。"""
    text = " ".join((req or "").split())
    return text[:cap] + "…" if len(text) > cap else text

# 契約フィールド参照の受理形式（コード側の解決対象。これ以外は未解決＝対応なし扱い）
_FIELD_REF_RE = re.compile(
    r"\A(directive|constraints|acceptance|runbook|workspace|branch"
    r"|target_paths|rest_summary)(?:\[(\d+)\])?\Z")

# 列フィールド（インデックス参照可）。スカラーフィールドへの [i] は不正参照
_LIST_FIELDS = {"constraints", "acceptance", "runbook", "target_paths"}

# 着手確認の表示に使うフィールドの日本語ラベル（提示文のコード組立・§8.10f）
_FIELD_LABELS = {
    "directive": "命令（逐語実行）",
    "constraints": "拘束条件",
    "acceptance": "完了条件",
    "runbook": "runbook",
    "workspace": "作業場所",
    "branch": "ブランチ",
    "target_paths": "対象文書",
    "rest_summary": "残り作業",
}

# 対応表の各行の status（コード導出の 3 値・§8.10f 着手確認での提示）
STATUS_FIELD = "field"          # ✅ 契約フィールドに載っている（参照が解決できた）
STATUS_EXIT_GATE = "exit_gate"  # ✅ 出口ゲートが担当（検証系の要求・決定的振り分け）
STATUS_UNCOVERED = "uncovered"  # ❌ 契約に無い


class EntryGateReport:
    """突合 1 回分の結果。

    items:     [{"req", "src", "refs", "status"}] — status はコード導出の 3 値。
               refs は解決できた参照のみ（uncovered は空）。
    unchecked: 突合を実行できなかった（LLM 応答を対応表として解釈できない・実行不能）。
               着手確認へ「未検査」の注記を機械付与する（失敗報告の帰属区別・§8.10f）。
    note:      unchecked のときの短い理由。
    """

    def __init__(self, items: list | None = None, unchecked: bool = False,
                 note: str = ""):
        self.items = items or []
        self.unchecked = unchecked
        self.note = note

    def uncovered(self) -> list:
        return [it for it in self.items if it.get("status") == STATUS_UNCOVERED]

    def to_record(self) -> dict:
        """着手確認レコード・確定タスクへ運ぶ形（§8.10f 着手時判断の記録）。"""
        return {"items": self.items, "unchecked": self.unchecked, "note": self.note}


def resolve_reference(contract: dict, ref) -> bool:
    """契約フィールド参照が契約の実体へ解決できるか（決定的・LLM 不関与）。

    covered と認めるのはここを通った参照だけ — LLM が対応を捏造しても、参照先が
    実在しなければ covered にならない（§8.10f 合否のコード導出）。
    """
    if not isinstance(ref, str):
        return False
    m = _FIELD_REF_RE.match(ref.strip())
    if not m:
        return False
    field, idx = m.group(1), m.group(2)
    value = contract.get(field)
    if field in _LIST_FIELDS:
        items = value or []
        if idx is None:
            return bool(items)
        return int(idx) < len(items)
    if idx is not None:
        return False        # スカラーフィールドへのインデックス参照は不正
    return bool(value)


def _ref_label(contract: dict, ref: str) -> str:
    """解決済み参照の表示ラベル（着手確認の対応表に実体を併記する）。"""
    m = _FIELD_REF_RE.match(ref.strip())
    field, idx = m.group(1), m.group(2)
    label = _FIELD_LABELS[field]
    if field == "acceptance" and idx is not None:
        kind = ((contract.get("acceptance") or [])[int(idx)] or {}).get("kind")
        return f"{label}{int(idx) + 1}（{kind}）" if kind else f"{label}{int(idx) + 1}"
    if field == "runbook" and idx is not None:
        kind = ((contract.get("runbook") or [])[int(idx)] or {}).get("kind")
        return f"{label}{int(idx) + 1}（{kind}）" if kind else f"{label}{int(idx) + 1}"
    if idx is not None:
        return f"{label}{int(idx) + 1}"
    return label


def _contract_doc(contract: dict) -> str:
    """成立した契約の LLM 向けブロック。運搬フィールドのみを添字つきで列挙する。

    契約化脳の思考過程・unmapped は含めない（作った側の説明で突合側を汚染しない —
    出口ゲートの遮断と同じ規律・§8.10f）。
    """
    fields = {k: contract.get(k) for k in _FIELD_LABELS}
    doc = json.dumps(fields, ensure_ascii=False, indent=1)
    if len(doc) > _CONTRACT_MAX_CHARS:
        doc = doc[:_CONTRACT_MAX_CHARS] + "\n…（以降略）"
    return doc


class EntryGateChecker:
    """依頼原文と成立契約から対応表を得て、各行の covered / uncovered をコード導出する。

    run_llm: callable(prompt: str) -> str
        書き込み不能の突合 LLM 単発呼び出し（ConversationManager._run_entry_gate_llm）。
        実行不能は例外で返す。
    prompt_template: prompts/entry_gate.md の内容（正本は md ファイル）。
    """

    def __init__(self, run_llm, prompt_template: str):
        self._run_llm = run_llm
        self._template = prompt_template

    def _parse(self, stdout: str) -> list | None:
        """突合 LLM の出力から対応表を取り出す。解釈できなければ None（リトライ）。"""
        try:
            try:
                parsed = json.loads(extract_json(stdout))
            except (json.JSONDecodeError, ValueError):
                parsed = json.loads(extract_json(repair_json_escapes(stdout)))
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        items = parsed.get("items")
        if not isinstance(items, list):
            return None
        # 空の対応表は「全件列挙が行われていない」＝解釈不能と同じ扱い（リトライ→未検査）。
        # 依頼には常に確定要約が含まれ、要求が 0 件になることはない（exit_gate の
        # 空判定表拒否と同じ規律）
        if not items:
            return None
        for it in items:
            if not isinstance(it, dict):
                return None
            if not isinstance(it.get("req"), str) or not it["req"].strip():
                return None
            if not isinstance(it.get("mapped_to"), list):
                return None
        return items

    def _derive(self, contract: dict, raw_items: list) -> list:
        """各行の status をコード導出する（LLM の対応宣言は採らない・§8.10f）。"""
        rows = []
        for it in raw_items:
            req = it["req"].strip()
            # 検証・レビュー系の要求は決定的に出口ゲートへ振り分ける（語彙 SSOT・#169）。
            # LLM の mapped_to に依らない — 依頼の書き方で経路が変わることを防ぐ
            if is_verification_text(req):
                rows.append({"req": req, "src": it.get("src") or "",
                             "refs": [], "status": STATUS_EXIT_GATE})
                continue
            refs = [r.strip() for r in it["mapped_to"]
                    if resolve_reference(contract, r)]
            rows.append({"req": req, "src": it.get("src") or "",
                         "refs": refs,
                         "status": STATUS_FIELD if refs else STATUS_UNCOVERED})
        return rows

    def check(self, request_view: str, summary: str,
              contract: dict) -> EntryGateReport:
        """突合を 1 回行う。JSON 逸脱・空の対応表は同一バックエンドで 1 回リトライし、
        なお解釈できなければ unchecked（未検査）で返す — 着手確認は止めない（§8.10f）。
        """
        request_doc = request_view or ""
        if len(request_doc) > _REQUEST_MAX_CHARS:
            request_doc = request_doc[:_REQUEST_MAX_CHARS] + "\n…（以降略）"
        prompt = (self._template
                  .replace("{request_doc}", request_doc)
                  .replace("{summary}", summary or "")
                  .replace("{contract_doc}", _contract_doc(contract)))
        raw = None
        for attempt in (1, 2):
            try:
                stdout = self._run_llm(prompt)
            except Exception as e:
                logger.warning("入口ゲート LLM の実行に失敗（%d 回目）: %s", attempt, e)
                continue
            raw = self._parse(stdout)
            if raw is not None:
                break
            logger.warning("入口ゲート LLM の出力を対応表として解釈できない（%d 回目）",
                           attempt)
        if raw is None:
            return EntryGateReport(
                unchecked=True,
                note="突合エージェントの応答を対応表として解釈できない")
        return EntryGateReport(items=self._derive(contract, raw))


def render_table(record: dict, contract: dict) -> str:
    """着手確認へ載せる対応表ブロック（§8.10f 着手確認での提示・コード組立のみ）。

    突合エージェントの散文は載せない — 対応表 JSON とコード導出の合否だけから組む。
    """
    lines = ["依頼⇄契約の対応表（契約化と別系統の突合・合否はコード導出）:"]
    if record.get("unchecked"):
        # 未検査を無印で通さない（失敗報告の帰属区別・§8.10f）
        lines.append("⚠ 依頼⇄契約の突合を実行できませんでした（未検査）: "
                     + (record.get("note") or ""))
        return "\n".join(lines)
    items = record.get("items") or []
    uncovered = [it for it in items if it.get("status") == STATUS_UNCOVERED]
    if uncovered:
        lines.insert(0, f"⚠ 契約に載っていない要求が {len(uncovered)} 件あります"
                        "（このまま着手すると契約外＝機械検査されません。"
                        "直す場合はスレッドで訂正してください）")
    for it in items:
        req = _display(it.get("req") or "")
        status = it.get("status")
        if status == STATUS_FIELD:
            dest = "、".join(_ref_label(contract, r) for r in (it.get("refs") or []))
            lines.append(f"- ✅ {req} → {dest}")
        elif status == STATUS_EXIT_GATE:
            lines.append(f"- ✅ {req} → 出口ゲートが担当（完了報告前の独立検証）")
        else:
            lines.append(f"- ❌ {req} → 契約に無い")
    # 部分列挙への保険（突合エージェントも LLM であり全件列挙は保証できない）:
    # 表の完全性を人に信じさせない — 表に無い要求の扱いを「契約に無い」側へ倒す
    # 読み方を固定注記で示し、最終裁定（P5）の判断材料を欠落側にも効かせる
    lines.append("（この表は突合エージェントの列挙です。あなたの依頼にあって"
                 "この表に無い要求は「❌ 契約に無い」とみなして判断してください）")
    return "\n".join(lines)


def misbooking_notice(entry_record: dict, outside_paths: list) -> str | None:
    """受注不備の可能性の冒頭注記（失敗報告の帰属区別・§8.10f 出口側）。

    条件は決定的 — answered ツリー閉包 FAIL（outside_paths 非空）かつ、着手時の
    対応表に人が「このまま着手」で通した ❌ 行が存在するときだけ返す。❌ 行の要求と
    約束の外のパス列は**並記**にとどめる（散文⇄パスの対応づけは推測になるため行わない —
    並べて人が見る）。該当しなければ None。
    """
    uncovered = [it for it in (entry_record.get("items") or [])
                 if it.get("status") == STATUS_UNCOVERED]
    if not uncovered or not outside_paths:
        return None
    lines = [f"⚠ 受注処理に不備の可能性: この依頼には契約に載らなかった要求が "
             f"{len(uncovered)} 件ありました（着手時に提示済み）。",
             "約束の外の変化はその作業の産物である可能性があります"
             " — worker の逸脱と断定しません。",
             "契約に載らなかった要求（未検査 — 検査されていません。未達ではありません）:"]
    for it in uncovered[:_NOTICE_MAX_ROWS]:
        lines.append(f"- {_display(it.get('req') or '')}")
    if len(uncovered) > _NOTICE_MAX_ROWS:
        lines.append(f"- …他 {len(uncovered) - _NOTICE_MAX_ROWS} 件")
    lines.append("約束の外の変化:")
    for p in outside_paths[:_NOTICE_MAX_ROWS]:
        lines.append(f"- {p}")
    if len(outside_paths) > _NOTICE_MAX_ROWS:
        lines.append(f"- …他 {len(outside_paths) - _NOTICE_MAX_ROWS} 件")
    return "\n".join(lines)
