"""承認パイプライン E2E テスト — ApprovalPipeline の Tier 振り分けを検証する。

検証する振る舞い（安全装置の核心・§8.4 / §8.10）:
  - Tier 1（読み取り専用など低リスク）→ 自動承認され PTY に y が送られる
  - Tier 3（sudo など高リスク）→ 人間承認へエスカレートし、Slack 承認リクエストが
    送信元へ飛び、人間の approve が PTY に反映される
  - いずれも監査ログに tier / decision が記録される

中核 decide() は CLI 非依存で Decision を返すだけなので、本テストは interactive(pty)
アダプタである process()（decide + y/n 伝達）を driver に使い、Decision と PTY 反映の
両方を観測する。ya-ta（ai_gateway）への Tier 判定と qu-e（SSH）は外部依存のため、分類結果は
FakeClassifier で注入し、「分類結果 → 正しいハンドラへ振り分け → 正しい allow/deny」を保証する。
pytest-asyncio に依存せず、各テストは asyncio.run() で同期駆動する。

構築手順書: docs/procedures/08-approval-pipeline.md Step 8（テスト）
"""

import asyncio
import os
import tempfile

import tier3_handler as t3
from approval_types import Decision, PendingApproval
from interceptor import InterceptedPrompt, PromptType
from main import ApprovalPipeline, DEFAULT_ALWAYS_DENY, DEFAULT_ALWAYS_ESCALATE, _normalize_command, _union

# RiskClassifier.__init__ は config["ya-ta"]（model / llm_timeout_sec / llm_think）と
# config["sa-ru"]["ollama_host"] を読むだけ（ollama 実行は classify 時のみ）。本テストは
# classifier を FakeClassifier に差し替えるため、モデル名・接続先・timeout はダミーで足りる。
CONFIG = {"ya-ta": {"model": "dummy-model", "llm_timeout_sec": 300},
          "sa-ru": {"ollama_host": "http://localhost:11434"},
          # #103 yaml SSOT 化: Tier2/Tier3 運用値は sa-ru.yaml approval が唯一の源になり
          # ApprovalPipeline 構築時の必須キーになった（実効値と同値を与える）
          "approval": {"tier2_timeout_sec": 120, "tier3_timeout_sec": 300,
                       "poll_interval_sec": 1}}


class FakePTY:
    """WorkerPtyWrapper の approve()/deny() だけを観測するスタブ。

    last_action は実際に PTY へ送られる文字（承認=y / 拒否=n）を記録する。
    """
    instance_id = "test-instance"

    def __init__(self):
        self.last_action = None

    def approve(self, prompt_type=None):
        self.last_action = "y"

    def deny(self, prompt_type=None):
        self.last_action = "n"


class FakeNotifier:
    """SlackNotifier のスタブ。送信された承認リクエストを観測する。"""

    def __init__(self):
        self.sent = None
        self.notes = []

    def send_approval_request(self, **kw):
        self.sent = kw

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)


class FakeClassifier:
    """ya-ta（ai_gateway）への分類依頼を固定 tier で置き換えるスタブ。"""

    def __init__(self, tier, reason="test"):
        self._tier = tier
        self._reason = reason

    async def classify(self, pending):
        return {"tier": self._tier, "reason": self._reason}


class FakeLogger:
    """AuditLogger のスタブ。/opt 配下へ書かず、記録されたエントリを観測する。"""

    def __init__(self):
        self.entries = []

    def log(self, entry):
        self.entries.append(entry)


class FakeTier2:
    """qu-e 審査（Tier2Handler）を SSH 無しで固定結果に差し替えるスタブ。

    interactive フェイルセーフで Tier1 が Tier2 へ引き上げられる経路を、実 SSH を張らずに
    観測する。approve=True なら allow、False なら Tier3 へ escalate（deny）。
    """

    def __init__(self, approve=True, reason="qu-e review"):
        self._approve = approve
        self._reason = reason
        self.called_with = None

    async def handle(self, pending, ctx=None) -> Decision:
        self.called_with = pending
        if self._approve:
            return Decision(allow=True, handler="tier2_sentinel")
        return Decision(allow=False, escalate=True, handler="tier2_sentinel", reason=self._reason)


def _pipeline(tier, notifier, approval_dir=None, tier2=None):
    """classifier を固定 tier に、logger をメモリに差し替えた ApprovalPipeline を組む。"""
    pipeline = ApprovalPipeline(CONFIG, slack_notifier=notifier)
    pipeline.classifier = FakeClassifier(tier)
    pipeline.logger = FakeLogger()
    if tier2 is not None:
        # Tier 2（qu-e 審査）は SSH を張るため、テストでは固定結果のスタブへ差し替える。
        pipeline.handlers[2] = tier2
    if approval_dir is not None:
        # Tier 3 は /opt 配下の既定ディレクトリではなく、テスト用 tmp へ承認ファイルを書かせる。
        # timeout/poll は構築時注入（#103）のため、旧モジュール定数の差し替えではなく
        # ここで短縮値を渡して高速化する（実効値は 300 秒 / 1 秒）。
        pipeline.handlers[3] = t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir,
                                               timeout_sec=2.0, poll_interval_sec=0.05)
    return pipeline


def test_headless_tier1_auto_approves():
    """headless（tool_input が権威的）の Tier 1 は従来どおり自動承認される（床上げの対象外）。

    decide() を直接 driver にし、source="headless" の PendingApproval が Tier1 のまま
    auto 承認されることを確認する（interactive フロアが headless に波及しないことの回帰）。
    """
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pending = PendingApproval(
        tool_name="Read", tool_input={"file_path": "src/readme.md"}, source="headless")

    result = asyncio.run(pipeline.decide(pending, instance_id="test-hl1"))

    assert result.allow                                 # headless Tier1 は自動承認のまま
    assert result.handler == "tier1_auto"
    assert notifier.sent is None                        # 低リスクは人間に通知しない
    assert pipeline.logger.entries[-1]["tier"] == 1
    assert pipeline.logger.entries[-1]["decision"] == "approved"


def test_interactive_tier1_floored_to_qu_e():
    """interactive(pty) は単一スクレイプ行を根拠に Tier1 自動承認せず、qu-e 審査(Tier2)へ床上げする。

    分類器が Tier1 を返しても、interactive 由来はなりすまし緩和のため最低 qu-e 審査を経る
    （設計 §3.3 (3) フェイルセーフ (2)）。qu-e が approve すれば allow・PTY へ y。
    """
    notifier = FakeNotifier()
    tier2 = FakeTier2(approve=True)
    pipeline = _pipeline(1, notifier, tier2=tier2)
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Read file? [y/n]",
        command="cat src/readme.md",
        context="Run: cat src/readme.md",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-1"))

    assert result.allow                                 # qu-e approve → 承認
    assert result.handler == "tier2_sentinel"           # Tier1 ではなく Tier2(qu-e) が処理
    assert tier2.called_with is not None                # qu-e 審査が呼ばれた
    assert pty.last_action == "y"                       # 承認が PTY に反映される
    assert notifier.sent is None                        # qu-e approve なら人間には上げない
    # 監査ログに Tier 2 / approved が残る（床上げ後の tier を記録）
    assert pipeline.logger.entries[-1]["tier"] == 2
    assert pipeline.logger.entries[-1]["decision"] == "approved"


def test_interactive_undeterminable_escalates_to_human():
    """承認対象を stdout から復元できない（command None）interactive プロンプトは人間承認へ直行する。

    旧実装は "unknown" を無害文字列として Tier1 自動承認し得た。判定不能は Tier1/2 に載せず
    Tier3（人間）へフェイルセーフする（設計 §3.3 (3) フェイルセーフ (1)）。
    """
    notifier = FakeNotifier()
    approval_dir = tempfile.mkdtemp()
    pipeline = _pipeline(1, notifier, approval_dir=approval_dir)  # 分類器 Tier1 でも判定不能が優先
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Do you want to proceed? [y/n]",
        command=None,                                   # 提示行が無く判定不能
        context="just logs\nno directive here",
    )

    async def scenario():
        async def human_approves():
            await asyncio.sleep(0.1)
            request_id = notifier.sent["request_id"]
            path = os.path.join(approval_dir, f"{request_id}.json")
            t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir,
                            timeout_sec=2.0, poll_interval_sec=0.05)._mark_status(
                path, t3.STATUS_APPROVED)
        res, _ = await asyncio.gather(
            pipeline.process(prompt, pty, "test-undet", team_id="T1", channel="C1"),
            human_approves(),
        )
        return res

    result = asyncio.run(scenario())

    assert result.handler == "tier3_human"              # 判定不能 → 人間承認へ直行
    assert notifier.sent is not None                    # Slack 承認リクエストが飛ぶ
    assert "interactive_undeterminable" in notifier.sent["risk_reason"]
    entry = pipeline.logger.entries[-1]
    assert entry["tier"] == 3
    assert "interactive_undeterminable" in entry["reason"]


def test_tier3_dangerous_command_escalates_to_human():
    """ya-ta が Tier 3 判定したコマンドは人間承認へエスカレートし、承認後に PTY へ y が送られる。

    コマンドは安全性チェック（always_deny / always_escalate）に該当しないものを使い、
    「分類結果 Tier 3 → Tier 3 ハンドラ振り分け」経路そのものを検証する
    （sudo 等の安全性チェック該当コマンドは別テスト test_safety_* が担当）。
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
            t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir,
                            timeout_sec=2.0, poll_interval_sec=0.05)._mark_status(
                path, t3.STATUS_APPROVED)
        res, _ = await asyncio.gather(
            pipeline.process(prompt, pty, "test-3", team_id="T1", channel="C1"),
            human_approves(),
        )
        return res

    # ポーリング間隔とタイムアウトは _pipeline が構築時に短縮値を注入済み（#103 で注入式に変更）。
    result = asyncio.run(scenario())

    assert result.handler == "tier3_human"              # Tier 3（人間承認）へ振り分けられた
    assert notifier.sent is not None                    # Slack 承認リクエストが送信された
    assert notifier.sent["command"] == "git push --force origin main"
    assert notifier.sent["team_id"] == "T1"             # 送信元ワークスペースへ返す（§8.10）
    assert result.allow                                 # 人間が approve → 承認
    assert pty.last_action == "y"                       # 承認結果が PTY に反映される
    assert pipeline.logger.entries[-1]["tier"] == 3


def test_safety_always_deny_blocks_before_tier():
    """always_deny 該当コマンドは Tier 判定前に即時拒否される（§3.3 (0) / 08手順書 §6）。

    classifier を Tier 1（自動承認）に固定しても、安全性チェックが先に発火して deny になることで
    「LLM 判定の前段で決定論的に止まる」最終防壁を検証する。
    """
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)        # 仮に分類器が Tier1 でも安全性チェックが優先される
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="Run: rm -rf /",
        context="Run: rm -rf /",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-deny"))

    assert result.handler == "safety_deny"              # Tier ハンドラではなく安全性チェックが処理
    assert not result.allow
    assert "always_deny" in result.reason
    assert pty.last_action == "n"                        # 即時 n（拒否）
    assert notifier.sent is None                         # 人間にも問い合わせない（即拒否）
    entry = pipeline.logger.entries[-1]
    assert entry["tier"] == 0                            # Tier 判定前（0）として記録
    assert "always_deny" in entry["reason"]


def test_safety_always_escalate_routes_to_human():
    """always_escalate 該当コマンド（sudo 等）は分類をスキップして Tier 3（人間承認）へ直行する。"""
    notifier = FakeNotifier()
    approval_dir = tempfile.mkdtemp()
    pipeline = _pipeline(1, notifier, approval_dir=approval_dir)  # 分類器 Tier1 でも安全性チェックが優先
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
            t3.Tier3Handler(slack_notifier=notifier, approval_dir=approval_dir,
                            timeout_sec=2.0, poll_interval_sec=0.05)._mark_status(
                path, t3.STATUS_APPROVED)
        res, _ = await asyncio.gather(
            pipeline.process(prompt, pty, "test-esc", team_id="T1", channel="C1"),
            human_approves(),
        )
        return res

    result = asyncio.run(scenario())

    assert result.handler == "tier3_human"              # 安全性チェック → Tier 3 へ直行
    assert notifier.sent is not None                    # 人間へ承認リクエスト送信
    assert "always_escalate" in notifier.sent["risk_reason"]
    assert result.allow
    entry = pipeline.logger.entries[-1]
    assert entry["tier"] == 3
    # escalate 経由でも監査ログ reason に安全性チェック由来を残す（always_deny と対称・§3.5）
    assert "always_escalate" in entry["reason"]


def test_safety_partial_match_does_not_false_deny():
    """回帰: `rm -rf /tmp/...` を `rm -rf /` の部分一致で誤拒否しない（語境界照合）。

    安全性チェックに該当しないので Tier 判定へ進む。interactive 由来のため Tier1 は qu-e 審査
    （Tier2）へ床上げされ、qu-e approve で承認される（safety_deny にならないことを確認）。
    """
    notifier = FakeNotifier()
    tier2 = FakeTier2(approve=True)
    pipeline = _pipeline(1, notifier, tier2=tier2)
    pty = FakePTY()
    prompt = InterceptedPrompt(
        prompt_type=PromptType.YN,
        raw_text="Execute? [y/n]",
        command="Run: rm -rf /tmp/build/foo",
        context="Run: rm -rf /tmp/build/foo",
    )

    result = asyncio.run(pipeline.process(prompt, pty, "test-partial"))

    assert result.handler != "safety_deny"              # 安全性チェックは発火しない
    assert result.handler == "tier2_sentinel"           # interactive 床上げで qu-e 審査へ
    assert result.allow
    assert pty.last_action == "y"


# ── 照合の正規化（自明なバイパス封じ・§3.3 (0)「照合の正規化」） ──

def test_normalize_collapses_whitespace_and_strips_abs_path():
    """正規化: 連続空白の畳み込みと先頭トークンの絶対パス接頭辞剥がし（単体）。"""
    # 期待値の `-fr` は (c) フラグ順ソート後の正規形（`-rf`→`-fr`）。
    assert _normalize_command("rm   -rf   /") == "rm -fr /"       # (a) 空白畳み込み
    assert _normalize_command("\trm\t-rf\n/") == "rm -fr /"       # (a) タブ・改行も対象
    assert _normalize_command("/bin/rm -rf /") == "rm -fr /"      # (b) 絶対パス接頭辞剥がし
    assert _normalize_command("/usr/local/bin/mkfs.ext4 /dev/sda") == "mkfs.ext4 /dev/sda"


def test_normalize_flag_order_canonicalized():
    """正規化 (c): 連結ショートフラグの文字順を畳む（`-rf`/`-fr`/`-Rf` を同一化）。長い `--force` は不変。"""
    assert _normalize_command("rm -rf /") == _normalize_command("rm -fr /")
    assert _normalize_command("rm -Rf /") == _normalize_command("rm -rf /")   # 小文字化も込み
    assert _normalize_command("git push --force") == "git push --force"       # 長フラグは対象外


def test_safety_deny_matches_despite_whitespace_padding():
    """バイパス封じ: `rm   -rf   /`（空白水増し）でも always_deny が発火する（§3.3 (0)）。"""
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)        # 分類器 Tier1 でも安全性チェックが優先
    pending = PendingApproval(tool_name="Bash", tool_input={"command": "rm   -rf   /"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-ws"))

    assert result.handler == "safety_deny"
    assert not result.allow
    assert "always_deny" in result.reason


def test_safety_deny_matches_despite_abs_path_launch():
    """バイパス封じ: `/bin/rm -rf /`（絶対パス起動）でも always_deny が発火する（§3.3 (0)）。

    素の語境界照合では `rm` 直前の `/` が境界と認められず素通りしていた欠陥の回帰。
    """
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pending = PendingApproval(tool_name="Bash", tool_input={"command": "/bin/rm -rf /"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-abs"))

    assert result.handler == "safety_deny"
    assert not result.allow
    assert "always_deny" in result.reason


def test_normalize_strips_legacy_directive_prefix():
    """正規化 (b) 前段: レガシー指示接頭辞 Run:/Execute:/Write to: を剥がし先頭を実行ファイルにする。"""
    assert _normalize_command("Run: /bin/rm -rf /") == "rm -fr /"
    assert _normalize_command("Execute:   /usr/bin/mkfs /dev/sda") == "mkfs /dev/sda"


def test_safety_deny_matches_legacy_scrape_abs_path():
    """バイパス封じ: interactive scrape 経路の `Run: /bin/rm -rf /` でも always_deny が発火する（§3.3 (0) (b)）。"""
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    # interactive scrape は tool_name="" で command に "Run: ..." を載せる（to_pending 相当）。
    pending = PendingApproval(tool_name="", tool_input={"command": "Run: /bin/rm -rf /"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-legacy"))

    assert result.handler == "safety_deny"
    assert not result.allow


def test_safety_deny_matches_despite_flag_reorder():
    """バイパス封じ (c): `rm -fr /`（フラグ順入替）でも always_deny が発火する（§3.3 (0)）。"""
    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pending = PendingApproval(tool_name="Bash", tool_input={"command": "rm -fr /"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-flag"))

    assert result.handler == "safety_deny"
    assert not result.allow


def test_safety_escalate_matches_case_insensitively():
    """バイパス封じ (d): `SUDO`（大文字）でも always_escalate が発火し Tier3 へ倒れる（§3.3 (0)）。"""
    captured = {}

    class FakeTier3:
        async def handle(self, pending, ctx):
            captured.update(ctx)
            return Decision(allow=True, handler="tier3_human")

    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)
    pipeline.handlers[3] = FakeTier3()
    pending = PendingApproval(tool_name="Bash", tool_input={"command": "SUDO reboot"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-case"))

    assert result.handler == "tier3_human"                # 大文字でも escalate
    assert "always_escalate" in captured["risk_reason"]


# ── チェックは無効化されない（コード固定デフォルト＋fail-closed・§3.3 (0)） ──

def test_defaults_always_present_and_yaml_only_augments():
    """コード固定デフォルトは常に静的安全性チェックへ載り、yaml は和集合で追加のみ（単体）。"""
    # _union: デフォルトは先頭に保持され、yaml 未知規則のみ後置・重複排除される。
    assert _union(["a", "b"], None) == ["a", "b"]              # yaml 欠落でもデフォルト不変
    assert _union(["a", "b"], ["b", "c"]) == ["a", "b", "c"]   # 既知は重複させず未知のみ追加
    assert _union(["a", "b"], "sudo") == ["a", "b"]            # 非リスト(文字列)は無視（文字反復させない）
    # 実 pipeline: デフォルト規則が必ず含まれる（yaml が空でも消えない）。
    pipeline = ApprovalPipeline(CONFIG, slack_notifier=FakeNotifier())
    for rule in DEFAULT_ALWAYS_DENY:
        assert rule in pipeline.always_deny
    for rule in DEFAULT_ALWAYS_ESCALATE:
        assert rule in pipeline.always_escalate


def test_degraded_escalates_unmatched_to_human_not_llm():
    """fail-closed: yaml ロード失敗（degraded）時、チェック未該当を LLM 自動 allow でなく Tier3 へ。

    分類器を Tier1（自動承認）に固定しても、degraded では LLM 判定へ回さず人間承認へ倒す。
    """
    captured = {}

    class FakeTier3:
        async def handle(self, pending, ctx):
            captured.update(ctx)
            return Decision(allow=True, handler="tier3_human")

    notifier = FakeNotifier()
    pipeline = _pipeline(1, notifier)        # 分類器 Tier1 固定（LLM は自動承認を返す想定）
    pipeline.degraded = True                 # yaml ロード失敗相当
    pipeline.handlers[3] = FakeTier3()
    # デフォルトの deny/escalate いずれにも該当しない良性コマンド。
    pending = PendingApproval(tool_name="Bash", tool_input={"command": "git status"})

    result = asyncio.run(pipeline.decide(pending, instance_id="test-degraded"))

    assert result.handler == "tier3_human"               # LLM(Tier1) でなく人間承認へ
    assert "degraded" in result.reason
    assert "degraded" in captured["risk_reason"]
    assert pipeline.logger.entries[-1]["tier"] == 3
