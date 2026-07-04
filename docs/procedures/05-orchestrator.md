# 05. sa-ru: Orchestrator

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (sa-ru を配備)](#step-2-pyinfra実行-sa-ru-を配備)
  - [Step 3: PyInfra 実行後 （手動実行）](#step-3-pyinfra-実行後-手動実行)
- [動作確認](#動作確認)
  - [1. launchd サービス稼働確認](#1-launchd-サービス稼働確認)
  - [2. タスクキュー動作確認（light タスク）](#2-タスクキュー動作確認light-タスク)
  - [3. Slack 通知確認](#3-slack-通知確認)
  - [4. heavy タスク並行実行](#4-heavy-タスク並行実行)
  - [5. cross-review 動作検証](#5-cross-review-動作検証)
  - [6. 配列フォールバック動作検証](#6-配列フォールバック動作検証)
  - [7. `max_fallback_attempts` 制限の検証](#7-max_fallback_attempts-制限の検証)
  - [8. file_audit アラート受信 → Slack 表示](#8-file_audit-アラート受信--slack-表示)
- [検証項目](#検証項目)
- [主要 API（実装本体への索引）](#主要-api実装本体への索引)

## 概要

sa-ru は Gemma 4 12B（マルチモーダル）をベースとしたオーケストレーターで、Mac mini 上に常駐する。
Slack からの指示をファイルベースタスクキューで受け取り、ya-ta（ライブラリ）でタスク分類・リスク判定を行い、
MBP 上の適切なモデル（Claude Code / Antigravity CLI / Gemma 4 31B）にルーティングする。
対話型 worker CLI（Claude Code / Antigravity CLI 等）は汎用 PTY ラッパー（`WorkerPtyWrapper`）で stdin/stdout を制御し、y/n 承認パイプラインを統合する。経路選択は `ya-ta.yaml` の `models.<name>.methods` 配列と用途（対話 / 単発 / cross-review）から動的に決まる。
#### アーキテクチャ

```
Slack Bot ──[タスクファイル]──→ sa-ru ──[Python import]──→ ya-ta
                                  │
                                  ├── SSH+PTY (WorkerPtyWrapper) → Claude Code (heavy 主軸)
                                  ├── SSH+PTY (WorkerPtyWrapper) / SSH+subprocess → Antigravity CLI (対話 heavy / 高度なマルチモーダル解析・cross-review・フォールバック)
                                  ├── SSH+subprocess → Gemma 4 31B (light)
                                  ├── SSH+subprocess → Qwen3.6-35B-A3B (qu-e)
                                  └── slack-sdk → Slack (通知・承認リクエスト)
```

## 実行場所

Mac mini M4 Pro (64GB)

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy が完了している
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通が確立している
- Slack Bot トークン（`SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET`）を取得済（[03-slack-bot.md](03-slack-bot.md) で発行）

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

#### 1-1. Slack 認証情報の配置

`/opt/taka-ma/config/.env` に Slack 認証情報を配置する（[03-slack-bot.md](03-slack-bot.md) で取得した値を使用）。Step 2 の PyInfra 実行で launchd が sa-ru を起動した時点で読み込まれるため、**PyInfra 実行前に配置する**。

```bash
ssh mac-mini "cat >> /opt/taka-ma/config/.env" << 'EOF'
SLACK_BOT_TOKEN=<xoxb-...>
SLACK_SIGNING_SECRET=<...>
EOF
ssh mac-mini "chmod 600 /opt/taka-ma/config/.env"
```

### Step 2: PyInfra実行 (sa-ru を配備)

Mac mini 上で**ローカル実行**する（`@local`）。

```bash
pyinfra -y @local pyinfra/deploys/orchestrator.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mac-mini ...` と記載していたが、これはホストを定義した**インベントリ**が前提（本リポジトリに未整備）で、実行すると `mac-mini is neither an inventory file, ...` で失敗する（01 の NOTE と同一の欠陥）。本手順は Mac mini ローカルの `@local` 実行に統一する。

[`pyinfra/deploys/orchestrator.py`](../../pyinfra/deploys/orchestrator.py) が下記を冪等に実行する（番号は論理フロー順、pyinfra スクリプトの実行順とは別。冪等性は担保済）:

| # | 内容 | 実装 |
|---|------|------|
| 1 | Python パッケージ導入（`slack-sdk` / `python-dotenv` / `pexpect` / `pyyaml`） | `pip.packages` |
| 2 | `ollama pull gemma4:12b`（オーケストレーター推論モデル・マルチモーダル） | `server.shell` |
| 3 | [`src/orchestrator/`](../../src/orchestrator/) を `/opt/taka-ma/sa-ru/orchestrator/` に sync | `files.sync` |
| 4 | データディレクトリ作成（`/opt/taka-ma/data/{tasks,tasks/done,approvals}`） | `files.directory` |
| 5 | 設定ファイル `sa-ru.yaml` をテンプレートから生成・配置 | `files.template` |
| 6 | ya-ta（`ai_gateway` パッケージ）の Python import 動作確認 | `server.shell` |
| 7 | launchd Agent 登録（`com.taka-ma.sa-ru.plist`、`bootout` → `bootstrap`） | `files.template` + `server.shell` |

> **NOTE**: sa-ru.yaml の設定項目は [`src/orchestrator/config/sa-ru.yaml`](../../src/orchestrator/config/sa-ru.yaml) 本体を参照（設計意図は設計書 §2.1 / §8.3 / §10）。

### Step 3: PyInfra 実行後 （手動実行）

`launchd` の `RunAtLoad` + `KeepAlive` により自動起動・自動再起動するため、手動実行は不要。

## 動作確認

### 1. launchd サービス稼働確認

```bash
ssh mac-mini "launchctl list | grep sa-ru"
ssh mac-mini "tail -20 /opt/taka-ma/logs/sa-ru.log"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `launchctl list` の出力 | `com.taka-ma.sa-ru` 行があり、PID が数値、Status が `0` | サービス未登録、PID 欄が `-`、Status が非ゼロ |
| `sa-ru.log` の出力 | 起動メッセージが記録されている、Python Traceback なし | 起動ログ無し、Traceback、エラーメッセージ |

### 2. タスクキュー動作確認（light タスク）

```bash
# テスト用タスクファイル投入
ssh mac-mini "cat > /opt/taka-ma/data/tasks/\$(date +%s)_test-light.json" << 'EOF'
{
  "task_id": "test-light-001",
  "status": "init",
  "source": "manual",
  "command": "What is 2+2? Answer in one word.",
  "channel_id": "",
  "created_at": "2026-04-10T00:00:00+09:00",
  "updated_at": "2026-04-10T00:00:00+09:00"
}
EOF

sleep 10
ssh mac-mini "tail -30 /opt/taka-ma/logs/sa-ru.log"
ssh mac-mini "cat /opt/taka-ma/data/tasks/done/*test-light*.json"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| ログのステータス遷移 | `accepted` → `in_progress` → `completed` の順に記録される | 遷移が欠落、`failed` で終わる、in_progress で停止 |
| タスクファイル | `done/` に移動し、`status: completed`、`result` に応答が入る | tasks/ に残ったまま、status が completed 以外、result が空 |

### 3. Slack 通知確認

`channel_id` を指定したタスクを投入し、Slack `#taka-ma` の通知を観察する。

| 観点 | 成功 | エラー |
|------|------|--------|
| タスク分類通知 | `#taka-ma` に「タスク分類: light」メッセージが届く | 通知が届かない / 別チャンネルに届く |
| タスク完了通知 | 完了メッセージが届き、`result` 相当のテキストが含まれる | 通知が届かない、結果テキストが欠落 |

### 4. heavy タスク並行実行

2 つの heavy タスクを同時に投入する（詳細は [Appendix_04-orchestration-flow.md](../design/Appendix_04-orchestration-flow.md) を参照）。

| 観点 | 成功 | エラー |
|------|------|--------|
| 並行性 | 両タスクの `in_progress` 期間が時間的に重なる | 1 件目が completed になってから 2 件目が in_progress に入る（直列実行） |
| 完了 | 両方とも `completed` で終わる | 片方または両方が failed |

### 5. cross-review 動作検証

`:opus :gemini` 末尾指定のタスクを投入する。

| 観点 | 成功 | エラー |
|------|------|--------|
| 並行投入 | opus と gemini の両方が同時実行される | 一方のみ実行、または直列実行 |
| ya-ta 統合 | DeepSeek-R1 32B による統合結果が生成される | 統合されず両出力が別々に残る |
| Slack 通知 | 統合済み 1 メッセージで通知される | 2 メッセージに分かれる、または通知が欠落 |

### 6. 配列フォールバック動作検証

`ya-ta.yaml` の `routing.category_defaults.heavy` 先頭（[0]）を存在しないモデル名に書き換えてタスクを投入する。

| 観点 | 成功 | エラー |
|------|------|--------|
| 次候補での実行 | 配列 [1] のモデルでタスクが実行され、`completed` になる | 失敗のまま終わる、[0] でリトライし続ける |
| Slack 通知 | fallback が発生した旨の通知が届く | 通知無し |

### 7. `max_fallback_attempts` 制限の検証

`fallback.max_fallback_attempts: 0` を設定し、先頭モデル障害を発生させる。

| 観点 | 成功 | エラー |
|------|------|--------|
| 次候補の試行 | 試行されない（`max_fallback_attempts: 0` を尊重） | 次候補が実行されてしまう |
| 失敗通知 | `_notify_failure` 経由で `failed` 通知が Slack に届く | 通知無し、または completed 通知 |

### 8. file_audit アラート受信 → Slack 表示

テスト用 alert ファイルを `/opt/taka-ma/data/file-audit-alerts/` に配置し、qu-e からの SSH push を擬似的に再現する（詳細仕様は [08-approval-pipeline.md](08-approval-pipeline.md) §8.12）。

| 観点 | 成功 | エラー |
|------|------|--------|
| `FileAuditHandler` 受信 | `sa-ru.log` にアラート受信ログが記録される | 処理されない、ログ無し |
| Slack 通知 | Approve / Reject ボタン付きメッセージが Slack に送信される | 通知無し、ボタンが欠落 |
| ファイル移動 | アラートファイルが `done/` 配下に移動する | 元の配置のまま残る |

### 9. 会話 → 着手確認 → 実行

会話キューに発話を投入し、会話返信 → 着手確認 → 確定タスク生成までを確認する。

```bash
# 会話メッセージを投入（status=init）
ssh mac-mini "cat > /opt/taka-ma/data/conversations/\$(date +%s)_test-conv.json" << 'EOF'
{
  "message_id": "test-conv-001",
  "conversation_id": "T0:C0:U0",
  "status": "init",
  "source": "slack_dm",
  "text": "2+2 を計算するスクリプトを書いて、と一言で頼みたい。これで実行して。",
  "force_ready": false,
  "user_id": "U0", "team_id": "", "channel_id": "",
  "thread_ts": null,
  "created_at": "2026-06-11T00:00:00+00:00"
}
EOF
sleep 8
# ready=true なら exec-confirmations/ に pending レコードが出る
ssh mac-mini "ls /opt/taka-ma/data/exec-confirmations/"
# 着手を擬似（confirmed に書き換え）→ tasks/ に確定タスクが生成される
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 会話継続（曖昧な発話） | `ready=false` で Slack に会話返信、tasks/ にタスクは作られない | いきなりタスク化される |
| 着手確認の提示 | 意図が固まると exec-confirmations/ に pending レコード + Slack に着手/やり直すボタン | レコード無し、ボタン無し |
| confirmed → 実行 | レコードを `status=confirmed` にすると tasks/ に `source=conversation`・`command=要約` の init タスクが生成され、検証 2 と同様に completed まで進む | 確定タスクが作られない |
| timeout | pending を 5 分放置で自動 timeout、実行されない | 放置で実行される / レコードが残り続ける |

> **NOTE**: PTY ラッパーの y/n 検知・承認パイプラインの統合検証は [08-approval-pipeline.md](08-approval-pipeline.md) で実施する。
> **NOTE**: サービスの緊急停止 / 復旧 / 状態確認は [`docs/operations/u-zu/slack-bot.md`](../operations/u-zu/slack-bot.md) の「サービス管理」セクションを参照。

## 検証項目

> **検証概要**: sa-ru が Mac mini 上で常駐し、タスクファイル → ya-ta 分類 → MBP の各 worker LLM へのルーティング → Slack 通知の一連のフローが期待通り動作することを確認する。

| # | 検証項目 |
|---|---------|
| 1 | `ollama run gemma4:12b` で推論が動作する |
| 2 | `/opt/taka-ma/data/{tasks,tasks/done,approvals,conversations,exec-confirmations}` が存在する（会話/着手確認 dir は起動時に自動作成） |
| 3 | 汎用 PTY ラッパー（`WorkerPtyWrapper`）が対話型 worker CLI（Claude Code / Antigravity CLI 等）の y/n プロンプトを検知できる（実機検証は 08 で実施） |
| 4 | `run_ssh_command()` で MBP 上の Gemma 4 31B が実行できる |
| 5 | `run_ssh_command()` で MBP 上の Antigravity CLI が実行できる |
| 6 | ai-gateway の `decomposer` / `classifier` / `risk_classifier` が import できる |
| 7 | Slack に通知メッセージが送信される（slack-sdk 経由） |
| 8 | タスクファイル投入 → `accepted` → `in_progress` → `completed` の遷移 |
| 9 | `confidence < 0.8` の light タスクが heavy に強制ルーティングされる |
| 10 | light 失敗時に heavy に昇格して再実行される |
| 11 | heavy タスクが最大 `max_heavy_instances` まで並行実行される（上限は qu-e 通知で動的変動、§8.14） |
| 12 | Slack に結果通知が届く（送信元 `channel_id` に返る） |
| 13 | Blender プロセス検知が動作する（`blender_detection: true` 時、`run()` に統合済み） |
| 14 | launchd サービスとして安定稼働する（exit 0） |
| 15 | ログが `/opt/taka-ma/logs/sa-ru.log` に出力される |
| 16 | cross-review が並行投入 → ya-ta 統合で動作する |
| 17 | 配列フォールバックが期待通り動作する |
| 18 | file_audit アラートが Slack に転送される |
| 19 | qu-e のリソース最適化通知を受信し、`max_heavy_instances`（heavy 並行数上限）が動的更新される（§8.14） |
| 20 | 会話キューの発話を脳 LLM で処理し、`ready=false` は会話返信・`ready=true` は着手確認を提示する（§8.3 (A)） |
| 21 | 着手確認が `confirmed` で `source=conversation` の確定タスクが生成され dispatcher に流れる。`rejected`/`timeout` では実行されない（§8.10b） |
| 22 | `fallback.max_fallback_attempts: 0` で次候補を試行せず `_notify_failure` 経由で `failed` 通知が Slack に届く |

## 主要 API（実装本体への索引）

本実装は [`src/orchestrator/`](../../src/orchestrator/) を参照。中身の説明は構築作業ではないが、検証・運用時の手掛かりとして主要メソッドを索引化する:

- [`Orchestrator.run()`](../../src/orchestrator/__init__.py) — dispatcher + worker_light + worker_heavy を並行起動
- [`Orchestrator._dispatcher()`](../../src/orchestrator/__init__.py) — タスクファイル監視・分解・キュー投入
- [`Orchestrator._conversation_loop() / _handle_conversation_message()`](../../src/orchestrator/__init__.py) — 会話キュー監視 → `ConversationManager.handle_message()` へ（§8.3 (A)）。ファイル取り回しは共有 `FileQueue`
- [`Orchestrator._exec_confirmation_loop() / _finalize_confirm()`](../../src/orchestrator/__init__.py) — 着手確認の決着検知。confirmed→確定タスク生成、rejected/timeout→実行せず通知（§8.10b）。走査は `FileQueue.iter_records()`（壊れレコードは failed/ 隔離）
- [`FileQueue`](../../src/orchestrator/file_queue.py) — 各待受の列挙・パース・壊れファイル隔離（failed/）・done/ 退避を集約する共有ファイルキュー。tasks/conversations/controls/exec-confirmations が利用。待受方式（これら 4 経路は poll 据え置き／watchdog は file_audit・リソース通知に限定）の選択方針と根拠は [design §8.15](../design/design-development-system.md)
- [`ConversationManager`](../../src/orchestrator/conversation.py) — 会話セッション保持・脳 LLM（`sa-ru.model`）呼び出し・要約提示（`_present_summary`）・確定タスク生成（`create_exec_task`）。プロンプトは [`prompts/converse.md`](../../src/orchestrator/prompts/converse.md)
- [`Orchestrator._execute_chain()`](../../src/orchestrator/__init__.py) — サブタスク連鎖実行（依存・cascading skip 対応）
- [`Orchestrator._worker_light() / _worker_heavy()`](../../src/orchestrator/__init__.py) — カテゴリ別ワーカー（heavy は `DynamicConcurrencyLimiter` で制御、上限は §8.14 で動的変動）
- [`Orchestrator._execute_worker_task()`](../../src/orchestrator/__init__.py) — モデル候補リスト生成・フォールバック・cross-review 振り分け
- [`Orchestrator._execute_cross_review() / _integrate_cross_review()`](../../src/orchestrator/__init__.py) — 複数モデル並行投入 → ya-ta 統合
- [`Orchestrator._update_status() / _push_task_context()`](../../src/orchestrator/__init__.py) — タスク状態遷移時に qu-e へ SSH push（§8.13 / A1 §5）。payload に `workspace`（`_workspace_for(task_id)` = `{workspace_base}/{task_id}`）を含め、qu-e の path→task_id 帰属を可能にする。あわせて `thread_ts` を含め、qu-e の file_audit アラートが実行中タスクと同一 Slack スレッドへ Thread 返信できるようにする（§8.12）
- [`FileAuditHandler`](../../src/orchestrator/__init__.py) — qu-e の file_audit アラート受信（§8.12 / A1 §1〜§3）
- [`ResourceNotifyHandler`](../../src/orchestrator/__init__.py) — qu-e のリソース最適化通知を受信し `heavy_limiter.set_limit()` で並行数上限を動的更新（§8.14、フロー図 [Appendix_resource-optimization-flow.md](../design/Appendix_resource-optimization-flow.md)）
- [`DynamicConcurrencyLimiter`](../../src/orchestrator/concurrency.py) — 実行時に上限を変更できる `asyncio.Semaphore` 代替。heavy 並行数制御に使用（§8.14）
- [`RemoteProcessManager.run_ssh_command() / run_model_subprocess()`](../../src/orchestrator/process_manager.py) — 汎用 SSH コマンド実行 / worker モデルの SSH 単発実行（対話不要。prompt は stdin 渡し、ただし `keychain_auth: true` のモデル（agy）は GUI 起源 tmux 内で引数渡し実行し出力ファイルで回収する `_run_in_gui_tmux`。§8.6 Antigravity・§8.7 Gemma）
- [`WorkerPtyWrapper`](../../src/orchestrator/pty_wrapper.py) — 対話型 worker CLI 用の **汎用 PTY ラッパー**（pexpect + tmux）。起動コマンドを `command` 引数で受け取り、Claude Code / Antigravity CLI / 将来の Codex 等を共通インタフェースで扱う。`cwd`（タスク専用 workspace）を渡すと tmux `-c` で当該ディレクトリ起動。後方互換のため `ClaudeCodeWrapper` エイリアスを残置
- `_select_method(model_conf, use_case)`（[`__init__.py`](../../src/orchestrator/__init__.py)）— `ya-ta.yaml` の `methods` 配列と用途（`default` / `cross_review` / `multimodal`）から経路（pty / subprocess）を動的選択
- `_run_worker_pty(instance_id, cli_command, command, channel, model_flag, workspace)`（[`__init__.py`](../../src/orchestrator/__init__.py)）— PTY 経路の **駆動ループ**（§8.5）。worker を `WorkerPtyWrapper`（`workspace` を cwd に）で起動 → タスク投入 → stdout を逐次読取 → y/n 検出時に `ApprovalPipeline.process()` で承認/拒否 → 完了（EOF）まで継続し蓄積 stdout を返す。pexpect は別スレッド、承認は `run_coroutine_threadsafe` で event loop へ委譲（Tier3 の Slack 待ちのため）
- [`ApprovalPipeline`](../../src/approval-pipeline/main.py) — `_run_worker_pty` が y/n 検出時に呼ぶ承認本体。Tier 分類は ya-ta を in-process 呼出、Tier2 審査は qu-e へ SSH（§8.8〜§8.9）。`Orchestrator.__init__` で `config` と `ssh.mbp_host` を注入して生成
- [`SlackNotifier.notify() / send_approval_request() / send_exec_confirm_request() / send_file_audit_alert()`](../../src/orchestrator/slack_notifier.py) — Slack 通知（`send_exec_confirm_request` は着手/やり直すボタン）

設定: [`src/orchestrator/config/sa-ru.yaml`](../../src/orchestrator/config/sa-ru.yaml)
