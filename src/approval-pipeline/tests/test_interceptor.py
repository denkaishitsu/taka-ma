"""y/n インターセプター（interactive(pty) アダプタの検出層）単体テスト。

Claude Code の Ink メニュー承認は headless アダプタへ移行したため、本モジュールはレガシー y/n
（agy 対話等の汎用対話 CLI 用）専用に戻った。ここではその検出・command 復元・PendingApproval
変換・ANSI 除去を検証する。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

from interceptor import detect_prompt, strip_ansi, extract_command, to_pending, PromptType


def test_detect_yn_prompt():
    """`[y/n]` 形式のプロンプトを YN として検出し、context から command を復元する。"""
    result = detect_prompt("Do you want to proceed? [y/n]", ["Run: git push origin main"])
    assert result is not None
    assert result.prompt_type == PromptType.YN
    assert result.command == "Run: git push origin main"


def test_detect_allow_prompt():
    """`Allow?` 形式のプロンプトも検出する（CLI ごとの聞き方の違いを共通に拾う）。"""
    result = detect_prompt("Allow? (y/n)", ["Write to: src/app.ts"])
    assert result is not None
    assert result.prompt_type in (PromptType.ALLOW, PromptType.YES_NO)


def test_detect_prompt_none_for_plain_output():
    """承認プロンプトでない通常の出力行では None を返す（素通し）。"""
    assert detect_prompt("Building project...", ["Run: npm run build"]) is None


def test_extract_command_picks_nearest_directive():
    """Run/Execute/Write to の提示行のうち、プロンプトに最も近い（新しい）ものを採る。"""
    ctx = ["Run: old-command", "some noise", "Write to: src/new.ts"]
    assert extract_command(ctx) == "Write to: src/new.ts"
    # 手掛かりが無ければ None（＝判定不能）。旧実装の "unknown" を無害文字列として渡さない。
    assert extract_command(["just logs", "no directive"]) is None


def test_detect_prompt_undeterminable_command_is_none():
    """提示行が無いプロンプトでは command を None（判定不能）にして同梱する。"""
    result = detect_prompt("Do you want to proceed? [y/n]", ["just logs", "no directive"])
    assert result is not None
    assert result.command is None


def test_detect_prompt_ignores_usage_help_line():
    """`Usage: foo [y/n]` 等の help/usage 出力は承認要求ではないため検出しない（誤検出是正）。"""
    assert detect_prompt("Usage: foo [y/n]", ["Run: git push"]) is None
    assert detect_prompt("Options: --confirm (yes/no)", ["Run: git push"]) is None
    # help 語が無い本物のプロンプトは従来どおり検出する。
    assert detect_prompt("Proceed? [y/n]", ["Run: git push"]) is not None


def test_detect_prompt_does_not_drop_real_prompt_with_common_words():
    """回帰: `example`/`help` 等の一般語を含む正当なプロンプトを誤棄却しない（未応答ハング防止）。

    除外語を usage/synopsis/options の help 見出し語に限定した是正の回帰。ファイル名 example.py や
    `Show help?` のような実プロンプトが検出されなくなると承認が飛ばず worker がハングする。
    """
    assert detect_prompt("Overwrite example.py? [y/n]", ["Run: cp x example.py"]) is not None
    assert detect_prompt("Show help? [y/n]", ["Run: mytool --help"]) is not None


def test_to_pending_converts_to_structured():
    """検出プロンプトを中核の PendingApproval へ変換する（tool_name は空・command を tool_input に）。"""
    result = detect_prompt("proceed? [y/n]", ["Run: rm -rf /tmp/x"])
    pending = to_pending(result)
    assert pending.tool_name == ""
    assert pending.tool_input == {"command": "Run: rm -rf /tmp/x"}
    # interactive 由来を明示（中核のフェイルセーフ判定に使う）。
    assert pending.source == "interactive"


def test_to_pending_undeterminable_has_no_command():
    """判定不能（command None）なら tool_input に command を載せない（中核が Tier3 へ回す）。"""
    result = detect_prompt("proceed? [y/n]", ["no directive here"])
    pending = to_pending(result)
    assert pending.tool_input == {}
    assert pending.source == "interactive"


def test_strip_ansi_removes_charset_designation_sequences():
    """ESC ( B 等（文字セット指定、CSI/OSC どちらでもない第三形式）を除去する。

    実機の Tier3 承認リクエスト Context 欄に "\\x1b(B" が残り文字化けする欠陥を
    実機検証で確認・是正。
    """
    raw = "Create file\x1b(B../../../../tmp/t08-check.txt\x1b(B?"
    assert "\x1b" not in strip_ansi(raw)
    assert strip_ansi(raw) == "Create file../../../../tmp/t08-check.txt?"
