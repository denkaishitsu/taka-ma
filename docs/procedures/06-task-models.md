# 06. タスク実行モデル

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備(PyInfra 実行前)](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (worker LLM を配備)](#step-2-pyinfra実行-worker-llm-を配備)
  - [Step 3: PyInfra 実行後 （手動実行 / OAuth 認証）](#step-3-pyinfra-実行後-手動実行--oauth-認証)
- [動作確認](#動作確認)
  - [1. Gemma 4 31B の配備確認](#1-gemma-4-31b-の配備確認)
  - [2. Gemma 4 31B 単発実行(light)](#2-gemma-4-31b-単発実行light)
  - [3. Claude Code 単発実行(heavy 主軸)](#3-claude-code-単発実行heavy-主軸)
  - [4. Antigravity CLI 単発実行(高度なマルチモーダル解析 / セカンドオピニオン参加 / フォールバック)](#4-antigravity-cli-単発実行高度なマルチモーダル解析--セカンドオピニオン参加--フォールバック)
  - [5. Mac mini から SSH 経由で 3 モデルが起動できる](#5-mac-mini-から-ssh-経由で-3-モデルが起動できる)
- [検証項目](#検証項目)
- [主要 API(実装本体への索引)](#主要-api実装本体への索引)

## 概要

MBP 上で稼働する **タスク実行モデル（worker LLM）** 群を構築する。sa-ru(構築手順書 05)が ya-ta(構築手順書 04)の判定に基づき、SSH 経由でこれらのモデルへタスクを振り分ける。

**初期投入の 3 モデル**

| モデル | 役割 | 推論方式 | 呼び出し経路 |
|--------|------|---------|--------------|
| Claude Code（Opus 4.8） | heavy 主軸(要件定義 / 設計 / 実装 / テスト等の探索的タスク) | API(ProMax サブスク) | `methods: [pty]` — SSH + 汎用 PTY ラッパー(`WorkerPtyWrapper`、y/n プロンプト介入) |
| Antigravity CLI（Gemini 3.5 Flash） | heavy 対話タスク / cross-review 参加 / Opus 障害時フォールバック(テキスト・コード) / 高度なマルチモーダル解析(基本はローカル gemma4、設計書 §2.4) | API(Pro サブスク) | `methods: [pty, subprocess]` — **両対応**。対話 heavy = PTY(§8.5 と共通)、高度な解析単発 / cross-review / フォールバック = subprocess(`agy -p`) |
| Gemma 4 31B | light(軽量タスク、1 回の応答で完結するもの) | ローカル(ollama) | `methods: [subprocess]` — SSH + subprocess(`ollama run gemma4:31b`) |

> **NOTE**: 役割・通信仕様の詳細は設計書 §1.3(モデル配置一覧)/ §2.3 〜 §2.5(役割分担)/ §7(軽量タスク処理モデル)/ §8.4.x(相互扶助機能)/ §8.5 〜 §8.7(通信仕様)を参照。meta カテゴリは廃止済(2026-04-19)。Gemini 3.1 Pro の固有の強みは「高度なマルチモーダル解析」(基本解析はローカル gemma4、生成は Phase 2 — 設計書 §2.4)だが、heavy 対話 / cross-review / フォールバックは **全モデル横断の汎用機能**(§8.4.x)で固定されない。

#### アーキテクチャ

```
sa-ru (Mac mini) ──SSH──→ MBP
                            ├── Claude Code ×N (heavy 主軸、PTY: 汎用 WorkerPtyWrapper、起動コマンド: claude)
                            ├── Antigravity CLI (PTY: 対話 heavy / subprocess: 高度なマルチモーダル解析単発・cross-review・フォールバック、起動コマンド: agy)
                            └── Gemma 4 31B (ollama) (light、subprocess、`ollama run gemma4:31b`)
```

## 実行場所

MBP M4 Max (128GB)

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy が完了している(Homebrew / Python / pyinfra / ollama)
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通が確立している
- 各 worker のサブスク契約が有効である:
  - Anthropic **ProMax**(Claude Code 用)
  - Google Gemini **Pro**(Antigravity CLI 用)

## 構築手順

### Step 1: 手動準備(PyInfra 実行前)

PyInfra ではカバーできない事前確認のみ。

#### 1-1. Anthropic ProMax 契約の有効性確認

https://console.anthropic.com/ にブラウザでログインし、ProMax サブスクが有効であることを確認する。Claude Code は **API キー発行不要**(初回起動時の login でサブスク連携)。

#### 1-2. Google Gemini Pro 契約の有効性確認

Google アカウントの Gemini Pro サブスクが有効であることを確認する。Antigravity CLI は **API キー発行不要**(初回起動時の OAuth でアカウント連携)。

> **NOTE**: Anthropic API SDK / Gemini API SDK を直接利用する場合は API キー方式もあるが、本構築では Claude Code / Antigravity CLI(サブスク + OAuth)を使う。

### Step 2: PyInfra実行 (worker LLM を配備)

MBP 上で**ローカル実行**する（`@local`）。

```bash
pyinfra -y @local pyinfra/deploys/task_models.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mbp ...` と記載していたが、これはホストを定義した**インベントリ**が前提（本リポジトリに未整備）で、実行すると `mbp is neither an inventory file, a (list of) hosts or connectors nor refers to a python module` で失敗する（01 の NOTE と同一の欠陥）。本手順は MBP ローカルの `@local` 実行に統一する。

[`pyinfra/deploys/task_models.py`](../../pyinfra/deploys/task_models.py) が下記を冪等に実行する:

| # | 内容 | 実装 |
|---|------|------|
| 1 | Node.js 導入(`brew install node`、Claude Code / Antigravity CLI の前提) | `brew.packages` |
| 2 | Claude Code 導入(`npm install -g @anthropic-ai/claude-code`、起動コマンド: `claude`) | `server.shell` |
| 3 | Antigravity CLI 導入(公式 install.sh、起動コマンド: `agy`。binary は `~/.local/bin/agy`) | `server.shell` |
| 3b | worker 用 tmux サーバの launchd 常駐(`com.taka-ma.worker-tmux`、GUI セッション起源。下記 NOTE 参照) | `files.template` + `server.shell` |
| 4 | `ollama pull gemma4:31b`(軽量タスク用モデル、約 20GB) | `server.shell` |
| 5 | 各 CLI / モデルの version / list 取得による配備確認 | `server.shell` |

> **NOTE（agy と SSH の制約・実測 2026-07-03）**: agy の認証トークンは macOS keychain 管理であり、**SSH セッション（別セキュリティセッション）からは keychain を読めない**（素の `ssh mbp "agy -p ..."` は `Error: authentication failed or timed out` になる）。このため GUI ログイン時に launchd で tmux サーバ（セッション名 `taka-ma-worker`）を起動しておき、SSH からの agy 実行は **この tmux サーバ内で行う**。GUI 起源の tmux 内では keychain 参照が可能（実測で確認済）。claude / ollama はファイルベース認証のため素の SSH で動く。

> **NOTE**: 各 worker LLM は常駐サービスではない。タスク投入時に sa-ru が SSH 経由で起動するため、launchd 登録は不要。

### Step 3: PyInfra 実行後 （手動実行 / OAuth 認証）

各 CLI の初回起動でブラウザ OAuth フローを実施し、サブスク契約済アカウントと紐付ける。

#### 3-1. Claude Code の認証

```bash
ssh mbp "claude"
```

初回起動時に表示される URL をブラウザで開き、Anthropic アカウント(ProMax 契約済)でログインして認証を完了する。認証情報は MBP の `~/.claude/` 配下に保存される。

#### 3-2. Antigravity CLI の認証

```bash
ssh mbp "agy"
```

初回起動時に Google Sign-In が走る。SSH（リモート）セッションでは認可 URL が表示されるので、ブラウザで開き Google アカウント(Gemini Pro 契約済)でログインして認証を完了する。認証情報は system keyring で管理される（保存形態・SSH 非対話での再利用可否は実機確認で確定）。

> **NOTE**: 認証情報は各 CLI が自前で管理する(Claude Code は `~/.claude/`、agy は system keyring)。`/opt/taka-ma/config/.env` への API キー配置は本構築では不要(将来 API SDK 直接利用に切り替える場合のみ追加)。binary は `~/.local/bin/agy` に配置されるため、SSH 非対話シェルの PATH に `~/.local/bin` が含まれることを確認する。

## 動作確認

### 1. Gemma 4 31B の配備確認

```bash
ssh mbp "ollama list | grep gemma4"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `ollama list` の出力 | `gemma4:31b` 行があり、約 20GB のサイズ表示 | 行が無い、ダウンロード未完了 |

### 2. Gemma 4 31B 単発実行(light)

```bash
ssh mbp "ollama run gemma4:31b 'What is 2+2? Answer in one word.'"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 標準出力 | Gemma からの応答テキストが返る(例: `4`) | エラーで終了、空応答、ollama 未起動 |

### 3. Claude Code 単発実行(heavy 主軸)

```bash
ssh mbp "claude --version"
ssh mbp "claude -p 'Explain Pyinfra in one sentence.'"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `--version` の出力 | バージョン文字列が返る | コマンド未認識 |
| `-p` の出力 | Claude Opus 4.8 の応答が返る | 認証エラー(OAuth 未完了)、API エラー、空応答 |

### 4. Antigravity CLI 単発実行(高度なマルチモーダル解析 / セカンドオピニオン参加 / フォールバック)

```bash
ssh mbp "agy --version"
# -p（認証必要）は GUI 起源 tmux サーバ経由で実行する（Step 2 NOTE 参照）
ssh mbp "tmux send-keys -t taka-ma-worker 'agy -p \"Explain Pyinfra in one sentence.\" > /tmp/agy-check.txt 2>&1' Enter"
sleep 20 && ssh mbp "cat /tmp/agy-check.txt"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `--version` の出力 | バージョン文字列が返る | コマンド未認識 |
| `-p` の出力 | Gemini 3.5 Flash の応答が返る | 認証エラー(`authentication failed or timed out` = 素の SSH 直実行 or OAuth 未完了)、API エラー、空応答 |

### 5. Mac mini から SSH 経由で 3 モデルが起動できる

Mac mini 側(sa-ru 環境)から SSH 経由で各モデルへ単発実行を投げる。

```bash
ssh mac-mini "ssh mbp 'ollama run gemma4:31b \"hi\"'"
ssh mac-mini "ssh mbp 'claude -p \"hi\"'"
# agy は GUI 起源 tmux サーバ経由（Step 2 NOTE 参照）
ssh mac-mini "ssh mbp 'tmux send-keys -t taka-ma-worker \"agy -p \\\"hi\\\" > /tmp/agy-v9.txt 2>&1\" Enter'"
sleep 20 && ssh mbp "cat /tmp/agy-v9.txt"
```

| 観点 | 成功 | エラー |
|------|------|--------|
| Mac mini → MBP の SSH 経路 | 3 コマンドとも応答が返る | SSH 接続失敗、PATH 不備、認証情報未配備 |

## 検証項目

> **検証概要**: 初期投入の 3 種の worker LLM(Claude Code / Antigravity CLI / Gemma 4 31B)が MBP 上で構築済み、サブスク + OAuth で認証済、Mac mini の sa-ru から SSH 経由で起動・実行できることを確認する。

| # | 検証項目 | 対応 |
|---|---------|------|
| 1 | Node.js が MBP に導入済(`node --version` が動作) | Step 2 |
| 2 | ollama に `gemma4:31b` が pull 済 | 動作確認 1 |
| 3 | Gemma 4 31B が単発実行で応答を返す | 動作確認 2 |
| 4 | Claude Code が MBP に導入済(`claude --version` が動作) | Step 2 / 動作確認 3 |
| 5 | Claude Code が OAuth 認証済で単発実行で応答を返す | Step 3 / 動作確認 3 |
| 6 | Antigravity CLI が MBP に導入済(`agy --version` が動作) | Step 2 / 動作確認 4 |
| 7 | Antigravity CLI が OAuth 認証済で単発実行で応答を返す | Step 3 / 動作確認 4 |
| 8 | 認証情報が各 CLI の管轄(`~/.claude/` / agy は system keyring)で保存され、Git 管理されていない | Step 3 |
| 9 | Mac mini から SSH 経由で 3 モデルそれぞれを起動できる | 動作確認 5 |

## 主要 API(実装本体への索引)

- PyInfra: [`pyinfra/deploys/task_models.py`](../../pyinfra/deploys/task_models.py)(Claude Code / Antigravity CLI / Gemma 4 31B の 3 モデル統合配備)
- 設計書 [§1.3](../design/design-development-system.md#13-モデル配置一覧) / [§2.3 Claude Code](../design/design-development-system.md#23-claude-code-nopus-48--mbp-並行実行) / [§2.4 Gemini](../design/design-development-system.md#24-gemini-35-flashapi--mbp) / [§2.5 Gemma 4 31B](../design/design-development-system.md#25-gemma-4-31bmbp-ローカル) / [§7 軽量タスク処理モデル](../design/design-development-system.md#7-軽量タスク処理モデル-セットアップ)
- 通信仕様: [§8.4.x 相互扶助機能(全モデル横断)](../design/design-development-system.md#84x-相互扶助機能全モデル横断ya-ta-の中核価値) / [§8.5 対話型 worker CLI(汎用 PTY 経路)](../design/design-development-system.md#85--sa-ru--対話型-worker-cli重量タスク実行pty-経路) / [§8.6 Antigravity CLI(subprocess 経路)](../design/design-development-system.md#86--sa-ru--antigravity-clisubprocess-経路) / [§8.7 sa-ru → Gemma 4 31B](../design/design-development-system.md#87--sa-ru--gemma-4-31b軽量タスク実行)
- ya-ta 側のモデル登録: [`src/ai_gateway/config/ya-ta.yaml`](../../src/ai_gateway/config/ya-ta.yaml) の `models` セクション
