"""y/n インターセプター — interactive(pty) アダプタが stdout から承認プロンプトを検出する。

構築手順書: docs/procedures/08-approval-pipeline.md Step 2（y/n インターセプター）

役割: agy 対話・将来の Codex 等、レガシーな単純テキスト y/n（`[y/n]` / `(yes/no)` / `Allow?`）を
出す汎用対話 CLI を pty 経由で扱うアダプタの検出層。検出したプロンプトは to_pending で中核の
PendingApproval に変換し、承認判定は CLI 非依存の decide() が行う。

Claude Code の Ink TUI メニュー承認は headless アダプタ（claude -p + PreToolUse フック）へ移行した
ため、Ink 検出（MENU / TRUST_DIALOG）は本モジュールから撤去した（設計 §6）。本モジュールは
レガシー y/n 専用に戻り、特定 CLI にロックインしない汎用対話の抽象を保つ。
"""

import re
from dataclasses import dataclass
from enum import Enum


class PromptType(Enum):
    """検出した承認プロンプトの形式。CLI ごとの聞き方の違いを正規化した種別。"""
    YN = "yn"
    YES_NO = "yes_no"
    ALLOW = "allow"
    UNKNOWN = "unknown"


@dataclass
class InterceptedPrompt:
    """検出した 1 件の承認プロンプト。判定に必要な情報を 1 つにまとめた値。"""
    prompt_type: PromptType
    raw_text: str
    command: str | None   # 要求されているコマンド/操作。スクレイプで復元できなければ None（判定不能）
    context: str          # 前後のstdout


# 承認プロンプトの検出パターン（レガシー: 単純テキスト形式）。CLI ごとに y/n の聞き方が
# 異なる（agy 等は (yes/no) や Allow? 等）ため、特定 CLI に縛られず共通に拾えるよう複数形式を列挙する。
PATTERNS = [
    (re.compile(r'\[y/n\]', re.IGNORECASE), PromptType.YN),
    (re.compile(r'\(yes/no\)', re.IGNORECASE), PromptType.YES_NO),
    (re.compile(r'Allow\?', re.IGNORECASE), PromptType.ALLOW),
]

# 承認プロンプトではない help/usage 出力を誤検出しないための除外語（設計 §3.3 (3) 検出精度）。
# 例: `Usage: foo [y/n]` の `[y/n]` はフラグ説明であって承認要求ではない。マーカーが載る行に
# これらの語があれば承認プロンプトと見なさない（偽の承認フローで無関係な文字列を審査する事故を防ぐ）。
# 対象は help ブロック見出し語（usage/synopsis/options）に限る。example/help/e.g. 等の一般語は
# 正当なプロンプト（`Overwrite example.py? [y/n]` / `Show help? [y/n]`）にも現れ、実プロンプトを
# 取りこぼす（未応答でハング）ため除外語には含めない。
_NON_PROMPT_RE = re.compile(r'\b(usage|synopsis|options?)\b', re.IGNORECASE)

# ANSI エスケープ（CSI / OSC / 文字セット指定）除去。端末出力には制御コードが混じるため、
# 承認リクエストの Context 欄や command 抽出の前にこれを取り除く（生の ANSI が Slack 上で
# 文字化けする欠陥を実機検証で確認・是正）。文字セット指定（ESC ( B 等、CSI/OSC のどちらでも
# ない第三の形式）も対象にする（当初 CSI/OSC のみで素通りしていた欠陥を是正）。
_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]')


def strip_ansi(text: str) -> str:
    """ANSI エスケープシーケンスを除去した表示テキストを返す。

    orchestrator 側（_drive の context_buf 蓄積）からも呼ぶため公開関数にする
    （Tier3 承認リクエストの Context 欄に生の ANSI が混入し Slack 上で文字化けする
    欠陥を実機検証で確認・是正）。
    """
    return _ANSI_RE.sub("", text)


# 後方互換（モジュール内の既存呼び出しとの一貫性のため残す）
_strip_ansi = strip_ansi


def detect_prompt(stdout_line: str, context_buffer: list[str]) -> InterceptedPrompt | None:
    """stdout の 1 行/1 チャンクがレガシー y/n 承認プロンプトかを判定する。

    pty アダプタが呼び出す。単純テキスト形式（YN / YES_NO / ALLOW）を PATTERNS で走査し、
    該当すれば承認対象コマンドを直近の context_buffer から復元して InterceptedPrompt に同梱する
    （プロンプト行自体には「何を承認するか」が乗らないことが多いため）。非該当では None を返し、
    アダプタはそのまま素通しする。

    ただしマーカーが usage/help 出力（`Usage: foo [y/n]` 等）に含まれるだけの場合は承認要求では
    ないため None を返す（設計 §3.3 (3) 検出精度）。マーカーが載る行＝出力末尾行で判定する
    （pexpect はマーカー出現位置で before を切るため、combined の末尾行がマーカーの載る行）。
    command が復元できなければ None のまま同梱し、判定不能として中核が Tier3 へ回す。
    """
    marker_line = stdout_line.splitlines()[-1] if stdout_line else stdout_line
    for pattern, ptype in PATTERNS:
        if pattern.search(stdout_line):
            if _NON_PROMPT_RE.search(marker_line):
                return None
            command = extract_command(context_buffer)
            return InterceptedPrompt(
                prompt_type=ptype,
                raw_text=stdout_line,
                command=command,
                context="\n".join(context_buffer[-10:]))
    return None


def to_pending(prompt: 'InterceptedPrompt'):
    """interactive アダプタの変換層: 検出プロンプトを中核の PendingApproval に変換する。

    レガシー y/n の stdout scrape では tool_name/tool_input が構造化されていないため、
    scrape した command 文字列を `tool_input["command"]` に載せ、tool_name は空にする
    （operation_str が空 tool_name を command そのものとして扱う）。headless アダプタは
    フック stdin の構造化 JSON を直接 PendingApproval にできるため、この変換を経由しない。

    source="interactive" を明示し、中核 decide() が信頼境界フェイルセーフを適用できるようにする
    （単一スクレイプ行での Tier1 自動承認を禁じ、最低 qu-e 審査へ）。command が None（判定不能）の
    ときは tool_input に command を載せず、decide() が人間承認（Tier3）へ回す（設計 §3.3 (3)）。
    """
    from approval_types import PendingApproval
    tool_input = {} if prompt.command is None else {"command": prompt.command}
    return PendingApproval(
        tool_name="",
        tool_input=tool_input,
        context=prompt.context,
        source="interactive",
    )


def extract_command(context: list[str]) -> str | None:
    """承認対象のコマンド/操作を stdout バッファから復元する。

    CLI は承認を求める直前に「これから何をするか」を Run: / Execute: / Write to: の行で
    提示する。プロンプト行に最も近い提示を採るため新しい行から遡って最初の一致を返す。
    手掛かりが無ければ None（＝判定不能）。旧実装は "unknown" を返していたが、これは中核の
    分類器に「unknown」という無害文字列として渡り Tier1 自動承認され得た（危険操作が文脈不明の
    名の下に素通り）。判定不能を None で表し、中核が人間承認（Tier3）へフェイルセーフする
    （設計 §3.3 (3) 信頼境界）。
    """
    for line in reversed(context):
        if any(prefix in line for prefix in ["Run:", "Execute:", "Write to:"]):
            return line.strip()
    return None
