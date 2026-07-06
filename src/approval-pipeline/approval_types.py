"""承認判定の CLI 非依存な型 — PendingApproval / Decision と操作文字列化。

設計書 §3（承認パイプライン設計）の中核が受け渡す型を定義する。中核（ApprovalPipeline.decide）
は worker CLI の存在を知らず、この構造化データだけを扱う。各実行アダプタ（headless / interactive）
が自 CLI の承認要求を PendingApproval へ、判定結果 Decision を自 CLI の伝達手段へ変換する。
"""

import json
from dataclasses import dataclass, field


@dataclass
class PendingApproval:
    """承認判定にかける 1 件のツール実行要求（CLI 非依存の入力型）。

    アダプタが自 CLI の承認要求（headless=PreToolUse フックの stdin JSON、
    interactive=レガシー y/n の context 抽出）をこの型に変換して中核へ渡す。
    """
    tool_name: str            # 例 "Bash" / "Write" / "Edit"（interactive で不明なら ""）
    tool_input: dict          # 例 {"command": "..."} / {"file_path": "...", "content": "..."}
    tool_use_id: str = ""     # 監査突合・冪等用の一意 ID（無ければ空）
    context: str = ""         # 承認者向けの前後文脈（Slack 提示・qu-e 審査に使う）
    source: str = "headless"  # 承認要求の由来。"headless"=フック stdin の権威的 tool_input、
                              # "interactive"=非構造・非信頼な stdout スクレイプ由来（設計 §3.3 (3)）。
                              # 中核 decide() が interactive にフェイルセーフ（Tier1 禁止・判定不能→Tier3）を適用する。


@dataclass
class Decision:
    """中核が返す裁定（CLI 非依存の出力型）。

    アダプタがこれを自 CLI の伝達手段へ変換する（headless=permissionDecision:allow / exit 2、
    interactive=y/n 送信）。escalate は Tier2→Tier3 へ繋ぐ内部シグナル。
    """
    allow: bool
    handler: str = ""         # 監査用のハンドラ名（tier1_auto / tier2_sentinel / tier3_human / safety_deny）
    reason: str = ""          # 監査・Slack 提示用の判定理由
    escalate: bool = False    # Tier2 が qu-e deny 時に Tier3 へ上げるための内部シグナル


def operation_str(pending: PendingApproval) -> str:
    """PendingApproval を、安全性チェック照合・分類器・qu-e 審査が扱う操作文字列に整形する。

    旧実装の scrape 文字列（"Write to: <path>" / Bash コマンド）と同じ書式を保ち、
    安全性チェックの語境界正規表現・分類プロンプト（classify_risk.md）の期待入力を壊さない。
    """
    ti = pending.tool_input or {}
    name = pending.tool_name
    # Bash / 形式不明（interactive のレガシー y/n）は command 文字列をそのまま操作文字列にする。
    if name in ("", "Bash"):
        cmd = ti.get("command")
        if cmd is not None:
            return cmd
    # 書き込み系は旧 scrape と同じ "Write to: <path>" 書式（分類プロンプト・安全性チェックの期待に合わせる）。
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"Write to: {ti.get('file_path', '')}"
    if not name:
        return ""
    return f"{name}: {json.dumps(ti, ensure_ascii=False)}"
