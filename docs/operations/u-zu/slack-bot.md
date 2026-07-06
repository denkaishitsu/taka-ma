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
  - [基本](#基本)
  - [モデル指定](#モデル指定)
  - [複数モデル指定（cross-review）](#複数モデル指定cross-review)
  - [不正なモデル指定](#不正なモデル指定)
  - [ya-ta 検証コマンド（ドライラン）](#ya-ta-検証コマンドドライラン)

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
/taka-ma-model update opus47 --model-flag "--model opus-4.7-latest"
/taka-ma-model remove opus47
/taka-ma-model list
/taka-ma-model install opus47       ← ya-ta.yaml に反映 + sa-ru 再起動
/taka-ma-model uninstall opus47     ← ya-ta.yaml から削除 + sa-ru 再起動
```

- `add` / `update` / `remove`: ya-ta.yaml の models セクションを編集
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
| `:opus` | Claude Opus 4.6 | 重量タスク（デフォルト） |
| `:sonnet` | Claude Sonnet 4.6 | 中量タスク（Opus より高速・低コスト） |
| `:haiku` | Claude Haiku 4.5 | 軽量タスク（高速応答） |
| `:gemini` | Gemini 3.5 Flash | 高度なマルチモーダル解析（動画・音声・画像の理解） |
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
