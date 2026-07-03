"""承認パイプライン E2E テスト — ApprovalPipeline.process の Tier 振り分けを検証する。

検証する振る舞い（安全装置の核心・§8.4 / §8.10）:
  - Tier 1（読み取り専用など低リスク）→ 自動承認され PTY に y が送られる
  - Tier 3（sudo など高リスク）→ 人間承認へエスカレートし、Slack 承認リクエストが
    送信元へ飛び、人間の approve が PTY に反映される
  - いずれも監査ログに tier / decision が記録される

ya-ta（ai_gateway）への Tier 判定と qu-e（SSH）は外部依存のため、分類結果は FakeClassifier で
注入し、判定そのものではなく「分類結果 → 正しいハンドラへ振り分け → 正しい action」を保証する。
pytest-asyncio に依存せず実行できるよう、各テストは asyncio.run() で同期的に駆動する
（test_tier3_crossprocess.py と同方式）。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

import asyncio
import os
import tempfile

import tier3_handler as t3
from interceptor import InterceptedPrompt, PromptType
from main import ApprovalPipeline

# RiskClassifier.__init__ は config["ya-ta"]["model"] を読むだけ（ollama 実行は classify 時のみ）。
# 本テストは classifier を FakeClassifier に差し替えるため、モデル名はダミーで足りる。
CONFIG = {"ya-ta": {"model": "dummy-model"}}


class FakePTY:
    """WorkerPtyWrapper の approve()/deny() だけを観測するスタブ。

    last_action は実際に PTY へ送られる文字（承認=y / 拒否=n）を記録する。
    """
    instance_id = "test-instance"

    def __init__(self):
        self.last_action = None

    def approve(self):
        self.last_action = "y"

    def deny(self):
        self.last_action = "n"


class FakeNotifier:
    """SlackNotifier のスタブ。送信された承認リクエストを観測する。"""

    def __init__(self):
        self.sent = None
        self.notes = []

    def send_approval_request(self, **kw):
        self.sent = kw

    def notify(self, text, channel=None, team_id=None):
        self.notes.append(text)


class FakeClassifier:
    """ya-ta（ai_gateway）への分類依頼を固定 tier で置き換えるスタブ。"""

    def __init__(self, tier, reason="test"):
        self._tier = tier
        self._reason = reason

    async def classify(self, prompt):
        return {"tier": self._tier, "reason": self._reason}


class FakeLogger:
    """AuditLogger のスタブ。/opt 配下へ書かず、記録されたエントリを観測する。"""

    def __init__(self):
        self.entries = []

    def log(self, entry):
        self.entries.append(entry)


def _pipeline(tier, notifier, approval_dir=None):
    """classifier を固定 tier に、logger をメモリに差し替えた ApprovalPipeline を組む。"""
    pipeline = ApprovalPipeline(CONFIG, slack_notifier=notifier)
    pipeline.classifier = FakeClassifier(tier)
    pipeline.logger = FakeLogger()
    if approval_dir is not None:
        # Tier 3 は /opt 配下の既定ディレクトリではなく、テスト用 tmp へ承認ファイルを書かせる。
        pipeline.handlers[3] = t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir)
    return pipeline


def test_tier1_auto_approve():
    """読み取り専用コマンド（Tier 1）は自動承認され、PTY に y が送られる。"""
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Read file? [y/n]",
        command="cat src/readme.md",
        context="",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-1"))

    assert result["action"] == "approved"
    assert result["handler"] == "tier1_auto"
    assert pty.last_action == "y"                       # 自動承認が PTY に反映される
    assert notifier.sent is None                        # 低リスクは人間に通知しない
    # 監査ログに Tier 1 / approved が残る
    assert pipeline.logger.entries[-1]["tier"] == 1
    assert pipeline.logger.entries[-1]["decision"] == "approved"


def test_tier3_dangerous_command_escalates_to_human():
    """ya-ta が Tier 3 判定したコマンドは人間承認へエスカレートし、承認後に PTY へ y が送られる。

    コマンドは安全床（always_deny / always_escalate）に該当しないものを使い、
    「分類結果 Tier 3 → Tier 3 ハンドラ振り分け」経路そのものを検証する
    （sudo 等の安全床該当コマンドは別テスト test_safety_* が担当）。
    """
    notifier = FakeNotifier()
    approval_dir = tempfile.mkdtemp()
    pipeline = _pipeline(3, notifier, approval_dir=approval_dir)
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="git push --force origin main",
        context="Run: git push --force origin main",
    )

    async def scenario():
        # ポーリング中に「人間が Slack で Approve を押した」ことを承認ファイルへ書いて模す。
        async def human_approves():
            await asyncio.sleep(0.1)
            request_id = notifier.sent["request_id"]
            path = os.path.join(approval_dir, f"{request_id}.json")
            t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir)._mark_status(
                path, t3.STATUS_APPROVED)
        res, _ = await asyncio.gather(
            pipeline.process(prompt, pty, "test-3", team_id="T1", channel="C1"),
            human_approves(),
        )
        return res

    # ポーリング間隔とタイムアウトを縮めて高速化（既定は 1 秒 / 5 分）。
    orig_poll, orig_to = t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS
    t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = 0.05, 2.0
    try:
        result = asyncio.run(scenario())
    finally:
        t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = orig_poll, orig_to

    assert result["handler"] == "tier3_human"           # Tier 3（人間承認）へ振り分けられた
    assert notifier.sent is not None                    # Slack 承認リクエストが送信された
    assert notifier.sent["command"] == "git push --force origin main"
    assert notifier.sent["team_id"] == "T1"             # 送信元ワークスペースへ返す（§8.10）
    assert result["action"] == "approved"               # 人間が approve → 承認
    assert pty.last_action == "y"                       # 承認結果が PTY に反映される
    assert pipeline.logger.entries[-1]["tier"] == 3


def test_safety_always_deny_blocks_before_tier():
    """always_deny 該当コマンドは Tier 判定前に即時拒否される（§3.3 (0) / 08手順書 §6）。

    classifier を Tier 1（自動承認）に固定しても、安全床が先に発火して deny になることで
    「LLM 判定の前段で決定論的に止まる」最終防壁を検証する。
    """
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)        # 仮に分類器が Tier1 でも安全床が優先される
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="Run: rm -rf /",
        context="Run: rm -rf /",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-deny"))

    assert result["handler"] == "safety_deny"           # Tier ハンドラではなく安全床が処理
    assert result["action"] == "denied"
    assert "always_deny" in result["reason"]
    assert pty.last_action == "n"                        # 即時 n（拒否）
    assert notifier.sent is None                         # 人間にも問い合わせない（即拒否）
    entry = pipeline.logger.entries[-1]
    assert entry["tier"] == 0                            # Tier 判定前（0）として記録
    assert "always_deny" in entry["reason"]


def test_safety_always_escalate_routes_to_human():
    """always_escalate 該当コマンド（sudo 等）は分類をスキップして Tier 3（人間承認）へ直行する。"""
    notifier = FakeNotifier()
    approval_dir = tempfile.mkdtemp()
    pipeline = _pipeline(1, notifier, approval_dir=approval_dir)  # 分類器 Tier1 でも安全床が優先
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="sudo chmod 777 /etc/hosts",
        context="Run: sudo chmod 777 /etc/hosts",
    )

    async def scenario():
        async def human_approves():
            await asyncio.sleep(0.1)
            request_id = notifier.sent["request_id"]
            path = os.path.join(approval_dir, f"{request_id}.json")
            t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir)._mark_status(
                path, t3.STATUS_APPROVED)
        res, _ = await asyncio.gather(
            pipeline.process(prompt, pty, "test-esc", team_id="T1", channel="C1"),
            human_approves(),
        )
        return res

    orig_poll, orig_to = t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS
    t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = 0.05, 2.0
    try:
        result = asyncio.run(scenario())
    finally:
        t3.POLL_INTERVAL, t3.TIMEOUT_SECONDS = orig_poll, orig_to

    assert result["handler"] == "tier3_human"           # 安全床 → Tier 3 へ直行
    assert notifier.sent is not None                    # 人間へ承認リクエスト送信
    assert "always_escalate" in notifier.sent["risk_reason"]
    assert result["action"] == "approved"
    entry = pipeline.logger.entries[-1]
    assert entry["tier"] == 3
    # bug_002: escalate 経由でも監査ログ reason に安全床由来を残す（always_deny と対称・§3.5）
    assert "always_escalate" in entry["reason"]


def test_safety_partial_match_does_not_false_deny():
    """bug_003 回帰: `rm -rf /tmp/...` を `rm -rf /` の部分一致で誤拒否しない（語境界照合）。

    安全床に該当しないので Tier 判定へ進み、Tier1 として自動承認される（safety_deny にならない）。
    """
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="Run: rm -rf /tmp/build/foo",
        context="Run: rm -rf /tmp/build/foo",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-partial"))

    assert result["handler"] != "safety_deny"           # 安全床は発火しない
    assert result["handler"] == "tier1_auto"            # 通常の Tier 判定（Tier1）へ進む
    assert result["action"] == "approved"
    assert pty.last_action == "y"
