"""出口ゲート — 完了報告前の独立検証段（設計書 §8.10f「出口ゲート」）。

機械検査（完了条件の検査・遵守照合）が全 PASS のタスクについて、実装担当と別系統の
検証エージェント（書き込み不能の LLM 単発呼び出し）が「依頼された指摘・要件の全件突合」と
「新規持ち込み不整合の敵対的検査」を行う。判定根拠は (a) 依頼の根拠文書（契約フィールド）、
(b) 成果物本体（sa-ru が固定コマンドで採取した実測証跡）、(c) 機械検査の証跡のみで、
実装担当 worker の作業記録・自己申告（results のテキスト）は**構造的に渡さない**
（grounding の「worker テキストを判定根拠に使わない」規律の拡張。例外は回答型依頼の
回答本文 — 回答が成果物そのものであるため成果物として渡す）。

総合 PASS/FAIL はコード側が「全件 resolved かつ新規不整合ゼロ」から機械導出し、
LLM 自身の合格宣言は採らない。本段の LLM は完了を**与える**方向には働けない
（「完了」の必要条件は従来どおり機械検査の全 PASS。§8.10g との整合）。

証跡採取コマンドの実行手段（SSH）と検証 LLM の実行手段は呼び出し側から関数として注入する
（run_probe: RemoteProcessManager.run_ssh_probe / run_llm: Orchestrator._run_exit_gate_llm。
単体テストでは偽物に差し替える）。
"""

import json
import logging
import re
import shlex

from ai_gateway.llm import extract_json, repair_json_escapes

logger = logging.getLogger("sa-ru.exit_gate")

# 証跡採取コマンドの SSH タイムアウト（秒）。grounding.PROBE_TIMEOUT_SEC と同趣旨
PROBE_TIMEOUT_SEC = 30

# 検証 LLM へ渡す証跡の上限（LLM のコンテキストと SSH stdin を守る）
_PATCH_MAX_CHARS = 30000      # HEAD パッチ 1 本
_FILE_MAX_CHARS = 12000       # 対象ファイル 1 件の行番号付き内容
_EVIDENCE_MAX_CHARS = 80000   # 証跡全体
_ANSWER_MAX_CHARS = 12000     # 回答型の回答本文
# 検査対象パスの上限件数（target_paths は契約側で最大 5 件・acceptance 由来を足した保険）
_MAX_PATHS = 8

# 報告（Slack・結果ファイル）へ載せる 1 コマンド出力の上限（grounding と同じ値）
_REPORT_OUTPUT_MAX_CHARS = 1500

# パスの受理形式（SSH コマンド文字列に乗るため防御的に検証する。規則は grounding の
# _ACCEPT_PARAM_RE と同一 — `grep -n "A-Za-z0-9._/" src/orchestrator/grounding.py
# src/orchestrator/exit_gate.py` で一致を確認する）
_SAFE_PATH_RE = re.compile(r"\A[A-Za-z0-9._/\-]+\Z")

_VERDICTS = ("resolved", "partial", "unresolved")


class ExitGateReport:
    """独立検証 1 回分の結果。

    ok:    全件 resolved かつ新規不整合ゼロ（コード側で機械導出）。
    note:  ok=False のときの短い理由（通知ヘッダ・差し戻し通知に載せる）。
    text:  判定表・新規不整合・採取証跡を列挙したレポート本文（通知・結果ファイルへ添付）。
    cause: ok=False のときの機械可読な失敗原因コード
           （exit_gate_failed / exit_gate_unverified。None は原因なし＝ok）。
    items / new_issues: 検証エージェントの判定表（差し戻し時の worker 前置きに使う）。
    """

    def __init__(self, ok: bool, note: str, text: str, cause: str | None = None,
                 items: list | None = None, new_issues: list | None = None):
        self.ok = ok
        self.note = note
        self.text = text
        self.cause = cause
        self.items = items or []
        self.new_issues = new_issues or []

    def findings_text(self) -> str:
        """差し戻し時に worker 指示へ前置きする指摘一覧（未解消・部分解消・新規不整合のみ）。"""
        lines = []
        for it in self.items:
            if it.get("verdict") != "resolved":
                lines.append(f"- [{it.get('verdict')}] {it.get('req')}"
                             f"（根拠: {it.get('evidence', '')}）")
        for issue in self.new_issues:
            lines.append(f"- [新規不整合] {issue.get('issue')}"
                         f"（根拠: {issue.get('evidence', '')}）")
        return "\n".join(lines)


def collect_check_paths(task: dict) -> list[str]:
    """検査対象パス（成果物本体の採取対象）を契約フィールドから決定的に集める。

    第一入力は target_paths（発話からの逐語抽出）、次いで acceptance の path 系 params。
    LLM は関与しない。
    """
    paths: list[str] = []
    for p in (task.get("target_paths") or []):
        if isinstance(p, str) and p not in paths:
            paths.append(p)
    for a in (task.get("acceptance") or []):
        p = (a.get("params") or {}).get("path")
        if isinstance(p, str) and p not in paths:
            paths.append(p)
    return paths[:_MAX_PATHS]


def numbered_cat_command(workspace: str, path: str) -> str | None:
    """安全検証済みの行番号付き読み取りコマンドを返す（不正パスは None）。

    パスは SSH コマンド文字列に乗るため防御的に検証する。出口ゲートの証跡採取と
    分解入力の文書抜粋（§8.4「分解入力」・#171）が同じ検証・同じコマンド形を共用する。
    """
    if not _SAFE_PATH_RE.match(path) or ".." in path.split("/"):
        return None
    return f"cat -n {shlex.quote(workspace)}/{shlex.quote(path)}"


def collect_doc_excerpts(run_probe, workspace: str, paths: list,
                         per_file_cap: int, total_cap: int) -> str | None:
    """対象文書の行番号付き抜粋を固定コマンドで採取して 1 ブロックの文字列にする。

    分解入力（§8.4「分解入力」・#171）用。採取は sa-ru 側のコードのみで行い、
    LLM 出力をコマンドに接続しない。読めないファイル（不正パス・不在・probe 失敗）は
    スキップし、1 件も採取できなければ None（呼び出し側は従来どおり要約のみで分解 —
    採取の失敗で分解を止めない）。上限超過は切り詰めて明示する。
    """
    blocks: list[str] = []
    total = 0
    for path in paths or []:
        if not isinstance(path, str):
            continue
        command = numbered_cat_command(workspace, path)
        if command is None:
            continue
        try:
            rc, out = run_probe(command, PROBE_TIMEOUT_SEC)
        except Exception:
            logger.warning("文書抜粋の採取に失敗（スキップ）: %s", path)
            continue
        if rc != 0 or not (out or "").strip():
            continue
        body = out
        if len(body) > per_file_cap:
            body = body[:per_file_cap] + "\n…（以降略）"
        block = f"--- {path} ---\n{body}"
        if total + len(block) > total_cap:
            room = total_cap - total
            if room <= 0:
                break
            block = block[:room] + "\n…（以降略）"
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks) if blocks else None


def _request_doc(task: dict) -> str:
    """依頼の根拠文書ブロックを契約フィールドのみから組み立てる（worker 出力を含めない）。"""
    lines = [f"確定要約（着手確認で人が承認した依頼内容）:\n{task.get('command', '')}"]
    if task.get("directive"):
        lines.append(f"逐語命令（directive）:\n{task['directive']}")
    constraints = task.get("constraints") or []
    if constraints:
        lines.append("拘束条件（constraints）:")
        for c in constraints:
            forbid = "（禁止）" if c.get("forbid") else ""
            lines.append(f"- {c.get('text', '')}{forbid}")
    acceptance = task.get("acceptance") or []
    if acceptance:
        lines.append("承認された完了条件（acceptance）:")
        for a in acceptance:
            lines.append(f"- kind={a.get('kind')} params={json.dumps(a.get('params') or {}, ensure_ascii=False)}")
    if task.get("branch"):
        lines.append(f"対象ブランチ: {task['branch']}")
    if task.get("target_paths"):
        lines.append(f"対象パス: {', '.join(task['target_paths'])}")
    return "\n\n".join(lines)


class ExitGateVerifier:
    """成果物の実測証跡を固定コマンドで採取し、検証 LLM の判定表から総合判定を機械導出する。

    run_probe: callable(command: str, timeout: int) -> (rc: int, output: str)
        非 0 終了を例外化せず rc で返すこと（RemoteProcessManager.run_ssh_probe）。
    run_llm: callable(prompt: str) -> str
        書き込み不能の検証 LLM 単発呼び出し（Orchestrator._run_exit_gate_llm）。
        実行不能は例外で返す。
    prompt_template: prompts/exit_gate.md の内容（正本は md ファイル）。
    """

    def __init__(self, run_probe, run_llm, prompt_template: str):
        self._run_probe = run_probe
        self._run_llm = run_llm
        self._template = prompt_template

    # ── 証跡採取（固定カタログ・コード組立のみ。LLM 出力をコマンドに接続しない） ──

    def _probe(self, probes: list, command: str, cap: int):
        """採取コマンドを 1 本実行し (command, rc, output) を記録して返す。出力は cap で丸める。"""
        rc, output = self._run_probe(command, PROBE_TIMEOUT_SEC)
        out = (output or "")
        if len(out) > cap:
            out = out[:cap] + "\n…（以降略）"
        probes.append((command, rc, out))
        return rc, out

    def _collect_evidence(self, workspace: str | None, task: dict,
                          answer_text: str | None) -> tuple[str, list]:
        """成果物本体の実測証跡を採取する。戻り値は (LLM 向け証跡テキスト, probe 記録)。"""
        probes: list = []
        blocks: list[str] = []
        if workspace:
            ws = shlex.quote(workspace)
            rc, _ = self._probe(probes, f"git -C {ws} rev-parse --is-inside-work-tree", 200)
            is_repo = (rc == 0)
            if is_repo:
                self._probe(probes, f"git -C {ws} status --porcelain", 4000)
                self._probe(probes, f"git -C {ws} log --oneline -10", 2000)
                self._probe(probes, f"git -C {ws} show HEAD --stat", 4000)
                self._probe(probes, f"git -C {ws} show HEAD", _PATCH_MAX_CHARS)
            for path in collect_check_paths(task):
                # パス検証・コマンド組立は分解入力の抜粋採取と共用（不正は採取しない。
                # 判定材料が減る＝unresolved 側に倒れるだけで、偽完了は生まない）。
                # cat -n で行番号付き内容を採る（要件突合の判定根拠を「行番号」にするため）
                command = numbered_cat_command(workspace, path)
                if command is None:
                    continue
                self._probe(probes, command, _FILE_MAX_CHARS)
        for command, rc, out in probes:
            blocks.append(f"$ {command} (rc={rc})\n{out if out.strip() else '（出力なし）'}")
        # 回答型依頼（acceptance に answered）は回答本文が成果物そのもの（§8.10f 出口ゲート）
        if answer_text is not None:
            body = answer_text[:_ANSWER_MAX_CHARS] + (
                "\n…（以降略）" if len(answer_text) > _ANSWER_MAX_CHARS else "")
            blocks.append(f"回答本文（回答型依頼の成果物）:\n{body}")
        evidence = "\n\n".join(blocks) if blocks else "（採取できた証跡なし）"
        if len(evidence) > _EVIDENCE_MAX_CHARS:
            evidence = evidence[:_EVIDENCE_MAX_CHARS] + "\n…（以降略）"
        return evidence, probes

    # ── 判定 ──

    def _parse(self, stdout: str) -> tuple[list, list] | None:
        """検証 LLM の出力から判定表を取り出す。解釈できなければ None（呼び出し側でリトライ）。"""
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
        new_issues = parsed.get("new_issues")
        if not isinstance(items, list) or not isinstance(new_issues, list):
            return None
        # 空の判定表は「全件突合が行われていない」＝解釈不能と同じ扱い（リトライ→fail-closed）。
        # 根拠文書には常に確定要約が含まれ、数え上げる要件が 0 件になることはない。
        # 空 [] を受理すると検証 LLM の手抜き出力が無検証のままゲートを PASS させる
        if not items:
            return None
        for it in items:
            if not isinstance(it, dict) or it.get("verdict") not in _VERDICTS:
                return None
        if any(not isinstance(i, dict) for i in new_issues):
            return None
        return items, new_issues

    def _report_text(self, items: list, new_issues: list, probes: list) -> str:
        """独立検証レポート本文（判定表・新規不整合・採取コマンドと rc・実出力）。"""
        lines = ["【独立検証】実装と別系統の検証エージェントが根拠文書と成果物本体のみで判定",
                 f"要件全件突合（{len(items)} 件）:"]
        if not items:
            lines.append("（根拠文書から数え上げた要件なし）")
        for it in items:
            lines.append(f"- [{it.get('verdict')}] {it.get('req')}"
                         f"（根拠: {it.get('evidence', '')}）")
        lines.append(f"新規持ち込み不整合の敵対的検査（{len(new_issues)} 件）:")
        if not new_issues:
            lines.append("（検出なし）")
        for issue in new_issues:
            lines.append(f"- {issue.get('issue')}（根拠: {issue.get('evidence', '')}）")
        lines.append("採取した実測証跡（成果物本体）:")
        if not probes:
            lines.append("（workspace なし — 回答本文のみで判定）")
        for command, rc, out in probes:
            lines.append(f"$ {command} (rc={rc})")
            body = out.strip()
            if len(body) > _REPORT_OUTPUT_MAX_CHARS:
                body = body[:_REPORT_OUTPUT_MAX_CHARS] + "\n…（以降略）"
            lines.append(body if body else "（出力なし）")
        return "\n".join(lines)

    def verify(self, workspace: str | None, task: dict, machine_checks: str,
               answer_text: str | None = None) -> ExitGateReport:
        """独立検証を 1 回行う。総合判定はコード側で機械導出する。

        machine_checks: GroundingVerifier の証跡テキスト（機械検査の実測。worker 出力ではない）。
        answer_text: 回答型依頼（acceptance に answered）のときの worker 回答本文。
            それ以外は None を渡す（自己申告の遮断）。
        """
        evidence, probes = self._collect_evidence(workspace, task, answer_text)
        prompt = (self._template
                  .replace("{request_doc}", _request_doc(task))
                  .replace("{evidence}", evidence)
                  .replace("{machine_checks}", machine_checks or "（なし）"))
        parsed = None
        for attempt in (1, 2):   # JSON 逸脱は同一バックエンドで 1 回リトライ（§8.10f）
            try:
                stdout = self._run_llm(prompt)
            except Exception as e:
                logger.warning("独立検証 LLM の実行に失敗（%d 回目）: %s", attempt, e)
                continue
            parsed = self._parse(stdout)
            if parsed is not None:
                break
            logger.warning("独立検証 LLM の出力を判定表として解釈できない（%d 回目）", attempt)
        if parsed is None:
            # 「未検査」— 測れていないのであって、測って落ちた（未達）のではない
            # （失敗報告の帰属区別・§8.10f）
            note = ("独立検証を実行できませんでした（未検査 — "
                    "検証エージェントの応答を判定表として解釈できない）")
            return ExitGateReport(
                ok=False, note=note, cause="exit_gate_unverified",
                text=self._report_text([], [], probes) + f"\n判定: {note}")
        items, new_issues = parsed
        # 総合判定の機械導出（§8.10f）: LLM の合格宣言は採らない
        bad = [it for it in items if it.get("verdict") != "resolved"]
        ok = not bad and not new_issues
        if ok:
            note = ""
            text = self._report_text(items, new_issues, probes) + \
                f"\n判定: 要件 {len(items)} 件すべて resolved・新規不整合なし"
        else:
            parts = []
            if bad:
                parts.append(f"未解消/部分解消の要件 {len(bad)} 件")
            if new_issues:
                parts.append(f"新規不整合 {len(new_issues)} 件")
            note = "独立検証で未達（" + "・".join(parts) + "）"
            text = self._report_text(items, new_issues, probes) + f"\n判定: {note}"
        return ExitGateReport(ok=ok, note=note, text=text,
                              cause=None if ok else "exit_gate_failed",
                              items=items, new_issues=new_issues)
