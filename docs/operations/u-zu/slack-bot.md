# Slack Bot 運用情報

構築完了後の継続運用情報（サービス管理 / アクセス制御 / タスク投入時の操作説明）。構築手順は [`docs/procedures/03-slack-bot.md`](../procedures/03-slack-bot.md) を参照。

## 目次

- [サービス管理](#サービス管理)
  - [アーキテクチャ](#アーキテクチャ)
  - [Mac mini のサービス一覧](#mac-mini-のサービス一覧)
  - [MBP のサービス一覧](#mbp-のサービス一覧)
  - [操作と処理の対応](#操作と処理の対応)
  - [Slack Bot 自体の管理](#slack-bot-自体の管理)
  - [PC シャットダウン・再起動時の挙動](#pc-シャットダウン再起動時の挙動)
  - [緊急停止](#緊急停止)
  - [復旧](#復旧)
- [アクセス制御](#アクセス制御)
  - [ロール定義](#ロール定義)
  - [ユーザー管理（Owner/Admin）](#ユーザー管理owneradmin)
  - [モデル管理（Owner/Admin）](#モデル管理owneradmin)
  - [コマンドごとのロール要件](#コマンドごとのロール要件)
  - [ロールチェックの実装](#ロールチェックの実装)
- [タスク投入時の操作説明](#タスク投入時の操作説明)
  - [入力の3経路と会話の流れ](#入力の3経路と会話の流れ)
  - [基本](#基本)
  - [モデル指定](#モデル指定)
  - [複数モデル指定（cross-review）](#複数モデル指定cross-review)
  - [不正なモデル指定](#不正なモデル指定)
  - [ya-ta 検証コマンド（ドライラン）](#ya-ta-検証コマンドドライラン)
- [プロジェクト別チャンネル運用](#プロジェクト別チャンネル運用)
  - [チャンネル追加手順](#チャンネル追加手順)
  - [リポジトリの指定](#リポジトリの指定)

## サービス管理

### アーキテクチャ

Slack Bot は sa-ru・ya-ta とは独立したプロセスとして常駐する。
`/taka-ma-stop` で sa-ru 等を停止しても Slack Bot 自体は稼働し続けるため、
Slack から `/taka-ma-start` で復旧できる。

```
Slack → Slack Bot (常駐) → launchctl start/stop → sa-ru / ya-ta
```

### Mac mini のサービス一覧

| サービス | launchd Label | plist | 自動起動 |
|---------|---------------|-------|---------|
| Slack Bot | `com.taka-ma.u-zu` | `~/Library/LaunchAgents/com.taka-ma.u-zu.plist` | RunAtLoad + KeepAlive |
| sa-ru | `com.taka-ma.sa-ru` | `~/Library/LaunchAgents/com.taka-ma.sa-ru.plist` | RunAtLoad + KeepAlive |
| ollama | Homebrew Services | `brew services` 管理 | brew services start 済み |

> **NOTE**: ya-ta は sa-ru のプロセス内でライブラリとして動作するため、独立サービスは不要（構築手順書 05 Step 9）。

### MBP のサービス一覧

| サービス | 管理方法 | 自動起動 |
|---------|---------|---------|
| ollama | `brew services` | brew services start 済み |
| qu-e | launchd（07-sentinel で構築予定） | — |

### 操作と処理の対応

| 操作 | 処理内容 | 対象サービス |
|------|---------|-------------|
| `/taka-ma-stop` | `launchctl bootout gui/$(id -u)/<label>` を実行（KeepAlive 再起動を防止） | sa-ru |
| `/taka-ma-start` | `launchctl bootstrap gui/$(id -u) <plist>` を実行 | sa-ru |
| `/taka-ma-status` | `launchctl list` + `pgrep ollama`（SSH 経由で MBP も確認） | 全サービス |
| `/taka-ma-blender on` | SSH 経由で `brew services stop ollama` | MBP の ollama |
| `/taka-ma-blender off` | SSH 経由で `brew services start ollama` | MBP の ollama |
| `/taka-ma-ollama-stop` | controls/ へ制御命令を投入 → sa-ru が `stop_ollama()`（稼働モデルを `ollama stop`、§8.10c）。サービスは残し次推論で自動再ロード | MBP の ollama 稼働モデル |

### Slack Bot 自体の管理

Slack Bot は自分自身を Slack コマンドで停止・再起動できない。ターミナルから Mac mini に SSH 接続して操作する。

```bash
# 停止（bootout は plist パスではなく Label を指定）
ssh mac-mini "launchctl bootout gui/\$(id -u)/com.taka-ma.u-zu"

# 起動
ssh mac-mini "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.taka-ma.u-zu.plist"

# 再起動（停止 → 起動）
ssh mac-mini "launchctl bootout gui/\$(id -u)/com.taka-ma.u-zu && launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.taka-ma.u-zu.plist"
```

| シナリオ | 対応 |
|---------|------|
| コード更新後の反映 | 再起動 |
| Bot がフリーズ | 停止 → 起動（KeepAlive が効かない場合） |
| 設定変更（.env 修正等） | 再起動 |
| クラッシュ | launchd の KeepAlive が自動再起動。手動対応不要 |

### PC シャットダウン・再起動時の挙動

> マシン本体の graceful な停止・再起動コマンド、mini/MBP の順序、再起動後の横断的な稼働確認は [停止・再起動 運用 Runbook](../runbook-shutdown-restart.md) を参照。本節は u-zu（Slack Bot）視点のサービス挙動のみを扱う。

全サービスの plist に `RunAtLoad: true` を設定しているため、**ユーザーがログインすれば launchd が自動起動する。手動復旧は不要。**

| シナリオ | 挙動 | 手動対応 |
|---------|------|---------|
| Mac mini 再起動 | ログイン後、Slack Bot・sa-ru・ollama が自動起動 | 不要 |
| MBP 再起動 | ログイン後、ollama が自動起動 | 不要 |
| `/taka-ma-stop` で手動停止 | `launchctl bootout gui/$(id -u)/<label>` で解除。KeepAlive による自動再起動なし | `/taka-ma-start` または `launchctl bootstrap gui/$(id -u) <plist>` |
| Slack Bot 自体がクラッシュ | KeepAlive により launchd が自動再起動 | 不要（ログで原因確認） |

### 緊急停止

Slack で `/taka-ma-stop` を実行すると、以下のサービスが停止する:

- `com.taka-ma.sa-ru`（ya-ta はライブラリとして内包されているため、sa-ru 停止で停止する）

Slack Bot 自体は停止しない（管理者として常駐）。

### 復旧

Slack で `/taka-ma-start` を実行すると、停止したサービスが再起動する。

ターミナルから手動で復旧する場合:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.sa-ru.plist
```

復旧後、`/taka-ma-status` で全サービスの稼働状態を確認する。

## アクセス制御

### ロール定義

| ロール | 権限 |
|--------|------|
| Owner | 全操作。ユーザー管理、モデル管理、システム停止・復旧、タスク投入 |
| Admin | モデル管理、システム状態確認、タスク投入 |
| User | タスク投入のみ |

ロールは Slack user ID に紐づけて管理する。初期 Owner はシステム構築者（`/opt/taka-ma/config/users.yaml` に手動登録）。

### ユーザー管理（Owner/Admin）

```
/taka-ma-user add @username owner
/taka-ma-user add @username admin
/taka-ma-user add @username user
/taka-ma-user update @username admin    ← ロール変更
/taka-ma-user remove @username
/taka-ma-user list
```

ユーザー情報は `/opt/taka-ma/config/users.yaml` に保存。テンプレートは [`src/slack_bot/config/users.yaml.example`](../../src/slack_bot/config/users.yaml.example) を参照。

### モデル管理（Owner/Admin）

```
/taka-ma-model add opus47 --full-name "claude-opus-4.7" --vendor anthropic --methods pty --model-flag "--model opus-4.7"
/taka-ma-model add llama4 --full-name "llama-4-8b" --model-id llama4:8b --methods subprocess --api-url http://localhost:11434/api/generate --keep-alive-sec 1800
/taka-ma-model update opus47 --model-flag "--model opus-4.7-latest"
/taka-ma-model remove opus47
/taka-ma-model list
/taka-ma-model install opus47       ← ya-ta.yaml に反映 + sa-ru 再起動
/taka-ma-model uninstall opus47     ← ya-ta.yaml から削除 + sa-ru 再起動
```

- `add` / `update` / `remove`: ya-ta.yaml の models セクションを編集。ローカルモデル（`type: local`）は `--api-url`（実行機から見た ollama 生成エンドポイント）と `--keep-alive-sec`（モデルを常駐させ続ける秒数）も必須。ローカルモデルの実行は CLI 起動ではなく HTTP API 呼び出しのため（設計書 §8.7）、この 2 つが無いと登録できても実行時に落ちる
- `install`: ya-ta.yaml に反映し、必要に応じてモデルのダウンロード（ollama pull 等）+ sa-ru 再起動。ローカルモデル（`type: local`）の `install` は `model_id` が必須で、未設定のまま実行すると（実体を何もダウンロードせずに成功と誤報しないよう）エラーになる
- `uninstall`: ya-ta.yaml から削除 + sa-ru 再起動

> **NOTE**: 起動 CLI 名(ya-ta.yaml の `command:`)は **`--vendor` から自動推測**される(例: `anthropic` → `claude`、`google` → `agy`)。推測を上書きしたい場合のみ `--command <CLI 名>` を明示する。

### コマンドごとのロール要件

| コマンド | Owner | Admin | User |
|---------|-------|-------|------|
| `/taka-ma-task` | o | o | o |
| `/taka-ma-status` | o | o | o |
| `/taka-ma-approve` | o | o | x |
| `/taka-ma-stop` | o | x | x |
| `/taka-ma-start` | o | x | x |
| `/taka-ma-ollama-stop` | o | x | x |
| `/taka-ma-logs` | o | o | x |
| `/taka-ma-blender` | o | o | x |
| `/taka-ma-user` | o | o（※） | x |
| `/taka-ma-model` | o | o | x |

※ Admin は User の追加・削除が可能。Owner の変更は Owner のみ。

> **最後の Owner は削除・降格できない**: システムを Owner 権限からロックアウトさせないため、Owner が 1 人だけのときにその Owner を `remove` / 降格 `update` しようとすると拒否される。先に別のユーザーを Owner に昇格させてから操作する。

### ロールチェックの実装

全コマンドハンドラの先頭で実行者の Slack user ID を `users.yaml` と照合する。本実装は [`src/slack_bot/services/role_check.py`](../../src/slack_bot/services/role_check.py) の `check_role(user_id, required_role)` を参照。階層比較の数値:

| ロール | レベル |
|--------|--------|
| owner | 3 |
| admin | 2 |
| user | 1 |

未登録の Slack user ID からのコマンドは全て拒否する。

## タスク投入時の操作説明

### 入力の3経路と会話の流れ

sa-ru への話しかけ方は 3 通りある。**返信がスレッドに入るか通常投稿になるかが経路で異なる**点に注意する。

| 経路 | 例 | @メンション | 返信の入り方 |
|------|----|-----------|------------|
| メンション | `@taka-ma ログインを直したい` | 必要 | **元発話のスレッド内**に返信 |
| DM | Bot との DM で発話 | 不要 | **そのスレッド内**に返信（DM は人単位で会話が続く） |
| スラッシュコマンド | `/taka-ma-task ログインを直したい` | 不要 | **通常投稿**（スレッドに入らない） |

スラッシュコマンドが通常投稿になるのは、Slack の仕様上スラッシュコマンドがスレッド起点（`thread_ts`）を持たないため。**会話を1本のスレッドにまとめたい場合は `@taka-ma` メンションか DM を使う。**

**会話の流れ**（どの経路でも共通）:

1. 発話すると、まず元発話に 👀 リアクションが付く（受付済みの合図）。
2. sa-ru の脳（ローカル LLM）が意図を判定する。数秒〜数十秒かかることがある。
3. 意図がまだ曖昧なら、sa-ru が聞き返してくる（例:「開発したいことや解決したい課題を教えてください」）。そのまま会話を続ける。
4. 意図が固まると、sa-ru が**構造化要約 + 着手確認ボタン**を提示する。ボタンを押すと実際のタスクとして着手する。

> 空メッセージや曖昧な発話では 4 に進まず 3 の聞き返しになる（正常挙動）。すぐ着手させたい定型命令は `/taka-ma-go`（LLM 判定を待たず直近会話を要約して着手確認へ進む）を使う。

### 基本

```
/taka-ma-task ログインフォームを実装して
```

### モデル指定

`:モデル名` をメッセージに付与すると、ya-ta の自動判定を上書きし、指定モデルで実行する。

```
/taka-ma-task この動画を解析して :gemini
/taka-ma-task この機能を実装して :sonnet
```

利用可能なモデル名は ya-ta.yaml の models キー名と完全一致。モデル追加時は ya-ta.yaml に登録すれば自動的に利用可能になる。

以下は初期登録例:

| 指定名 | モデル（例） | 用途 |
|--------|-------------|------|
| `:opus` | Claude Opus 5 | 重量タスク（デフォルト） |
| `:fable` | Claude Fable 5 | 最難関タスク（明示指定のみ） |
| `:sonnet` | Claude Sonnet 5 | 中量タスク（Opus より高速・低コスト） |
| `:haiku` | Claude Haiku 4.5 | 軽量タスク（高速応答） |
| `:gemini` | Gemini 3.6 Flash | 高度なマルチモーダル解析（動画・音声・画像の理解） |
| `:gemini-pro` | Gemini 3.1 Pro | Gemini 最上位（明示指定のみ） |
| `:gemma` | Gemma 4 31B | ローカル軽量（デフォルト light） |

> ya-ta.yaml にモデルを追加・変更すれば、この一覧も変わる。不正なモデル名を指定した場合は、その時点で登録済みのモデル一覧がエラーとして返る。

### 複数モデル指定（cross-review）

2 つ以上のモデルを指定すると、各モデルに並行投入し結果を統合する。

```
/taka-ma-task この設計にセキュリティ上の問題がないか検証して :opus :gemini
```

### 不正なモデル指定

未登録のモデル名を指定した場合、タスクは実行されない。利用可能なモデル一覧がエラーとして返る。

```
/taka-ma-task 解析して :damini
→ ⚠ ':damini' は登録されていません。利用可能: :gemini, :gemma, :haiku, :opus, :sonnet
```

### ya-ta 検証コマンド（ドライラン）

`/exam_gw` をメッセージ末尾に付与すると、タスクを実行せず ya-ta の判定結果（分解・分類・モデル選択・実行方式）だけを返す。

```
/taka-ma-task プロジェクトを解析して :gemini、問題点を改修して /exam_gw
```

## プロジェクト別チャンネル運用

プロジェクトごとに Private Channel を分け、それぞれのチャンネルで `@taka-ma` と会話して開発を進められる。

u-zu に受信チャンネルの許可リストは無く、**Bot が招待されている任意の Private Channel** の `@taka-ma` メンションを受け付ける（認可はユーザー ID のみ。[`src/slack_bot/handlers/events.py`](../../../src/slack_bot/handlers/events.py) `handle_mention()`）。返信・計画提示・承認・完了通知は受信イベントの `channel_id` / `thread_ts` に返るため、チャンネルを増やしても混線しない。会話セッションも `(team_id, channel_id, thread_ts)` 単位で分離される（`conversation_id`、設計書 §8.3）。構築手順書 3-1 の `SLACK_CHANNEL_ID` は `channel_id` を持たない通知のフォールバック先であり、受信を制限しない。

### チャンネル追加手順

1. Slack でプロジェクト用の Private Channel を作成する（例: `#taka-ma-myproject`）。パブリックチャンネルは対象外（App manifest に `channels:*` scope / `message.channels` event が無い。設計書 §1.2「sa-ru が外部と通信できるのは Slack Private channel のみ」）
2. チャンネル内で `/invite @taka-ma` を実行して Bot を招待する
3. `@taka-ma <発話>` で会話を開始する

`.env` の変更・再デプロイ・u-zu の再起動は不要。招待した時点で使える。招待を忘れると、メンション自体が届かないか、届いても返信の `chat_postMessage` が `not_in_channel` で失敗しユーザーには無反応に見えるので注意。

別ワークスペースのチャンネルを使う場合のみ、事前に構築手順書 [`03-slack-bot.md`](../../procedures/03-slack-bot.md) 3-4 のトークン登録（`SLACK_BOT_TOKEN_<TEAM_ID>` 等）が必要。

### リポジトリの指定

作業対象リポジトリは発話中に `repo:<絶対パス>` で明示するか、自然文で指定する（設計書 §8.13。自然文配線は #taka-ma/143）。

```
@taka-ma repo:/Users/xxx/DevDev/myproject ログインフォームを実装して
@taka-ma リポジトリ: /Users/xxx/DevDev/myproject の README を要約して
```

- 自然文指定はマーカー語（repo / repository / リポジトリ）＋区切り＋パスの形で検出され、`repo:` 記法と**同一の検証・展開**に通される（検証を通らない候補は案内文言で `repo:` 記法での再指定を促す。会話は止まらない）
- 指定は**会話セッションに持続**する（冒頭で指定 → 後の発話で着手、でも落ちない。再起動・時間経過も跨ぐ。指定し直せば最後の値が勝つ）。着手のたびに書き直す必要はない
- `~/` 前置きは worker ホストの HOME（`sa-ru.yaml` の `task_context.worker_home`）で絶対パスに展開される（未設定環境では従来どおり差し戻し）
- **着手確認に `workspace:` 行が常時表示される**。未指定の場合は「未指定（既定の空作業場）」と明示されるので、意図しない使い捨て workspace（`{workspace_base}/{task_id}`、既定 `/opt/taka-ma/work/<task_id>`）で走る前に気づける
- パスは絶対パス・安全文字のみ・`..` 不可（fail-closed 検証。`repo:` 記法の不正は発話時点で差し戻される）
