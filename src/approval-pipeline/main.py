"""承認パイプライン本体 — 静的安全性チェック → Tier 分類 → ハンドラ実行 → 監査ログ。

フロー（設計書 §3.3 / §3.4）:
  (0) 静的安全性チェック（決定論・Tier 判定前・最優先）— pipeline.yaml の safety リストと機械的に照合
  (1) スコープ判定 / (2) 三段階リスク判定 — ya-ta(LLM) が grey zone を判定

構築手順書: docs/procedures/08-approval-pipeline.md Step 6（パイプライン統合）
"""

import asyncio
import logging
import os
import re
import time

import yaml

from approval_types import Decision, operation_str
from interceptor import to_pending
from classifier import RiskClassifier
from tier1_handler import Tier1Handler
from tier2_handler import Tier2Handler
from tier3_handler import Tier3Handler
from audit_logger import AuditLogger

logger = logging.getLogger("sa-ru.approval")

# pipeline.yaml は本モジュールと同じツリーへ配備される（pyinfra files.sync、構築手順書 08）。
# 自モジュール相対で解決すれば実行時 cwd に依存せずロードできる（配備先＝
# /opt/taka-ma/sa-ru/approval-pipeline/config/pipeline.yaml）。
_PIPELINE_YAML = os.path.join(os.path.dirname(__file__), "config", "pipeline.yaml")

# コード固定のデフォルト静的安全性チェック（設計書 §3.3 (0)「チェックは無効化されない」）。
# pipeline.yaml が欠落・空・破損でもこれらは常に適用される絶対防壁。yaml の safety は
# これに和集合で「追加」されるだけで、内蔵規則の置換・削除・弱体化はできない。
DEFAULT_ALWAYS_DENY = ["rm -rf /", "mkfs", "dd if=/dev/zero", ":(){:|:&};:"]
DEFAULT_ALWAYS_ESCALATE = ["sudo", "deploy", "production"]

# 照合前正規化で剥がす実行ファイル絶対パス接頭辞（設計書 §3.3 (0)「照合の正規化」(b)）。
# `/bin/rm -rf /` を `rm -rf /` として捉え、絶対パス起動での素通りを塞ぐ。
_EXEC_PATH_PREFIXES = ("/usr/local/bin/", "/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")

# レガシー interactive scrape が付ける指示接頭辞（extract_command の Run:/Execute:/Write to:）。
# 先頭がこれだと (b) の絶対パス剥がしが実行ファイルに届かないため、(b) の前段で除去する
# （設計書 §3.3 (0)「照合の正規化」(b) 補足）。"Write to:" は 2 語なので順に長い方を先に試す。
_DIRECTIVE_PREFIXES = ("Write to:", "Run:", "Execute:")


def _normalize_command(command: str) -> str:
    """静的安全性チェックの照合前正規化（設計書 §3.3 (0)「照合の正規化」）。

    素の command 文字列は空白の水増し（`rm   -rf  /`）・絶対パス起動（`/bin/rm -rf /`・
    `Run: /bin/rm -rf /`）・フラグ順入替（`rm -fr /`）で規則を素通りできる。照合前に以下を施す:
      (a) 連続する空白（タブ・改行含む）を単一スペースへ畳み前後を除去、
      (b) 先頭のレガシー指示接頭辞（Run:/Execute:/Write to:）を除去したうえで、先頭トークンの
          実行ファイル絶対パス接頭辞を剥いで basename に落とす、
      (c) 連結ショートフラグ（1 ダッシュ＋複数英字）を小文字化＋文字順ソートで正規化し
          `-rf` と `-fr` を同一化する。
    大小文字無視（(d) IGNORECASE）は照合側 `_match_rule` の re.search で担う。操作文字列と
    規則の双方を同一手順で通してから語境界照合する（片側だけ正規化すると照合が崩れる）。
    """
    collapsed = re.sub(r"\s+", " ", command).strip()
    if not collapsed:
        return collapsed
    # (b) 前段: 先頭の指示接頭辞（Run:/Execute:/Write to:）を剥がし、先頭トークンを実行ファイルにする。
    for directive in _DIRECTIVE_PREFIXES:
        if collapsed.startswith(directive):
            collapsed = collapsed[len(directive):].strip()
            break
    if not collapsed:
        return collapsed
    tokens = collapsed.split(" ")
    # (b) 先頭トークン（実行ファイル）の絶対パス接頭辞を basename に落とす。
    for prefix in _EXEC_PATH_PREFIXES:
        if tokens[0].startswith(prefix):
            tokens[0] = tokens[0][len(prefix):]
            break
    # (c) 連結ショートフラグの文字順を正規化（`-rf`→`-fr`）。長い `--force` 等は対象外
    #     （2 文字目が英字でないため fullmatch しない）。小文字化してから両側同一に畳む。
    tokens = [
        ("-" + "".join(sorted(tok[1:].lower()))) if re.fullmatch(r"-[A-Za-z]{2,}", tok) else tok
        for tok in tokens
    ]
    return " ".join(tokens)


def _union(defaults: list[str], extra) -> list[str]:
    """コード固定デフォルト規則に yaml 由来の規則を和集合で追加する（順序保持・重複排除）。

    設計書 §3.3 (0)「チェックは無効化されない」: yaml は拡張のみで、デフォルトの置換・削除は
    できない。デフォルトを必ず先頭に置き、yaml 側の未知規則だけを後ろへ足す（`extra` が
    None / 非リストでもデフォルトは失われない）。
    """
    # extra は list を期待するが、誤設定（yaml で `always_deny: "sudo"` 等の文字列）だと
    # for が文字単位で反復し "s"/"u"/… が規則化され過剰 deny を招く。list 以外は無視して
    # デフォルトのみ返す（誤設定でデフォルトが壊れも過剰化もしないフォールセーフ）。
    if not isinstance(extra, list):
        extra = []
    merged = list(defaults)
    seen = set(merged)
    for rule in extra:
        if rule and rule not in seen:
            merged.append(rule)
            seen.add(rule)
    return merged


class ApprovalPipeline:
    """承認パイプライン本体。1 つの承認プロンプトを安全性チェック → Tier 分類 → ハンドラ実行で裁く。

    worker の y/n プロンプトを受け、(0) 決定論の静的安全性チェックで機械照合し、漏れたものを
    (1)(2) ya-ta(LLM) の Tier 分類に回して Tier 1/2/3 ハンドラへ振り分ける。全判定は監査
    ログに残す（設計書 §3.3 / §3.4 / §3.5）。
    """

    def __init__(self, config: dict, slack_notifier=None, ssh_host: str = "mbp"):
        """分類器・各 Tier ハンドラ・安全性チェックリスト・監査ロガーを組み立てる。

        Args:
            config: ya-ta（RiskClassifier）が参照するモデル設定等を含む dict。
            slack_notifier: Tier 3 の人間承認リクエストを Slack へ送る送信器。
            ssh_host: Tier 2 の qu-e 審査の SSH 接続先（qu-e は MBP 上の別プロセス・§8.8）。
        """
        # RiskClassifier は ya-ta を in-process 呼出（config から ya-ta モデルを参照）
        self.classifier = RiskClassifier(config)
        # Tier2/Tier3 の運用値（審査タイムアウト・人間承認タイムアウト・ポーリング間隔）は
        # sa-ru.yaml の approval ブロックを唯一の源にする（マージ済み config で受領。
        # コード側に既定値を置かず、欠落は構築時に即 KeyError で落として診断位置を揃える）。
        approval_conf = config["approval"]
        self.handlers = {
            1: Tier1Handler(),
            2: Tier2Handler(ssh_host=ssh_host,    # qu-e は SSH（§8.8）
                            timeout_sec=approval_conf["tier2_timeout_sec"]),
            3: Tier3Handler(slack_notifier,
                            timeout_sec=approval_conf["tier3_timeout_sec"],
                            poll_interval_sec=approval_conf["poll_interval_sec"]),
        }

        # pipeline.yaml（SSOT）をロードし、監査ログ出力先と安全性チェックリストを取得する（設計 §3.3 (0)/§3.4）。
        # loaded=False はロード失敗（不在・破損・権限）を表し、degraded（fail-closed）へ落とす。
        pcfg, loaded = self._load_pipeline_config()
        audit_path = pcfg.get("audit", {}).get("log_path")
        # log_path が yaml にあればそれを SSOT として使い、無ければ AuditLogger の既定にフォールバック。
        self.logger = AuditLogger(log_path=audit_path) if audit_path else AuditLogger()
        safety = pcfg.get("safety", {})
        # 静的安全性チェック（決定論の最終防壁・§3.3 (0)）。コード固定デフォルトに yaml を
        # 和集合で「追加」する（yaml は拡張のみ・内蔵規則の弱体化不可）。yaml 欠落・空でも
        # デフォルトは常に効くため、チェックが無効化されることはない。
        self.always_deny = _union(DEFAULT_ALWAYS_DENY, safety.get("always_deny"))
        self.always_escalate = _union(DEFAULT_ALWAYS_ESCALATE, safety.get("always_escalate_to_human"))
        # degraded: yaml ロード失敗時。決定論チェックはデフォルトで継続しつつ、いずれの
        # チェックにもスコープにも該当しない操作を LLM 自動 allow へ倒さず Tier 3 へ escalate する。
        self.degraded = not loaded

    @staticmethod
    def _load_pipeline_config() -> tuple[dict, bool]:
        """承認パイプライン設定 pipeline.yaml を読み込む（SSOT）。

        Returns: `(config, loaded)`。`loaded=False` はロード失敗（不在・破損・権限）を表す。
        配備済みファイルが無い/壊れている場合でも sa-ru（orchestrator）全体を落とさないよう
        例外を握り、空設定＋`loaded=False` を返す。呼び出し側はこれを degraded（fail-closed）
        として扱い、決定論の静的安全性チェックはコード固定デフォルトで継続する（§3.3 (0)）。
        欠落・破損は運用ログで気付けるよう error を出す。
        """
        try:
            with open(_PIPELINE_YAML) as f:
                return (yaml.safe_load(f) or {}), True
        except (OSError, yaml.YAMLError):
            # OSError 全般（FileNotFoundError / PermissionError / IsADirectoryError 等）を握る。
            # docstring の「sa-ru 全体を落とさない」を満たすため FileNotFoundError 限定にしない。
            logger.error(
                "pipeline.yaml をロードできません（degraded=fail-closed へ移行・"
                "静的安全性チェックはコード固定デフォルトで継続・監査ログは既定パス）: %s",
                _PIPELINE_YAML,
            )
            return {}, False

    async def decide(self, pending: 'PendingApproval', *, instance_id: str = "",
                     team_id: str | None = None, channel: str | None = None,
                     task_id: str = "", thread_ts: str | None = None,
                     deadline: float | None = None) -> 'Decision':
        """1 件のツール実行要求を裁定し、Decision（allow / handler / reason）を返す。CLI 非依存。

        裁定順は固定: まず決定論の安全性チェック（always_deny → always_escalate）を最優先で照合し、
        漏れたものだけ ya-ta の Tier 分類に回す。Tier 2 が deny を返したら Tier 3 へ繋ぐ。
        いずれの経路でも最後に監査ログを残す。**worker CLI の存在を知らず、allow/deny の物理的な
        伝達（y/n 送信・フック応答）には一切関与しない**（伝達は実行アダプタの責務）。

        Args:
            pending: 構造化された承認要求（tool_name / tool_input / context）。
            instance_id: 判定元の worker インスタンス識別子（監査・承認リクエスト用）。
            team_id / channel: Tier 3 承認リクエストを送信元ワークスペースへ返すための宛先（§8.10）。
            task_id: 紐付くタスク ID（承認リクエストの突き合わせ用）。
            thread_ts: 会話スレッドの起点 ts（Tier 3 承認リクエストを同一スレッドへ返すため）。
            deadline: decide 全体の締切（time.monotonic() 基準）。デーモン経由で前段（分類・
                qu-e 審査）が時間を消費しても Tier3 の人間待ちが外側タイムアウトより先に確定
                するよう、Tier3 ハンドラのポーリング上限をこの締切の内側に収める。None なら
                従来どおり固定 300 秒（interactive 経路など外側タイムアウトが無い呼び出し）。
        """
        start = time.monotonic()
        # 安全性チェック照合・分類器・Tier3 提示に使う操作文字列（旧 scrape と同じ書式）。
        command = operation_str(pending)

        # (0) 静的安全性チェック（決定論・Tier 判定前・最優先、§3.3 (0) / §3.4 の SF ノード）。
        #     LLM 判定の前段で機械的に照合する。ya-ta(LLM) が誤った/乗っ取られた場合でも
        #     破壊的操作を通さない最終防壁のため、意図的に LLM を介さない。
        deny_rule = self._match_rule(command, self.always_deny)
        if deny_rule:
            decision = Decision(allow=False, handler="safety_deny",
                                reason=f"always_deny: {deny_rule}")
            self._audit(decision, instance_id, command, tier=0, start=start)
            return decision

        # interactive(pty) フェイルセーフ (1): 承認対象を stdout スクレイプで復元できず判定不能
        # （command 空）なら、無害文字列として自動判定に載せず人間承認（Tier3）へ直行する
        # （旧実装は "unknown" を無害扱いで Tier1 自動承認し得た・設計 §3.3 (3)）。context 全体を
        # 承認リクエストに添えて人間が実操作を確認する。always_deny の後に置くのは、判定可能な
        # 危険操作は先に決定論で拒否するため（判定不能な command="" は deny 規則に一致しない）。
        if getattr(pending, "source", "headless") == "interactive" and not command:
            ctx = {
                "instance_id": instance_id,
                "risk_reason": "interactive_undeterminable: 承認対象を stdout から復元できず判定不能",
                "team_id": team_id,
                "channel": channel,
                "task_id": task_id,
                "thread_ts": thread_ts,
                "decide_deadline": deadline,
            }
            decision = await self.handlers[3].handle(pending, ctx)
            decision.reason = ctx["risk_reason"] + (
                f" ({decision.reason})" if decision.reason else "")
            self._audit(decision, instance_id, command, tier=3, start=start)
            return decision

        escalate_rule = self._match_rule(command, self.always_escalate)
        if escalate_rule:
            # スコープ・Tier 判定をスキップして人間承認（Tier 3）へ直行する。
            ctx = {
                "instance_id": instance_id,
                "risk_reason": f"always_escalate: {escalate_rule}",
                "team_id": team_id,
                "channel": channel,
                "task_id": task_id,
                "thread_ts": thread_ts,
                "decide_deadline": deadline,
            }
            decision = await self.handlers[3].handle(pending, ctx)
            # 安全性チェック由来であることを監査ログに残す（always_deny と対称・§3.5 SSOT）。
            # Tier3Handler の戻りは reason 空のことがあるため、ここで補う。
            decision.reason = ctx["risk_reason"] + (
                f" ({decision.reason})" if decision.reason else "")
            self._audit(decision, instance_id, command, tier=3, start=start)
            return decision

        # degraded（fail-closed・§3.3 (0)「チェックは無効化されない」）: pipeline.yaml の
        # ロードに失敗している場合、決定論チェック（デフォルト）を通過した残りを LLM 判定へ
        # 回すと「設定不備 → LLM 自動 allow」に倒れる（fail-open）。これを防ぎ、未該当操作を
        # 人間承認（Tier 3）へ直行させる。
        if self.degraded:
            ctx = {
                "instance_id": instance_id,
                "risk_reason": "degraded: pipeline.yaml ロード失敗のため人間承認へ fail-closed",
                "team_id": team_id,
                "channel": channel,
                "task_id": task_id,
                "thread_ts": thread_ts,
                "decide_deadline": deadline,
            }
            decision = await self.handlers[3].handle(pending, ctx)
            decision.reason = ctx["risk_reason"] + (
                f" ({decision.reason})" if decision.reason else "")
            self._audit(decision, instance_id, command, tier=3, start=start)
            return decision

        # (1)(2) リスク分類（tier ＋ reason）。reason は Tier 3 承認リクエストの risk_reason に使う。
        classification = await self.classifier.classify(pending)
        # ya-ta(ollama) はプロンプト次第で "tier":"3" 等の文字列/実数を返しうる。handlers は int キーの
        # ため int 化し、欠落・不正・未知 tier はフォールセーフに Tier3（人間承認）へ寄せる（§8.4）。
        try:
            tier = int(classification["tier"])
        except (KeyError, TypeError, ValueError):
            tier = 3
        if tier not in self.handlers:
            tier = 3
        # interactive(pty) フェイルセーフ (2): 単一スクレイプ行を根拠に Tier1 自動承認しない。
        # 最低 qu-e 審査（Tier2）へ引き上げる。qu-e は 1 行でなく直近 stdout 全体（context）を
        # 読むため、承認要求直前に差し込まれた偽の提示行（なりすまし）に依存しない再審査ができる
        # （設計 §3.3 (3)）。headless（tool_input が権威的）は対象外。
        if getattr(pending, "source", "headless") == "interactive" and tier == 1:
            tier = 2
        risk_reason = classification.get("reason", "")

        # Tier 3（人間承認）に必要なコンテキスト。Tier 1/2 は ctx を無視する。
        # team_id / channel は承認リクエストを送信元ワークスペースへ返すため（§8.10）。
        ctx = {
            "instance_id": instance_id,
            "risk_reason": risk_reason,
            "team_id": team_id,
            "channel": channel,
            "task_id": task_id,
            "thread_ts": thread_ts,
            "decide_deadline": deadline,
        }

        # 対応するハンドラで処理
        decision = await self.handlers[tier].handle(pending, ctx)

        # Tier 2 deny → Tier 3 エスカレート（qu-e の判定理由を risk_reason に引き継ぐ）
        if decision.escalate:
            tier = 3
            ctx["risk_reason"] = decision.reason or risk_reason
            decision = await self.handlers[3].handle(pending, ctx)

        self._audit(decision, instance_id, command, tier=tier, start=start)
        return decision

    async def process(self, prompt: 'InterceptedPrompt', pty_wrapper, instance_id: str,
                      *, team_id: str | None = None, channel: str | None = None,
                      task_id: str = "", thread_ts: str | None = None) -> 'Decision':
        """interactive(pty) アダプタ: 検出プロンプトを裁定し、結果を PTY に y/n で伝達する。

        中核 decide() は CLI 非依存で allow/deny を返すだけなので、pexpect 経由の worker
        （agy 対話・将来 Codex 等の汎用対話 CLI）向けに、ここで PendingApproval への変換と
        y/n キー送信の伝達を担う（このメソッド＝interactive アダプタの変換層）。
        """
        pending = to_pending(prompt)
        decision = await self.decide(
            pending, instance_id=instance_id, team_id=team_id,
            channel=channel, task_id=task_id, thread_ts=thread_ts)
        if decision.allow:
            pty_wrapper.approve(prompt.prompt_type)
        else:
            pty_wrapper.deny(prompt.prompt_type)
        return decision

    def _audit(self, decision: 'Decision', instance_id: str, command: str, *, tier: int, start: float):
        """1 回の判定結果を監査ログ（jsonl）へ 1 行追記する（§3.5）。"""
        duration_ms = int((time.monotonic() - start) * 1000)
        self.logger.log({
            "instance_id": instance_id,
            "command": command,
            "tier": tier,
            "handler": decision.handler,
            "decision": "approved" if decision.allow else "denied",
            "reason": decision.reason,
            "duration_ms": duration_ms,
        })

    @staticmethod
    def _match_rule(command: str, rules: list[str]) -> str | None:
        """command が safety リストの規則に一致すれば、その規則（元の字面）を返す（無ければ None）。

        照合前に command と規則の双方を `_normalize_command` で正規化し（空白畳み込み・先頭
        トークンの絶対パス接頭辞剥がし・連結フラグの文字順正規化・§3.3 (0)「照合の正規化」
        (a)(b)(c)）、`rm   -rf  /`・`/bin/rm -rf /`・`rm -fr /` の自明なバイパスを塞ぐ。
        大小文字の違い（(d)）は re.IGNORECASE で吸収する。
        素朴な部分一致は正規コマンドを誤拒否する（`rm -rf /` が `rm -rf /tmp/build` に、
        `sudo` が `cat /etc/sudoers` に一致してしまう）。規則の前後が単語/パス構成文字
        （`\\w` ・ `/` ・ `-`）でない位置のみ一致とみなす語境界照合にする。
        例: `rm -rf /` は `rm -rf /`（末尾）には一致し `rm -rf /tmp...` には一致しない。
        """
        norm_command = _normalize_command(command)
        for rule in rules:
            if not rule:
                continue
            norm_rule = _normalize_command(rule)
            if re.search(rf"(?<![\w/-]){re.escape(norm_rule)}(?![\w/-])",
                         norm_command, re.IGNORECASE):
                return rule
        return None
