# 08. y/n 承認パイプライン

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (approval-pipeline を配備)](#step-2-pyinfra実行-approval-pipeline-を配備)
- [動作確認](#動作確認)
  - [1. pytest 通過（配備直後）](#1-pytest-通過配備直後)
  - [2. Tier 1 自動承認（read 系コマンド）](#2-tier-1-自動承認read-系コマンド)
  - [3. Tier 2 qu-e 審査（file write 系コマンド）](#3-tier-2-qu-e-審査file-write-系コマンド)
  - [4. Tier 3 Slack 通知（sudo / `always_escalate_to_human` 該当）](#4-tier-3-slack-通知sudo--always_escalate_to_human-該当)
  - [5. 監査ログ記録](#5-監査ログ記録)
  - [6. `always_deny` 即時拒否](#6-always_deny-即時拒否)
  - [7. decide デーモン稼働（headless フックの判定実行系）](#7-decide-デーモン稼働headless-フックの判定実行系)
  - [8. フック経由の allow / deny（MBP からの end-to-end）](#8-フック経由の-allow--denymbp-からの-end-to-end)
  - [9. fail-closed（デーモン停止時に全ツール deny）](#9-fail-closedデーモン停止時に全ツール-deny)
- [検証項目](#検証項目)
- [主要 API（実装本体への索引）](#主要-api実装本体への索引)
  - [設定](#設定)
  - [テスト](#テスト)

## 概要

worker CLI のツール実行を三段階リスク判定（Tier 1/2/3）に基づいて自動 / 半自動 / 手動承認する。承認要求の取得は実行アダプタごとに異なる（設計書 §3 / §8.5 / §8.8 / §8.9 参照）:

- **headless アダプタ（Claude Code）**: MBP の PreToolUse フックが SSH で Mac mini の薄いクライアント（`decide_client.py`）を起動し、**launchd 常駐の decide デーモン**（`decide_daemon.py`・Unix ドメインソケット）が中核 `decide()` を実行する（設計 [Appendix §2.1](../design/Appendix_worker-execution-adapters.md#21-判定実行系--decide-デーモンmac-mini-常駐とフックの薄いクライアント化)）
- **interactive(pty) アダプタ（agy 対話等）**: 対話型 CLI が出力する y/n プロンプトを汎用 PTY ラッパー（`WorkerPtyWrapper`、構築手順書 05 主要 API 参照）で捕捉し、sa-ru 内（in-process）でパイプラインを起動する

> **NOTE**: interactive 経路の承認パイプラインは **sa-ru の一部** として動作し、独自の launchd 登録は不要。headless 経路の判定実行系のみ decide デーモン（`com.taka-ma.decide-daemon`）として launchd 常駐する（Step 2 で配備）。

#### アーキテクチャ

```
MBP（worker 側・headless）                Mac mini
claude -p … の PreToolUse フック
  ssh mac-mini decide_client … || exit 2 ─→ decide_client（標準ライブラリのみ）
                                              └ UDS /opt/taka-ma/data/decide.sock
                                            decide_daemon（launchd 常駐・asyncio 並行）
                                              └ ApprovalPipeline.decide()
sa-ru (Mac mini・interactive)
  ├── WorkerPtyWrapper（PTY stdout 監視 → y/n 検出）
  └── approval-pipeline (sa-ru 配下、launchd 不要)
        ├── interceptor    （y/n パターン検出、context から対象コマンド抽出）
        ├── classifier     （ya-ta 連携 → Tier 判定、§3.3）
        ├── tier1_handler  （Low Risk: 自動承認）
        ├── tier2_handler  （Medium Risk: qu-e 審査 → deny 時 escalate、§8.8）
        ├── tier3_handler  （High Risk: Slack 経由人間承認、§8.9、5 分タイムアウト）
        └── audit_logger   （jsonl 監査ログ、§3.5）
```

承認フロー全体は [設計書 §3.4 承認フロー図](../design/design-development-system.md#34-承認フロー図) を正とする（ya-ta スコープ判定 → 範囲内なら自動承認、範囲外なら Tier 1/2/3 分類）。

## 実行場所

Mac mini（判定・Tier 1〜3 ハンドラ・監査ログ） + MBP（PTY 制御）

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy 完了（Python / pyinfra / `/opt/taka-ma-env/`）
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通確立
- [03-slack-bot.md](03-slack-bot.md) の Slack Bot 稼働（Tier 3 通知の前提）
- [04-ai-gateway.md](04-ai-gateway.md) の ya-ta 稼働（Tier 判定の前提）
- [05-orchestrator.md](05-orchestrator.md) の sa-ru 稼働（`WorkerPtyWrapper` がパイプラインの起動点）
- [07-sentinel.md](07-sentinel.md) の qu-e 稼働（Tier 2 審査の前提）

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

手動準備は不要（パイプライン本体・設定ファイル・テストはすべて Step 2 の PyInfra で配備される）。

### Step 2: PyInfra実行 (approval-pipeline を配備)

```bash
pyinfra -y @local pyinfra/deploys/approval_pipeline.py
```

[`pyinfra/deploys/approval_pipeline.py`](../../pyinfra/deploys/approval_pipeline.py) が下記を冪等に実行する:

| # | 内容 | 実装 |
|---|------|------|
| 1 | pytest 導入（`/opt/taka-ma-env`、テスト実行の前提） | `pip.packages` |
| 2 | [`src/approval-pipeline/`](../../src/approval-pipeline/) を `/opt/taka-ma/sa-ru/approval-pipeline/` に sync（`config/pipeline.yaml` を含む） | `files.sync` |
| 3 | 旧 `decide_cli.py` の残骸掃除（デーモンへ吸収済み。`files.sync` は delete しないため明示撤去） | `files.file present=False` |
| 4 | pytest 実行（`tests/` 配下全件: `test_interceptor.py` / `test_e2e.py` / `test_tier3_crossprocess.py` / `test_decide_daemon.py` / `test_decide_client.py`） | `server.shell` |
| 5 | decide デーモンの launchd 常駐化（[`com.taka-ma.decide-daemon.plist.j2`](../../pyinfra/templates/com.taka-ma.decide-daemon.plist.j2)・`KeepAlive` で crash 自動再起動） | `files.template` + `server.shell`（bootout → bootstrap リトライ） |

> **NOTE**: pipeline.yaml の設定項目は [`src/approval-pipeline/config/pipeline.yaml`](../../src/approval-pipeline/config/pipeline.yaml) 本体を参照（設計意図は設計書 §3 / §8.9）。
>
> **ローダ**: `ApprovalPipeline` は起動時に自モジュール相対の `config/pipeline.yaml`（＝配備先 `/opt/taka-ma/sa-ru/approval-pipeline/config/pipeline.yaml`）をロードし、`audit.log_path`（監査ログ出力先の SSOT）・`safety.always_deny` / `safety.always_escalate_to_human` を取得する。安全性チェック（`safety.*`）は ya-ta の Tier 判定**前**に決定論で照合する（即時拒否＝§6 / 人間直行＝§4）。フローは設計書 §3.3 (0) 静的安全性チェック / §3.4 フロー図の `SF` ノードを参照。

## 動作確認

### 1. pytest 通過（配備直後）

```bash
ssh mac-mini "cd /opt/taka-ma/sa-ru/approval-pipeline && PYTHONPATH=/opt/taka-ma/ya-ta:/opt/taka-ma/sa-ru/orchestrator /opt/taka-ma-env/bin/python -m pytest tests/ -v"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| pytest 結果 | 全テストが PASSED（`test_interceptor.py` / `test_e2e.py`） | FAILED あり、import エラー |

### 2. Tier 1 自動承認（read 系コマンド）

sa-ru 経由で対話型 worker CLI に read 系コマンドを投入し、`ApprovalPipeline.process()` が Tier 1 ハンドラで自動承認することを確認する。

| 観点 | 成功 | エラー |
|------|------|--------|
| handler 結果 | `decision: allow`、`handler: tier1` | escalate / deny に振り分けられる |
| PTY 応答 | `y` が stdin に送信され、worker CLI が処理続行 | 応答が返らない、人手介入待ち |

### 3. Tier 2 qu-e 審査（file write 系コマンド）

file write 系コマンドを投入し、Tier 2 ハンドラから qu-e への問い合わせが届き、approve / deny が返ることを確認する（qu-e 側ログは構築手順書 07 動作確認 2/3/4 を参照）。

| 観点 | 成功 | エラー |
|------|------|--------|
| qu-e 問い合わせ | qu-e のレビューログに該当コマンドが記録される | 問い合わせが届かない |
| handler 結果（approve） | `decision: allow`、`handler: tier2` | escalate に切り替わる |
| handler 結果（deny） | `tier: 3`、`handler: tier3` に切り替わる（escalate） | deny のまま停止 |

### 4. Tier 3 Slack 通知（sudo / `always_escalate_to_human` 該当）

sudo 等のコマンドを投入し、Tier 3 ハンドラから Slack に承認リクエストが投稿されることを確認する。

| 観点 | 成功 | エラー |
|------|------|--------|
| Slack 通知 | 承認リクエストがチャネルに投稿される（Approve / Reject ボタン付き） | 通知が届かない、ボタン欠落 |
| Approve ボタン | `y` が stdin に送信される、`decision: allow` | stdin 送信なし |
| Reject ボタン | `n` が stdin に送信される、`decision: deny` | stdin 送信なし |
| タイムアウト | `tier3_timeout_sec`（既定 300 秒）経過で自動 deny | タイムアウトせず無限待機 |

### 5. 監査ログ記録

```bash
ssh mac-mini "tail -3 /opt/taka-ma/logs/approval-audit.jsonl"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| jsonl 追記 | `process()` 1 回ごとに 1 行追記（`instance_id` / `command` / `tier` / `handler` / `decision` / `reason` / `duration_ms`） | 追記なし、JSON パース失敗 |

### 6. `always_deny` 即時拒否

`always_deny` リストに含まれるコマンド（例: `rm -rf /`）を投入し、Tier 判定前に即時拒否されることを確認する。

| 観点 | 成功 | エラー |
|------|------|--------|
| handler 結果 | `decision: deny`（Tier 判定スキップ） | 通常 Tier フローに流れる |
| 監査ログ | `reason` に `always_deny` 該当が記録される | 通常 reason のみ |

### 7. decide デーモン稼働（headless フックの判定実行系）

```bash
ssh mac-mini "launchctl print gui/\$(id -u)/com.taka-ma.decide-daemon | grep state"
ssh mac-mini "ls -l /opt/taka-ma/data/decide.sock"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| launchd 状態 | `state = running` | 不在・spawn 失敗（`/opt/taka-ma/logs/decide-daemon-error.log` を確認） |
| ソケット | `decide.sock` が存在（srw・オーナーのみ） | 不在（デーモン起動失敗） |

### 8. フック経由の allow / deny（MBP からの end-to-end）

MBP 上から、フックコマンドと同一経路（SSH → decide_client → デーモン）で判定を通す。

```bash
# Tier 1 相当（read 系）→ exit 0 ＋ permissionDecision:"allow"
echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"tool_use_id":"t1"}' | \
  ssh mac-mini "/opt/taka-ma-env/bin/python3 /opt/taka-ma/sa-ru/approval-pipeline/decide_client.py --task-id verify"; echo "exit=$?"
# always_deny 相当 → stderr に理由・exit 2
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"},"tool_use_id":"t2"}' | \
  ssh mac-mini "/opt/taka-ma-env/bin/python3 /opt/taka-ma/sa-ru/approval-pipeline/decide_client.py --task-id verify"; echo "exit=$?"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| allow | stdout に `permissionDecision":"allow"`・exit=0 | exit 非 0、応答なし |
| deny | stderr に理由・exit=2 | exit 0 で素通り、exit 1 等（契約違反） |

### 9. fail-closed（デーモン停止時に全ツール deny）

```bash
ssh mac-mini "launchctl bootout gui/\$(id -u)/com.taka-ma.decide-daemon"
echo '{"tool_name":"Bash","tool_input":{"command":"ls"},"tool_use_id":"t3"}' | \
  ssh mac-mini "/opt/taka-ma-env/bin/python3 /opt/taka-ma/sa-ru/approval-pipeline/decide_client.py"; echo "exit=$?"
ssh mac-mini "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.taka-ma.decide-daemon.plist"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 停止中の判定 | stderr に fail-safe deny・exit=2 | exit 0 / exit 1（fail-open の穴） |
| 復帰 | bootstrap 後、動作確認 8 の allow が再び通る | 再起動後も判定不能 |

## 検証項目

> **検証概要**: y/n 承認パイプラインが sa-ru の一部として組み込まれ、対話型 worker CLI からの y/n プロンプトを Tier 1/2/3 に分類し、それぞれ自動承認 / qu-e 審査 / Slack 経由人間承認を実行できることを確認する。`always_deny` 即時拒否、5 分タイムアウト、jsonl 監査ログを含む。

| # | 検証項目 | 対応 |
|---|---------|------|
| 1 | `/opt/taka-ma/sa-ru/approval-pipeline/` 配下に src/approval-pipeline/ がデプロイ済 | Step 2 |
| 2 | y/n パターン検出テストが通過（`tests/test_interceptor.py`） | 動作確認 1 |
| 3 | E2E テストが通過（`tests/test_e2e.py`） | 動作確認 1 |
| 4 | Tier 1（read 系コマンド）が自動承認される | 動作確認 2 |
| 5 | Tier 2（file write）が qu-e に転送され、審査結果が返る | 動作確認 3 |
| 6 | Tier 2 deny 時に Tier 3 にエスカレートする | 動作確認 3 |
| 7 | Tier 3（sudo 等）が Slack に通知を送信する | 動作確認 4 |
| 8 | Slack の Approve ボタンで `y` が stdin に送信される | 動作確認 4 |
| 9 | Slack の Reject ボタンで `n` が stdin に送信される | 動作確認 4 |
| 10 | Tier 3 タイムアウト（`tier3_timeout_sec` 既定 300 秒）で自動 deny | 動作確認 4 |
| 11 | 全操作が監査ログ（approval-audit.jsonl）に記録される | 動作確認 5 |
| 12 | `always_deny` リストのコマンドが即座に拒否される | 動作確認 6 |
| 13 | decide デーモンが launchd 常駐し UDS を待ち受けている | 動作確認 7 |
| 14 | MBP からフック経路（SSH → decide_client → デーモン）で allow / deny が返る | 動作確認 8 |
| 15 | デーモン停止時にフック経路が exit 2（fail-closed）となり、read 系の素通りがない | 動作確認 9 |

## 主要 API（実装本体への索引）

本実装は [`src/approval-pipeline/`](../../src/approval-pipeline/) を参照。

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `ApprovalPipeline.process()` | [`main.py`](../../src/approval-pipeline/main.py) | パイプライン本体。classify → handler → escalate（必要時）→ 監査ログ |
| `detect_prompt()` / `extract_command()` | [`interceptor.py`](../../src/approval-pipeline/interceptor.py) | stdout 行から y/n プロンプトを正規表現検出、context バッファから対象コマンドを抽出 |
| `RiskClassifier.classify()` | [`classifier.py`](../../src/approval-pipeline/classifier.py) | ya-ta へ Tier 判定を依頼（設計書 §3.3）。ya-ta はライブラリ方式のため `ai_gateway.RiskClassifier` を **in-process** 呼出（同期処理は to_thread） |
| `Tier1Handler.handle()` | [`tier1_handler.py`](../../src/approval-pipeline/tier1_handler.py) | Low Risk: 自動承認 |
| `Tier2Handler.handle()` | [`tier2_handler.py`](../../src/approval-pipeline/tier2_handler.py) | Medium Risk: qu-e 審査（§8.8）。qu-e へ **SSH** で `review_cli.py` を 1 ショット実行 → JSON。approve のみ承認、deny / escalate および失敗時は escalate を返す |
| `Tier3Handler.handle()` | [`tier3_handler.py`](../../src/approval-pipeline/tier3_handler.py) | High Risk: Slack 経由人間承認（§8.9）、`tier3_timeout_sec` で自動 deny |
| `AuditLogger.log()` | [`audit_logger.py`](../../src/approval-pipeline/audit_logger.py) | jsonl 形式の監査ログ（§3.5） |
| `DecideDaemon` / `PipelineHolder` | [`decide_daemon.py`](../../src/approval-pipeline/decide_daemon.py) | headless フック判定の常駐サーバ（UDS・asyncio 並行・config mtime 再ロード。設計 Appendix §2.1） |
| `decide_client.main()` | [`decide_client.py`](../../src/approval-pipeline/decide_client.py) | フックの薄い入口（標準ライブラリのみ）。allow=exit 0 / deny・全異常=exit 2 の出力契約 |

### 設定

- [`src/approval-pipeline/config/pipeline.yaml`](../../src/approval-pipeline/config/pipeline.yaml) — パイプライン設定を一元管理（`tier3_timeout_sec` / `audit.log_path` / `safety.always_deny` / `safety.always_escalate_to_human`）
- [`src/orchestrator/config/sa-ru.yaml`](../../src/orchestrator/config/sa-ru.yaml) `headless` ブロック — フックコマンドの組み立て材料（`mini_host` / `decide_client` / `decide_socket` / `python_bin` / `hook_timeout_sec`）

### テスト

- [`tests/test_interceptor.py`](../../src/approval-pipeline/tests/test_interceptor.py) — y/n パターン検出テスト
- [`tests/test_e2e.py`](../../src/approval-pipeline/tests/test_e2e.py) — Tier 1 自動承認 / Tier 3 escalate の E2E
- [`tests/test_decide_daemon.py`](../../src/approval-pipeline/tests/test_decide_daemon.py) — デーモンの契約（allow/deny 変換・fail-closed・並行性・判定タイムアウト）
- [`tests/test_decide_client.py`](../../src/approval-pipeline/tests/test_decide_client.py) — クライアントの終了コード契約（allow=0 / deny・到達不可=2）を分離実行で検証
