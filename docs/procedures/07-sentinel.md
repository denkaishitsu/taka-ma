# 07. qu-e:Sentinel

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (qu-e を配備)](#step-2-pyinfra実行-qu-e-を配備)
- [動作確認](#動作確認)
  - [1. launchd サービス稼働確認](#1-launchd-サービス稼働確認)
  - [2. Tier 2 コードレビュー（安全コマンド → approve）](#2-tier-2-コードレビュー安全コマンド--approve)
  - [3. Tier 2 コードレビュー（危険コマンド → deny）](#3-tier-2-コードレビュー危険コマンド--deny)
  - [4. Tier 2 差分レビュー（review_diff）](#4-tier-2-差分レビューreview_diff)
  - [5. file_audit（watchdog → jsonl 追記 → sa-ru へ SSH push）](#5-file_auditwatchdog--jsonl-追記--sa-ru-へ-ssh-push)
  - [6. task_context 受信（sa-ru → qu-e SSH push）](#6-task_context-受信sa-ru--qu-e-ssh-push)
  - [7. ヘルスチェック](#7-ヘルスチェック)
- [検証項目](#検証項目)
- [主要 API（実装本体への索引）](#主要-api実装本体への索引)
  - [プロンプト](#プロンプト)
  - [設定](#設定)

## 概要

qu-e は MBP 上で常駐する守護プロセス。ローカル LLM（Qwen3.6-35B-A3B、ollama 経由）で **Tier 2 コードレビュー** / **file_audit による不可逆変更の検知** / **task_context 受信** / **ヘルスチェック** / **リソース監視** を担う。設計書 §2.6 / §4 / §8.8 / §8.11 / §8.12 / §8.13 参照。

> **NOTE**: qu-e 推論モデルは Qwen3.6-35B-A3B（MoE 総 35B / active 3B、Q4_K_M 重み ~23GB・実常駐 27GB@262144、2026-06-25 実測）。

#### アーキテクチャ

```
qu-e (MBP) — launchd 常駐
  ├── Tier 2 コードレビュー    (sa-ru から SSH+subprocess 受信、§8.8)
  ├── file_audit               (watchdog 即時検知 → sa-ru へ SSH push、§8.12 / A1 §1〜§4)
  ├── task_context 受信        (sa-ru → qu-e SSH push、§8.13 / A1 §5)
  ├── ヘルスチェック           (CPU/Memory/Disk/Network、psutil)
  ├── リソース最適化           (推奨 heavy 並行数を sa-ru へ SSH push、§8.14 / §4.2)
  └── ollama → Qwen3.6-35B-A3B (重み ~23GB Q4_K_M・実常駐 27GB)
```

## 実行場所

MBP M4 Max (128GB)

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy 完了（Python / pyinfra / ollama）
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通確立（特に **MBP → Mac mini 方向**、`qu-e → sa-ru` の SSH push 経路として双方向必要）

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

手動準備は不要(モデルダウンロードを含めて Step 2 の PyInfra で実施)。

### Step 2: PyInfra実行 (qu-e を配備)

MBP 上で**ローカル実行**する（`@local`）。

```bash
pyinfra -y @local pyinfra/deploys/sentinel.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mbp ...` と記載していたが、これはホストを定義した**インベントリ**が前提（本リポジトリに未整備）で、実行すると `mbp is neither an inventory file, ...` で失敗する（01 の NOTE と同一の欠陥）。本手順は MBP ローカルの `@local` 実行に統一する。

[`pyinfra/deploys/sentinel.py`](../../pyinfra/deploys/sentinel.py) が下記を冪等に実行する:

| # | 内容 | 実装 |
|---|------|------|
| 1 | `ollama pull qwen3.6:35b-a3b-q4_K_M`（qu-e 推論モデル、重み 約 23GB Q4_K_M） | `server.shell` |
| 2 | 依存パッケージ（`watchdog` / `psutil` / `httpx` / `pyyaml`）を `/opt/taka-ma-env` に導入 | `pip.packages` |
| 3 | [`src/sentinel/`](../../src/sentinel/) を `/opt/taka-ma/qu-e/sentinel/` に sync | `files.sync` |
| 4 | 設定ファイル `qu-e.yaml` を `/opt/taka-ma/qu-e/config/qu-e.yaml` に配置 | `files.put` |
| 5 | launchd Agent 登録（`com.taka-ma.qu-e.plist`、`bootout` → `bootstrap`。bootout は非同期に片付くため bootstrap は最大 5 回・1 秒間隔でリトライ） | `files.template` + `server.shell` |

> **NOTE（データディレクトリ）**: `task-context` と file_audit の `log_dir`（`/opt/taka-ma/logs/file-audit`）は pyinfra の明示ステップではなく、qu-e 本体（`main.py` の起動処理 / `FileAuditHandler.__init__`）が起動時に `os.makedirs(..., exist_ok=True)` で作成する（MBP ローカル）。`file-audit-alerts` は sa-ru 側（Mac mini）のディレクトリで、qu-e から見るとリモートの alert 送信先であり MBP には作られない。deny/escalate 発生時に qu-e が `ssh mac-mini "mkdir -p ... && cat > ..."` で初めて作成する（§8.12）。

> **NOTE**: qu-e.yaml の設定項目は [`src/sentinel/config/qu-e.yaml`](../../src/sentinel/config/qu-e.yaml) 本体を参照（設計意図は設計書 §2.6 / §4 / §8.8 / §8.12 / §8.13）。

## 動作確認

> **NOTE（実行元）**: 以下の `ssh mbp "..."` は操作端末が Mac mini（または別端末）である前提。
> **MBP 自身で実行する場合は `ssh mbp` の自己宛て名前解決ができない**（`Could not resolve hostname mbp`）
> ため、`ssh mbp "cmd"` はそのまま `cmd`（ラッパー無しのローカル実行）に読み替える。動作確認2〜4の
> `ssh mac-mini "ssh mbp '...'"`（sa-ru→qu-e の実経路を模した二重 SSH）はこの対象外でそのまま実行する。

### 1. launchd サービス稼働確認

```bash
ssh mbp "launchctl list | grep qu-e"
ssh mbp "tail -20 /opt/taka-ma/logs/qu-e.log"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `launchctl list` の出力 | `com.taka-ma.qu-e` 行があり、PID が数値、Status が `0` | サービス未登録、PID 欄が `-`、Status が非ゼロ |
| `qu-e.log` の出力 | 起動メッセージが記録されている、Python Traceback なし | 起動ログ無し、Traceback、エラーメッセージ |

### 2. Tier 2 コードレビュー（安全コマンド → approve）

Mac mini 側から SSH 経由で qu-e の `review_cli.py` を呼び出す。

```bash
ssh mac-mini "ssh mbp 'cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e /opt/taka-ma-env/bin/python sentinel/review_cli.py --mode command --input \"cat file.txt\" --context {}'"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 標準出力 | JSON 1 行が返る（例: `{\"decision\": \"approve\", \"reason\": \"...\"}`） | JSON パースエラー、空応答 |
| `decision` | `approve` | `deny` / `escalate`（誤判定） |

### 3. Tier 2 コードレビュー（危険コマンド → deny）

```bash
ssh mac-mini "ssh mbp 'cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e /opt/taka-ma-env/bin/python sentinel/review_cli.py --mode command --input \"rm -rf /\" --context {}'"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `decision` | `deny` | `approve` で誤通過 |

### 4. Tier 2 差分レビュー（review_diff）

```bash
ssh mac-mini "ssh mbp 'cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e /opt/taka-ma-env/bin/python sentinel/review_cli.py --mode diff --input \"<diff text>\" --file-path src/app.py'"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 標準出力 | JSON 1 行で `decision` / `issues` / `severity` を含む（`reviewer.py` の `review_diff()` 出力スキーマ） | パースエラー、空応答 |

### 5. file_audit（watchdog → jsonl 追記 → sa-ru へ SSH push）

監視対象パス配下にファイルを作成し、qu-e の検知・判定・通知経路を観察する（詳細仕様は §8.12 / A1 §1〜§4）。

```bash
ssh mbp "echo test > /opt/taka-ma/data/file-audit-target/test.txt"
sleep 5
ssh mbp "tail -3 /opt/taka-ma/logs/file-audit/file-audit-*.jsonl"
ssh mac-mini "ls /opt/taka-ma/data/file-audit-alerts/"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| jsonl 追記 | `/opt/taka-ma/logs/file-audit/file-audit-{YYYY-MM-DD}.jsonl` にレコードが追記される | 追記がない、ファイル未生成 |
| sa-ru への SSH push（deny / escalate のみ） | Mac mini の `/opt/taka-ma/data/file-audit-alerts/` にアラートファイルが届く | approve でも push されてしまう / deny でも push されない |

### 6. task_context 受信（sa-ru → qu-e SSH push）

sa-ru から擬似的に task_context json を SSH push し、qu-e のメモリ store に反映されることを確認する（詳細仕様は §8.13 / A1 §5）。

```bash
ssh mac-mini "scp /tmp/test-context.json mbp:/opt/taka-ma/data/task-context/"
sleep 3
ssh mbp "tail -20 /opt/taka-ma/logs/qu-e.log | grep task_context"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| ログ | `task_context received: task_id=...` のようなメッセージあり | 受信ログなし |
| store 反映 | 後続の file_audit 判定で `指示範囲内` を考慮した結果が返る | 範囲外として誤判定 |
| thread_ts 伝播 | task_context に含まれる `thread_ts` が store に保持され、実行中タスクの file_audit アラートが同一 Slack スレッドへ Thread 返信される（§8.12） | `thread_ts` が None で別投稿になる |

### 7. ヘルスチェック

```bash
ssh mbp "tail -50 /opt/taka-ma/logs/qu-e.log | grep ヘルスチェック"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 定期実行ログ | `interval_sec` ごとに `ヘルスチェック` 実行ログが記録される（`main.py` の `health_check_loop()` が出力するログ文言は日本語「ヘルスチェック」であり英語 `health_check` は出力されない） | ログなし |
| 各項目 | CPU / Memory / Disk / Network の 4 項目とも数値あり、`healthy` / `warning` / `critical` のいずれかが判定される | 項目欠落、判定なし |

## 検証項目

> **検証概要**: qu-e が MBP 上で常駐し、Tier 2 コードレビュー / file_audit / task_context 受信 / ヘルスチェック / リソース最適化通知の一連の機能が期待通り動作することを確認する。

| # | 検証項目 | 対応 |
|---|---------|------|
| 1 | ollama に Qwen3.6-35B-A3B が pull 済 | Step 1 |
| 2 | `/opt/taka-ma/qu-e/sentinel/` 配下に src/sentinel/ がデプロイ済 | Step 2 |
| 3 | `/opt/taka-ma/data/task-context`（MBP ローカル、qu-e 起動時に自動作成）が存在 | Step 2 / 動作確認 6 |
| 4 | launchd サービスとして安定稼働（exit 0、KeepAlive で自動再起動） | 動作確認 1 |
| 5 | Tier 2 コードレビュー: 安全なコマンドが `approve` される | 動作確認 2 |
| 6 | Tier 2 コードレビュー: 危険なコマンド（`rm -rf /` 等）が `deny` される | 動作確認 3 |
| 7 | Tier 2 差分レビュー（`review_diff`）が JSON で返る | 動作確認 4 |
| 8 | file_audit: ファイル変更が即時検知され jsonl に追記される | 動作確認 5 |
| 9 | file_audit: deny / escalate と判定された変更が sa-ru に SSH push される（§8.12 / A1 §2） | 動作確認 5 |
| 10 | file_audit: jsonl が `retention_days` 超過後、起動時および日次でローテーション削除される（A1 §4） | コードレビュー（実装に存在） |
| 10b | workspace rotation: 終了済みタスクの既定 workspace（`{workspace_base}/{task_id}`）と task_context レコードが `workspace_rotation.retention_days` 超過後、起動時および日次で削除される。実行中・`repo:` 指定リポジトリ（workspace_base 外）・symlink 脱出・レコード無し orphan は削除されない（§8.13） | 分離実行テスト（`tests/test_workspace_rotator.py`）／起動ログ |
| 11 | task_context: sa-ru から SSH push された json を即時受信し、メモリ store に反映される（§8.13 / A1 §5） | 動作確認 6 |
| 11b | file_audit: 並行実行中の複数タスクで、変更パスが属する `workspace` から正しい task_id に帰属する（一致なし・複数 in_progress 時は非帰属、§8.13） | コードレビュー（`_pick_task_context`） |
| 11c | task_context: 受信した `thread_ts` が store に保持され、実行中タスクの file_audit アラートが同一 Slack スレッドへ Thread 返信される（§8.12） | 動作確認 6 |
| 11d | file_audit: 静的 `watch_paths` 外の実開発リポジトリ（task_context の `workspace`）がタスク期間中だけ動的に監視され、終了時に解除される（§8.12 動的監視。共有中は解除しない・登録失敗は escalate） | 分離実行テスト（`tests/test_repo_audit_102.py`） |
| 11e | commit_gate: 動的監視対象の git リポジトリへ pre-commit フックが自動導入され（既存フックは非上書き）、staged diff が approve のときのみコミットが通る（§8.12 コミット前ゲート、fail-closed） | 分離実行テスト（同上）／実機 |
| 12 | ヘルスチェックが CPU / Memory / Disk / Network を周期取得し、healthy/warning/critical を判定 | 動作確認 7 |
| 13 | メモリ使用量が想定範囲内（qu-e 単体 ~19GB、Gemma 4 31B との共存 OK） | 実機モニタリング |
| 14 | ログが `/opt/taka-ma/logs/qu-e.log` に出力される | 動作確認 1 |
| 15 | リソース最適化: メモリ使用率しきい値を跨いだとき、推奨 heavy 並行数が sa-ru の `resource_optimization.notify_dir` に SSH push される（§8.14） | コードレビュー（実装に存在）／実機 |

## 主要 API（実装本体への索引）

本実装は [`src/sentinel/`](../../src/sentinel/) を参照。

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `QueReviewer.review_command() / review_diff() / review_file_audit()` | [`reviewer.py`](../../src/sentinel/reviewer.py) | Tier 2 コードレビュー（コマンド / 差分 / file_audit）。ollama HTTP API で qu-e ローカル LLM 呼び出し、JSON レスポンス。パースエラー / 通信失敗 / タイムアウト時は escalate fallback |
| `review_cli.py` | [`review_cli.py`](../../src/sentinel/review_cli.py) | sa-ru からの SSH + subprocess エントリポイント。`--mode {command,diff}` で stdout に JSON 1 行を返す |
| `HealthChecker.check_all()` | [`health_checker.py`](../../src/sentinel/health_checker.py) | CPU / Memory / Disk / Network を psutil で取得、threshold 比較 |
| `FileAuditHandler` / `start_audit()` | [`file_auditor.py`](../../src/sentinel/file_auditor.py) | watchdog 即時検知 → debounce_sec 集約 → `review_file_audit()` → jsonl 追記 → deny / escalate を sa-ru へ SSH push |
| `GitignoreCache` | [`file_auditor.py`](../../src/sentinel/file_auditor.py) | .gitignore mtime キャッシュ（A1 §1） |
| `rotate_jsonl()` | [`file_auditor.py`](../../src/sentinel/file_auditor.py) | retention_days 超過の jsonl 削除（A1 §4） |
| `rotate_workspaces()` | [`workspace_rotator.py`](../../src/sentinel/workspace_rotator.py) | 終了済みタスクの既定 workspace と task_context レコードを `workspace_rotation.retention_days` 超過で削除。判定根拠はレコードの終了 status（completed/failed）＋mtime。削除直前に `FileAuditHandler.suppress_subtree` で自己操作を宣言し escalate 量産を防ぐ（§8.13） |
| `TaskContextHandler` | [`main.py`](../../src/sentinel/main.py) | watchdog で `/opt/taka-ma/data/task-context/` を監視、json 読み込み → store 反映（`workspace` 含む、§8.13） |
| `FileAuditHandler._pick_task_context(path)` | [`file_auditor.py`](../../src/sentinel/file_auditor.py) | 変更パスを各タスクの `workspace` 接頭辞で照合し、並行実行中の正しい task_id を特定（最長一致、曖昧時は非帰属、§8.13） |
| `DynamicWatchManager` | [`file_auditor.py`](../../src/sentinel/file_auditor.py) | 実開発リポジトリの動的監視。task_context の `workspace` をタスク期間中だけ observer に登録・終了で解除（参照カウント）。git リポジトリへ pre-commit フックを自動導入（§8.12 動的監視／コミット前ゲート） |
| `commit_audit_cli.py` | [`commit_audit_cli.py`](../../src/sentinel/commit_audit_cli.py) | コミット前監査ゲートの 1 ショット CLI。staged diff を審査し approve のみ exit 0。監査 jsonl に `event="commit"` で追記（§8.12） |
| `hooks/pre-commit` | [`hooks/pre-commit`](../../src/sentinel/hooks/pre-commit) | 自動導入される git pre-commit フック本体（sh）。監査基盤不在時は警告して素通し、基盤ありでの判定不能は fail-closed で中断 |
| `ResourceOptimizer.recommended_heavy_instances()` / `notify_payload()` | [`resource_optimizer.py`](../../src/sentinel/resource_optimizer.py) | メモリ使用率から推奨 heavy 並行数を算出し、§8.14 通知 payload（recommended_heavy_instances / memory_usage / level）を生成 |
| `resource_notify_loop()` | [`main.py`](../../src/sentinel/main.py) | `notify_interval_sec` 間隔で推奨並行数を算出し、前回値から変化時に sa-ru へ SSH push（§8.14、フロー図 [Appendix_resource-optimization-flow.md](../design/Appendix_resource-optimization-flow.md)） |
| `health_check_loop()` / `daily_rotation_loop()` / `main()` | [`main.py`](../../src/sentinel/main.py) | 起動シーケンス（Observer 起動 + 起動時 retention rotation・workspace rotation + 日次 rotation（jsonl・workspace 両方）/ リソース通知 asyncio task） |

### プロンプト

- [`prompts/file_audit.md`](../../src/sentinel/prompts/file_audit.md) — file_audit 判定プロンプト（A1 §1〜§2、起動時キャッシュ）
- Tier 2 審査（`review_command()` / `review_diff()`）のプロンプトは `reviewer.py` 内 inline 定義（外部ファイル化は要件外）

### 設定

- [`src/sentinel/config/qu-e.yaml`](../../src/sentinel/config/qu-e.yaml) — qu-e の全設定を一元管理
