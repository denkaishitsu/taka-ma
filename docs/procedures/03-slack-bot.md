# 03. u-zu: Slack Bot

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (u-zu を配備)](#step-2-pyinfra実行-u-zu-を配備)
  - [Step 3: PyInfra 実行後 （手動実行）](#step-3-pyinfra-実行後-手動実行)
- [動作確認](#動作確認)
  - [1. launchd サービス稼働確認](#1-launchd-サービス稼働確認)
  - [2. Socket Mode 接続確認](#2-socket-mode-接続確認)
  - [3. Slash Command 動作確認](#3-slash-command-動作確認)
  - [4. Tier 3 承認リクエスト動作確認](#4-tier-3-承認リクエスト動作確認)
- [検証項目](#検証項目)
- [主要 API（実装本体への索引）](#主要-api実装本体への索引)
  - [メイン起動](#メイン起動)
  - [Tier 3 承認リクエストの通知 (Block Kit)](#tier-3-承認リクエストの通知-block-kit)
  - [file_audit Approve / Reject ハンドラ（§8.12 / A1 §3）](#file_audit-approve--reject-ハンドラ812--a1-3)
- [運用情報](#運用情報)

## 概要

Slack Private Channel を唯一の人間インターフェースとして機能する Bot を構築する。
Socket Mode で接続し、外部からの Webhook 公開は不要。

会話フロントエンド化に伴い、u-zu は受け取った発話を即タスク化せず **会話キュー**（`/opt/taka-ma/data/conversations/`）へ流す。意図の引き出し・要約・実行意図判定は sa-ru の脳（`sa-ru.model`）が担い、人間の **着手確認**（着手 / やり直すボタン、§8.10b）を経てから確定タスクが生成される。u-zu はボタン押下を確認レコードへ反映するだけ（§8.3）。

## 実行場所

Mac mini M4 Pro (司令塔)

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy が完了している
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通が確立している
- Slack ワークスペース管理者権限（App 作成・トークン発行のため）

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

Slack ワークスペース・App の新規作成は Web 上で実施する（PyInfra ではカバーできない作業）。管理画面のクリック単位の操作詳細は [Appendix A: Slack 設定 完全手順](appendix-A-slack-setup.md) を参照。

#### 1-1. Slack ワークスペースの作成

1. https://slack.com/get-started#/createnew にアクセス
2. メールアドレスを入力、確認コードを入力
3. ワークスペース名: 任意（例: `taka-ma`）
4. 初期チャンネルはスキップしてよい（後で作成する）

#### 1-2. Slack App を作成

1. https://api.slack.com/apps にアクセス
2. 「Create New App」→「From scratch」
3. App Name: `taka-ma`
4. Workspace: 1-1 で作成したワークスペースを選択

#### 1-3. Socket Mode を有効化

1. 左メニュー「Socket Mode」→ Enable Socket Mode を ON
2. App-Level Token を生成:
   - Token Name: `taka-ma-socket`
   - Scope: `connections:write`
   - 生成された `xapp-...` トークンを控える

#### 1-4. Bot Token Scopes の設定

左メニュー「OAuth & Permissions」→ Bot Token Scopes に以下を追加:

| Scope | 用途 |
|-------|------|
| `chat:write` | メッセージ送信 |
| `chat:write.customize` | Bot 名・アイコンのカスタマイズ |
| `groups:history` | Private Channel のメッセージ読み取り |
| `groups:read` | Private Channel 情報の取得 |
| `app_mentions:read` | @メンション検知 |
| `im:history` | DM メッセージ読み取り |
| `commands` | スラッシュコマンド |
| `files:write` | ファイルアップロード（ログ添付等） |
| `reactions:write` | リアクション追加（ステータス表示） |

#### 1-5. Event Subscriptions の設定

左メニュー「Event Subscriptions」→ Enable Events を ON

Subscribe to bot events:

| Event | 用途 |
|-------|------|
| `app_mention` | @taka-ma で Bot 呼び出し |
| `message.groups` | Private Channel メッセージの監視 |
| `message.im` | DM メッセージの受信（Bot への直接指示） |

#### 1-6. Slash Commands の設定

左メニュー「Slash Commands」→ 以下を作成:

| コマンド | 説明 | 用途 |
|---------|------|------|
| `/taka-ma-task` | 相談を開始 | sa-ru に発話を送る（会話キューへ。即実行はしない） |
| `/taka-ma-go` | 会話を締めて着手 | 直近会話を要約し着手確認へ進める（定型命令の明示エスケープ） |
| `/taka-ma-status` | システム状態を確認 | 各コンポーネントの稼働状況 |
| `/taka-ma-approve` | 承認リクエストに応答 | Tier 3 人間承認 |
| `/taka-ma-stop` | 緊急停止 | 全プロセスの即時停止 |
| `/taka-ma-start` | サービス復旧 | `/taka-ma-stop` で停止したサービスを再起動 |
| `/taka-ma-ollama-stop` | ollama 手動停止 | MBP の稼働 ollama モデルを停止（Owner、§8.10c。次推論で自動再ロード） |
| `/taka-ma-logs` | ログを取得 | 直近のログをチャンネルに投稿 |
| `/taka-ma-blender` | Blender モード切替 | LLM 停止/再開の手動トリガー |
| `/taka-ma-user` | ユーザー管理 | add / update / remove / list（Owner/Admin のみ） |
| `/taka-ma-model` | モデル管理 | add / remove / update / list / install / uninstall（Owner/Admin のみ） |

#### 1-7. App をワークスペースにインストール

1. 左メニュー「Install App」→「Install to Workspace」
2. 権限を確認して「Allow」
3. Bot User OAuth Token (`xoxb-...`) を控える

### Step 2: PyInfra実行 (u-zu を配備)

Mac mini 上で**ローカル実行**する（`@local`）。

```bash
pyinfra -y @local pyinfra/deploys/slack_bot.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mac-mini ...` と記載していたが、これはホストを定義した**インベントリ**が前提（本リポジトリに未整備）で、実行すると `mac-mini is neither an inventory file, ...` で失敗する（01 の NOTE と同一の欠陥）。本手順は Mac mini ローカルの `@local` 実行に統一する。

[`pyinfra/deploys/slack_bot.py`](../../pyinfra/deploys/slack_bot.py) が下記を冪等に実行する:

| # | 内容 | 実装 |
|---|------|------|
| 1 | Python パッケージ導入（`slack-bolt` / `slack-sdk` / `python-dotenv`） | `pip.packages` |
| 2 | [`src/slack_bot/`](../../src/slack_bot/) を `/opt/taka-ma/u-zu/slack_bot/` に sync | `files.sync` |
| 3 | launchd plist の配置 (`com.taka-ma.u-zu.plist`) | `files.template` |
| 4 | launchd 登録（`bootout` → `bootstrap`） | `server.shell` |

plist テンプレートは [`pyinfra/templates/com.taka-ma.u-zu.plist.j2`](../../pyinfra/templates/com.taka-ma.u-zu.plist.j2) を参照。主要キー:

| キー | 内容 |
|------|------|
| `Label` | サービス識別子（`com.taka-ma.u-zu`） |
| `ProgramArguments` | 起動コマンド |
| `RunAtLoad` | ログイン時の自動起動 |
| `KeepAlive` | プロセス異常終了時の自動再起動 |
| `StandardOutPath` | 標準出力ログのパス |
| `StandardErrorPath` | 標準エラーログのパス |
| `EnvironmentVariables` | `PATH` 等の環境変数 |

### Step 3: PyInfra 実行後 （手動実行）

#### 3-1. Slack トークンの配置

`/opt/taka-ma/config/.env` に Step 1 で取得した Slack 認証情報を配置する。**絶対に GitHub にコミットしない。**

```bash
ssh mac-mini "cat >> /opt/taka-ma/config/.env" << 'EOF'
SLACK_BOT_TOKEN=<xoxb-...>
SLACK_APP_TOKEN=<xapp-...>
SLACK_CHANNEL_ID=<C0X...>
EOF
ssh mac-mini "chmod 600 /opt/taka-ma/config/.env"
```

> **NOTE**: `.env` ファイルは `/opt/taka-ma/config/.env` で一元管理する。各コンポーネント（03-slack-bot, 06-task-models 等）が自分のキーを追記する。雛形 `.env.example` は本 deploy（Step 2）が [`pyinfra/templates/env.example.j2`](../../pyinfra/templates/env.example.j2) から `/opt/taka-ma/config/.env.example` に配置する。

#### 3-2. Private Channel の作成と Bot 招待

1. Slack で Private Channel `#taka-ma` を作成
2. Bot を招待: `/invite @taka-ma`

#### 3-3. 初期 Owner の登録

`/opt/taka-ma/config/users.yaml` にシステム構築者の Slack user ID を Owner として手動登録する。テンプレートは [`src/slack_bot/config/users.yaml.example`](../../src/slack_bot/config/users.yaml.example) を参照。

#### 3-4. 複数ワークスペース運用時のトークン登録

複数の Slack ワークスペースで運用する場合、各ワークスペースを `team_id` で識別し、ワークスペースごとの bot トークンを登録する。Socket Mode は app-level トークン 1 本で全ワークスペースのイベントを受信するため、追加ワークスペースでもポート開放は不要（OAuth installer は採らない）。ワークスペースを追加するごとに以下を繰り返す。

1. 1-2 で作成した同一 Slack App（`taka-ma`）を追加ワークスペースにもインストールする（1-7 と同じ操作を対象ワークスペースで実施）。発行される Bot User OAuth Token (`xoxb-...`) を控える。
2. 追加ワークスペースの `team_id`（`T...`）を控える。
3. 3-2 と同様に Private Channel を作成し、Bot を招待してチャンネル ID を控える。
4. `/opt/taka-ma/config/.env` に `team_id` をキーとして bot トークンとチャンネル ID を追記する。app-level トークン（`SLACK_APP_TOKEN`）は 3-1 の 1 本を全ワークスペース共通で使う。

```bash
ssh mac-mini "cat >> /opt/taka-ma/config/.env" << 'EOF'
SLACK_BOT_TOKEN_<TEAM_ID>=<xoxb-...>
SLACK_CHANNEL_ID_<TEAM_ID>=<C0X...>
EOF
ssh mac-mini "chmod 600 /opt/taka-ma/config/.env"
```

送信時は、タスクファイル（§8.3）の `team_id` に対応する `SLACK_BOT_TOKEN_<TEAM_ID>` を選択して応答先ワークスペースを特定する。

## 動作確認

### 1. launchd サービス稼働確認

```bash
ssh mac-mini "launchctl list | grep u-zu"
# → PID が数値、Status が 0

ssh mac-mini "tail -20 /opt/taka-ma/logs/u-zu.log"
# → 起動メッセージが出力され、Python traceback がないこと
```

### 2. Socket Mode 接続確認

`/opt/taka-ma/logs/u-zu.log` に Socket Mode 接続成功ログ（例: `Connected to Slack`）が記録されていること。

### 3. Slash Command 動作確認

Slack で `/taka-ma-task テスト` を実行し、Bot からの応答が返ることを確認。

### 4. Tier 3 承認リクエスト動作確認

テスト用の Tier 3 承認リクエストを発行し、Slack に Block Kit ボタン（Approve / Reject）付きメッセージが届くこと、両ボタンが正しく動作することを確認。

## 検証項目

> **検証概要**: Slack Bot が Mac mini 上で常駐し、Slash Commands / Block Kit ボタン / アクセス制御 / file_audit 経路を通じて Slack と taka-ma システムの双方向連携が期待通り動作することを確認する。

- [ ] Slack App が作成され、ワークスペースにインストール済み
- [ ] Socket Mode で接続が確立される
- [ ] `/taka-ma-task テスト` で応答が返る
- [ ] Bot への DM でメッセージがタスクとして受け付けられる
- [ ] `/taka-ma-status` で各コンポーネントの状態が表示される
- [ ] `/taka-ma-stop` で緊急停止が動作する
- [ ] `/taka-ma-start` で停止したサービスが復旧する
- [ ] Tier 3 承認リクエストが Block Kit ボタン付きで表示される
- [ ] Approve / Reject ボタンが正しく動作する
- [ ] `:gemini` 付きタスク → タスクファイルに model 情報が含まれる
- [ ] `:damini` 等の不正モデル指定 → エラー通知 + 利用可能モデル一覧
- [ ] `/exam_gw` 付きタスク → タスクファイルに `dry_run: true` がセットされる
- [ ] `/exam_gw` 付きタスク → 判定結果のみ返却、タスク未実行
- [ ] 未登録の Slack user ID からのコマンドが拒否される
- [ ] User ロールが `/taka-ma-stop` を実行できない
- [ ] Owner が `/taka-ma-user add @username user` でユーザーを追加できる
- [ ] Admin が Owner のロールを変更できない
- [ ] `/taka-ma-model add` でモデルが ya-ta.yaml に追加される
- [ ] `/taka-ma-model install` で ya-ta.yaml 反映 + sa-ru 再起動が実行される
- [ ] `/taka-ma-model list` で登録済みモデル一覧が返る
- [ ] トークンが `.env` にのみ保存され、Git 管理されていないこと
- [ ] launchd サービスとして自動起動する

## 主要 API（実装本体への索引）

本実装は [`src/slack_bot/`](../../src/slack_bot/) を参照。中身の説明は構築作業ではないが、検証・運用時の手掛かりとして主要モジュール・メソッドを索引化する。

### メイン起動

本実装は [`src/slack_bot/main.py`](../../src/slack_bot/main.py)（Socket Mode で起動）を参照。`main.py` は以下のハンドラモジュールを register する:

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `handlers.commands` | [`src/slack_bot/handlers/commands.py`](../../src/slack_bot/handlers/commands.py) | スラッシュコマンド |
| `handlers.events` | [`src/slack_bot/handlers/events.py`](../../src/slack_bot/handlers/events.py) | Event（メンション等） |
| `handlers.actions` | [`src/slack_bot/handlers/actions.py`](../../src/slack_bot/handlers/actions.py) | Action（ボタン応答） |

### Tier 3 承認リクエストの通知 (Block Kit)

本実装は [`src/slack_bot/templates/approval_block.py`](../../src/slack_bot/templates/approval_block.py) の `build_approval_request()` を参照。Block Kit の構成要素は以下:

| 要素 | 内容 |
|------|------|
| Header | 承認リクエストのタイトル |
| Section fields | リクエスト詳細 |
| Actions | Approve / Reject ボタン |

### file_audit Approve / Reject ハンドラ（§8.12 / A1 §3）

qu-e から sa-ru 経由で送信される file_audit アラート（§8.12、Block Kit Approve/Reject ボタン付き）の callback ハンドラ。動作方針:

- 押下後は **§8.3 のタスク投入経路を再利用**（専用経路は新設しない）
- **すべての操作・作業は同じ経路（§8.3）を通る**（Approve / Reject 双方とも §8.3 にタスク投入）
- ya-ta が分解 → 振り分け先 LLM が実行（A1 §3.1 動作主体表）

callback の属性:

| 属性 | 値 |
|------|---|
| `action_id` | `audit_approve` / `audit_reject` |
| `value` | `audit_log_id` |

本実装:

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `handle_audit_approve()` / `handle_audit_reject()` | [`handlers/actions.py`](../../src/slack_bot/handlers/actions.py) | callback 受信 → `audit_log_id` で jsonl レコード引き当て → §8.3 経路でタスク投入 |
| `_enqueue_audit_action_task()` | [`handlers/actions.py`](../../src/slack_bot/handlers/actions.py) | タスクファイル生成（`source: "slack_action"`、`task_id` / `channel_id` / `thread_ts` を含む） |
| `find_audit_record()` | [`services/audit_lookup.py`](../../src/slack_bot/services/audit_lookup.py) | A1 §4 の jsonl から `audit_log_id` で引き当て（当日 + 前日まで遡る） |

> **NOTE**: 投入された §8.3 タスクは ya-ta が分解し、Approve なら「qu-e の jsonl 追記」、Reject なら「jsonl 追記（qu-e）+ プロセス停止（sa-ru）+ revert（ya-ta が振り分けた LLM）」に分解される（A1 §3.1）。

### 会話投入 / 着手確認ハンドラ（§8.3 / §8.10b）

通常の発話（メンション / DM / `/taka-ma-task` / `/taka-ma-go`）は会話キューへ流し、着手確認のボタン押下を確認レコードへ反映する。

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `enqueue_conversation_message()` | [`services/conversation_queue.py`](../../src/slack_bot/services/conversation_queue.py) | 発話を会話キューへ（`conversation_id` 導出・`force_ready` 付与） |
| `handle_mention()` / `handle_message()` | [`handlers/events.py`](../../src/slack_bot/handlers/events.py) | メンション / DM を会話キューへ |
| `handle_task()` / `handle_go()` | [`handlers/commands.py`](../../src/slack_bot/handlers/commands.py) | `/taka-ma-task`（会話投入）/ `/taka-ma-go`（force_ready で締め） |
| `handle_exec_confirm()` / `handle_exec_reject()` | [`handlers/actions.py`](../../src/slack_bot/handlers/actions.py) | 着手 / やり直すボタン受信 → 確認レコードの status 更新 |
| `resolve_exec_confirm()` | [`services/exec_confirm.py`](../../src/slack_bot/services/exec_confirm.py) | 確認レコードを confirmed / rejected に更新（pending のみ受理） |

callback の属性: `action_id` = `exec_confirm` / `exec_reject`、`value` = `exec_request_id`。

### アクセス制御（実行者認可）ハンドラ（設計書 §1.2）

全ハンドラ（スラッシュコマンド / メンション / DM / ボタン）は処理本体の前に実行者の Slack user ID を `users.yaml` と照合し、必要ロール未満は拒否する。未登録ユーザーは一律拒否。ロール要件表・ロール定義は [`docs/operations/u-zu/slack-bot.md`](../operations/u-zu/slack-bot.md) の「アクセス制御」を正本とする。

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `check_role()` / `authorize()` | [`services/role_check.py`](../../src/slack_bot/services/role_check.py) | ロール階層判定（Owner⊃Admin⊃User）・未登録拒否・拒否メッセージ送信。各ハンドラ先頭の `if not authorize(...): return` ゲート |
| `load_users()` / `add_user()` / `update_user()` / `remove_user()` | [`services/user_store.py`](../../src/slack_bot/services/user_store.py) | `users.yaml` の読み書き（原子的書き込み）。ロール台帳の SSOT |
| `handle_user()` | [`handlers/commands.py`](../../src/slack_bot/handlers/commands.py) | `/taka-ma-user` add/update/remove/list。owner/admin が絡む変更は owner 限定（運用書 ※注） |

## 運用情報

Slack Bot の継続運用情報（サービス管理 / アクセス制御 / タスク投入時の操作説明）は [`docs/operations/u-zu/slack-bot.md`](../operations/u-zu/slack-bot.md) を参照。
