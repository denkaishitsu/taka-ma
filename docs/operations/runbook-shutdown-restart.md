# 停止・再起動 運用 Runbook（Mac mini / MacBook Pro）

- 2 台（Mac mini = 司令塔 / MacBook Pro = 実行機）の **graceful な停止・再起動手順**と、**再起動後の自動復帰前提・稼働確認**をまとめた運用ページ。
- システム構成・役割は [00. システムの俯瞰](../procedures/00-overview.md)、設計判断は [設計書](../design/design-development-system.md) を参照。
- u-zu（Slack Bot）視点のサービス挙動は [u-zu 運用情報](u-zu/slack-bot.md) を参照（本ページと相互参照）。

## 目次

- [前提：再起動後の自動復帰](#前提再起動後の自動復帰)
- [停止手順](#停止手順)
- [再起動手順](#再起動手順)
- [再起動後の稼働確認チェックリスト](#再起動後の稼働確認チェックリスト)

## 前提：再起動後の自動復帰

**再起動後の手動復旧は原則不要。** 各サービスは OS 起動時に自動復帰するよう構成されている。

| 対象 | 自動復帰の仕組み | 配置 | 根拠 |
|------|----------------|------|------|
| ollama | `brew services`（`enabled=True`）でログイン時に自動起動 | 両マシン | [`pyinfra/deploys/common.py`](../../pyinfra/deploys/common.py) `brew.service(running=True, enabled=True)` |
| sa-ru / u-zu | launchd Agent `RunAtLoad`+`KeepAlive`。GUI ログインで自動起動・異常終了で自動再起動 | Mac mini | [05](../procedures/05-orchestrator.md) / [03](../procedures/03-slack-bot.md)、`templates/com.taka-ma.{sa-ru,u-zu}.plist.j2` |
| qu-e | launchd Agent `RunAtLoad`+`KeepAlive` | MBP | [07](../procedures/07-sentinel.md)、`templates/com.taka-ma.qu-e.plist.j2` |
| ya-ta | sa-ru プロセス内のライブラリ（独立サービスなし）。sa-ru 起動で同時に復帰 | Mac mini | [05](../procedures/05-orchestrator.md) Step 9 |
| MBP worker LLM 群（light/heavy） | **常駐しない。** sa-ru がタスクごとに SSH で都度起動する（`ssh mbp "ollama run …"` / `claude` / `agy`） | MBP | [06](../procedures/06-task-models.md) |

注意点:

- **launchd Agent（sa-ru / u-zu / qu-e）の自動起動には GUI ログインが必要。** 無人再起動で確実に復帰させたい場合は、各マシンで「ユーザを自動でログイン」（システム設定 → ユーザとグループ）を有効にする。
- スリープは無効化済み（[01](../procedures/01-common-base.md) `sudo pmset -a sleep 0` / `disablesleep 1`）。停止操作をしない限り、放置でサービスが落ちることはない。
- MBP の ollama サービスさえ起動していれば、worker は sa-ru が SSH で都度起動するため、MBP 側に worker の手動起動は不要。

## 停止手順

新規タスク受付を止めてから停止する。停止順序は **Mac mini（司令塔）→ MacBook Pro（実行機）**。司令塔を先に止めることで、停止作業中に新しいタスクが MBP worker を起動するのを防ぐ。

### 1. 実行中タスクを落ち着かせる（Mac mini）

```bash
# 新規タスク受付を止める（sa-ru を停止。u-zu は管理者として残す）
#   Slack: /taka-ma-stop
# または SSH:
ssh mac-mini "launchctl bootout gui/\$(id -u)/com.taka-ma.sa-ru"
```

- sa-ru を止めると新規のタスク分解・worker 振り分けが止まる。実行中タスクの MBP worker（`ollama run` / `claude` / `agy`）は SSH 子プロセスのため、完了するか sa-ru 停止で打ち切られる。重要なタスクは完了を待つ。

### 2. Mac mini をシャットダウン

```bash
ssh mac-mini "sudo shutdown -h now"
```

- これで sa-ru / u-zu（launchd）・ollama（brew services）が停止する。`KeepAlive` はシャットダウン時には作用しない。

### 3. MacBook Pro をシャットダウン

```bash
ssh mbp "sudo shutdown -h now"
```

- qu-e（launchd）・ollama（brew services）が停止する。司令塔が先に落ちているため、新たな SSH worker は起動しない。

> 単に再起動するだけなら `-h`（halt）の代わりに `-r`（reboot）を使う。下記「再起動手順」参照。

## 再起動手順

起動順序は停止の逆、**MacBook Pro（実行機）→ Mac mini（司令塔）**。MBP の ollama を先に立ち上げておくと、mini の sa-ru が起動直後にタスクを受けても worker 接続に失敗しない。

```bash
# 1. MBP を再起動（ollama・qu-e が自動復帰）
ssh mbp "sudo shutdown -r now"

# 2. Mac mini を再起動（ollama・sa-ru・u-zu が自動復帰）
ssh mac-mini "sudo shutdown -r now"
```

- GUI ログインが完了すると launchd Agent（`RunAtLoad`）が各サービスを起動する。自動ログイン未設定の場合は、各マシンで一度ログインする必要がある。
- 個別サービスだけ落ちている場合の復旧は、機体ごと再起動せず launchd の `bootstrap` で個別復旧する（[u-zu 運用情報](u-zu/slack-bot.md#復旧) 参照）。

## 再起動後の稼働確認チェックリスト

両マシンが起動・ログインしたら、下記を上から順に確認する。

```bash
# 1. ollama（両マシン、brew services）
ssh mac-mini "brew services list | grep ollama"   # → started
ssh mbp      "brew services list | grep ollama"    # → started

# 2. launchd 常駐サービス
ssh mac-mini "launchctl list | grep taka-ma"       # → com.taka-ma.sa-ru / com.taka-ma.u-zu（PID 数値・Status 0）
ssh mbp      "launchctl list | grep taka-ma"       # → com.taka-ma.qu-e（PID 数値・Status 0）

# 3. ローカル LLM の存在（モデルが消えていないこと）
ssh mac-mini "ollama list | grep -E 'deepseek-r1|gemma4'"   # ya-ta / sa-ru 用
ssh mbp      "ollama list | grep -E 'gemma4|qwen3-coder'"   # light worker / qu-e 用
```

- [ ] 上記 1〜3 がすべて期待どおり
- [ ] **end-to-end 疎通**: Slack で `/taka-ma-status` が全サービス稼働を返す
- [ ] **end-to-end 疎通**: Slack に軽量タスクを 1 件投入し、sa-ru → ya-ta 分類 → MBP worker 起動 → 応答までが通る（MBP worker の SSH 都度起動が機能していることの確認）
