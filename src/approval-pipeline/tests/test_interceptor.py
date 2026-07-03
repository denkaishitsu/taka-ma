"""y/n インターセプター単体テスト。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

from interceptor import detect_prompt, InterceptedPrompt, PromptType


def test_detect_yn_prompt():
    line = "Do you want to proceed? [y/n]"
    result = detect_prompt(line, ["Run: git push origin main"])
    assert result is not None
    assert result.prompt_type == PromptType.YN


def test_detect_allow_prompt():
    line = "Allow? (y/n)"
    result = detect_prompt(line, ["Write to: src/app.ts"])
    assert result is not None
