"""y/n インターセプター — PTY stdout から承認プロンプトを検出する。

構築手順書: docs/procedures/08-approval-pipeline.md Step 2（y/n インターセプター）
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
    command: str          # 要求されているコマンド/操作
    context: str          # 前後のstdout


# 承認プロンプトの検出パターン。CLI ごとに y/n の聞き方が異なる（Claude Code は [y/n]、
# 他ツールは (yes/no) や Allow? 等）ため、特定 CLI に縛られず共通に拾えるよう複数形式を列挙する。
PATTERNS = [
    (re.compile(r'\[y/n\]', re.IGNORECASE), PromptType.YN),
    (re.compile(r'\(yes/no\)', re.IGNORECASE), PromptType.YES_NO),
    (re.compile(r'Allow\?', re.IGNORECASE), PromptType.ALLOW),
]


def detect_prompt(stdout_line: str, context_buffer: list[str]) -> InterceptedPrompt | None:
    """stdout の 1 行が承認プロンプトかを判定し、該当すれば承認対象コマンドと前後文脈を添えて返す。

    PTY ラッパーが行単位で呼び出す。プロンプト行自体には「何を承認するか」が乗らないことが
    多いため、直近の context_buffer から対象コマンドを復元して InterceptedPrompt に同梱する。
    非該当行では None を返し、ラッパーはそのまま素通しする。
    """
    for pattern, ptype in PATTERNS:
        if pattern.search(stdout_line):
            command = extract_command(context_buffer)
            return InterceptedPrompt(
                prompt_type=ptype,
                raw_text=stdout_line,
                command=command,
                context="\n".join(context_buffer[-10:]),
            )
    return None


def extract_command(context: list[str]) -> str:
    """承認対象のコマンド/操作を stdout バッファから復元する。

    CLI は承認を求める直前に「これから何をするか」を Run: / Execute: / Write to: の行で
    提示する。プロンプト行に最も近い提示を採るため新しい行から遡って最初の一致を返す。
    手掛かりが無ければ "unknown"（承認画面には文脈不明として提示される）。
    """
    for line in reversed(context):
        if any(prefix in line for prefix in ["Run:", "Execute:", "Write to:"]):
            return line.strip()
    return "unknown"
