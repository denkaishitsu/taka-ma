# 04. ya-ta: AI Gateway

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (ya-ta を配備)](#step-2-pyinfra実行-ya-ta-を配備)
- [動作確認](#動作確認)
  - [1. ライブラリ import 検証](#1-ライブラリ-import-検証)
  - [2. タスク分解](#2-タスク分解)
  - [3. タスク分類 + モデル指定解析](#3-タスク分類--モデル指定解析)
  - [4. リスク分類](#4-リスク分類)
  - [5. `/exam_gw` ドライラン](#5-exam_gw-ドライラン)
- [検証項目](#検証項目)
- [主要 API（実装本体への索引）](#主要-api実装本体への索引)
  - [モジュール](#モジュール)
  - [プロンプト](#プロンプト)
  - [設定](#設定)
  - [設計判断（要点）](#設計判断要点)
  - [ルーティングロジック（概要）](#ルーティングロジック概要)
  - [メモリ配分（Mac mini 64GB、参考）](#メモリ配分mac-mini-64gb参考)

## 概要

ya-ta はタスクの分解・分類・リスク判定を行う AI ルーティングエンジン。sa-ru から Python ライブラリとして import され、推論は ollama API 経由で DeepSeek-R1 32B が行う（設計書 §8.4）。

#### アーキテクチャ

```
sa-ru (Python プロセス)
  ├── ya-ta (ライブラリ import)
  │     └── ollama API → DeepSeek-R1 32B（推論）
  ├── TaskDecomposer  : タスク分解（DAG 生成）
  ├── TaskClassifier  : 難易度判定 + モデル指定解析
  └── RiskClassifier  : y/n 承認リスク判定
```

> **NOTE**: ya-ta は独立サービスを持たない。過去に独立プロセスとして動作させた際の exit -15 クラッシュ問題を受け、sa-ru に組み込まれた Python ライブラリとして再設計した（設計書 §4）。

## 実行場所

Mac mini M4 Pro (64GB)

## 前提条件

- [01-common-base.md](01-common-base.md) の `bootstrap.sh` および pyinfra deploy が完了している（Python / pyinfra / ollama）
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通が確立している（MBP 側から検証・操作する場合）

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

#### 1-1. DeepSeek-R1 32B のダウンロード

ya-ta の推論モデルを Mac mini の ollama に取得する。サイズが大きく時間がかかるため、PyInfra から分離する。

```bash
ssh mac-mini "ollama pull deepseek-r1:32b"
ssh mac-mini "ollama list | grep deepseek-r1"
# → deepseek-r1:32b  約19GB
```

#### 1-2. num_ctx の 32K 縮小（OOM 回避）— Step 2 の PyInfra が自動実行

ya-ta は Mac mini 64GB に sa-ru と同居する。DeepSeek-R1 を既定 128K で起動すると KV キャッシュで常駐 47GB に達し、sa-ru(実測 8.7GB) との同居で予算 56GB を食い尽くす（実測）。`ollama run` は num_ctx を渡さないため、**Step 2 の `ai_gateway.py` が `deepseek-r1:32b` に `PARAMETER num_ctx 32768` を焼き込む**（同タグ上書き・重み共有・冪等）。手動操作は不要。値は [`ya-ta.yaml`](../../src/ai_gateway/config/ya-ta.yaml) の `num_ctx` と一致させる。

初期投入後の検証:
```bash
ssh mac-mini "ollama run deepseek-r1:32b hi >/dev/null; ollama ps; ollama stop deepseek-r1:32b"
# → CONTEXT 32768 / SIZE 約26GB（128K 既定なら 47GB）
```

### Step 2: PyInfra実行 (ya-ta を配備)

Mac mini 上で**ローカル実行**する（`@local`）。

```bash
pyinfra -y @local pyinfra/deploys/ai_gateway.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mac-mini ...` と記載していたが、これはホストを定義した**インベントリ**が前提（本リポジトリに未整備）で、実行すると `mac-mini is neither an inventory file, ...` で失敗する（01 の NOTE と同一の欠陥）。本手順は Mac mini ローカルの `@local` 実行に統一する。

[`pyinfra/deploys/ai_gateway.py`](../../pyinfra/deploys/ai_gateway.py) が下記を冪等に実行する:

| # | 内容 | 実装 |
|---|------|------|
| 1 | [`src/ai_gateway/`](../../src/ai_gateway/) を `/opt/taka-ma/ya-ta/ai_gateway/` に sync（2 層構造: コンポーネント名 `ya-ta` の下に Python パッケージ `ai_gateway`） | `files.sync` |
| 2 | 設定ファイル `ya-ta.yaml` を `/opt/taka-ma/ya-ta/config/ya-ta.yaml` に配置 | `files.put` |
| 3 | 旧 launchd plist (`com.taka-ma.ya-ta.plist`) の残骸を削除（旧アーキテクチャからの移行のみ、冪等） | `server.shell` |
| 4 | sa-ru からの import 動作確認（`TaskDecomposer` / `TaskClassifier` / `RiskClassifier`） | `server.shell` |

> **NOTE**: 
ya-ta.yaml の設定項目は [`src/ai_gateway/config/ya-ta.yaml`](../../src/ai_gateway/config/ya-ta.yaml) 本体を参照（設計意図は設計書 §2.2 / §8.4）。

## 動作確認

### 1. ライブラリ import 検証

```bash
ssh mac-mini "cd /opt/taka-ma/sa-ru && /opt/taka-ma-env/bin/python -c \"
import sys
sys.path.insert(0, '/opt/taka-ma/ya-ta')
from ai_gateway.decomposer import TaskDecomposer
from ai_gateway.classifier import TaskClassifier, InvalidModelError
from ai_gateway.risk_classifier import RiskClassifier
print('import OK')
\""
```

| 観点 | 成功 | エラー |
|------|------|--------|
| 標準出力 | `import OK` が出力される | `ImportError` / `ModuleNotFoundError` / `Traceback` のいずれかが出る |

### 2. タスク分解

```bash
ssh mac-mini "cd /opt/taka-ma/sa-ru && /opt/taka-ma-env/bin/python -c \"
import sys, json
sys.path.insert(0, '/opt/taka-ma/ya-ta')
from ai_gateway.decomposer import TaskDecomposer

config = {'ya-ta': {'model': 'deepseek-r1:32b'}, 'models': {'opus': {}, 'gemini': {}, 'sonnet': {}, 'haiku': {}, 'gemma': {}}}
d = TaskDecomposer(config)

r = d.decompose('このJSONをYAMLに変換して')
print('単純:', json.dumps(r, ensure_ascii=False))

r = d.decompose('プロジェクトを解析して、問題点を改修して')
print('複合:', json.dumps(r, ensure_ascii=False))
\""
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `単純:` の出力 | サブタスク 1 件、各サブタスクが `step` / `command` / `category` / `depends_on` を持つ | サブタスクが空、JSON パースエラー、キー欠落 |
| `複合:` の出力 | サブタスク 2 件以上、`depends_on` がリスト型で実行順を表現 | 1 件のまま分解されない、`depends_on` がリスト型でない |
| `category` 値 | `light` または `heavy` のみ | `meta` など他の値が混じる |

### 3. タスク分類 + モデル指定解析

```bash
ssh mac-mini "cd /opt/taka-ma/sa-ru && /opt/taka-ma-env/bin/python -c \"
import sys, json
sys.path.insert(0, '/opt/taka-ma/ya-ta')
from ai_gateway.classifier import TaskClassifier, InvalidModelError

config = {
    'ya-ta': {'model': 'deepseek-r1:32b'},
    'models': {
        'opus': {'capabilities': ['code', 'reasoning']},
        'sonnet': {'capabilities': ['code', 'reasoning']},
        'haiku': {'capabilities': ['light-task', 'qa']},
        'gemini': {'capabilities': ['multimodal'], 'capability_description': '動画・音声・画像の解析 → model: gemini'},
        'gemma': {'capabilities': ['light-task']},
    }
}
c = TaskClassifier(config)

print('light:', json.dumps(c.classify('このJSONをYAMLに変換して: {\\\"a\\\": 1}'), ensure_ascii=False))
print('heavy:', json.dumps(c.classify('ログインフォームを実装して'), ensure_ascii=False))
print('heavy(解析):', json.dumps(c.classify('プロジェクト全体のアーキテクチャを評価して'), ensure_ascii=False))

print('単一指定:', c.parse_model('この動画を解析して :gemini'))
print('複数指定:', c.parse_model('この設計を検証して :opus :gemini'))
try:
    c.parse_model('解析して :damini')
except InvalidModelError as e:
    print('不正指定:', e)
\""
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `light:` の出力 | `category: light` | `heavy` / `meta` 等 |
| `heavy:` の出力 | `category: heavy` | `light` |
| `heavy(解析):` の出力 | `category: heavy` | `light` |
| `単一指定:` の出力 | `(コマンド本文, ['gemini'])`（コマンドから `:gemini` が除去される） | `:gemini` が文字列に残る、`models` が空リスト |
| `複数指定:` の出力 | `(コマンド本文, ['opus', 'gemini'])` | 一方のみ抽出、順序破壊 |
| `不正指定:` の出力 | `InvalidModelError` が送出され、エラーメッセージに利用可能モデル一覧が含まれる | 例外が送出されない / モデル一覧が含まれない |

### 4. リスク分類

```bash
ssh mac-mini "cd /opt/taka-ma/sa-ru && /opt/taka-ma-env/bin/python -c \"
import sys, json
sys.path.insert(0, '/opt/taka-ma/ya-ta')
from ai_gateway.risk_classifier import RiskClassifier

config = {'ya-ta': {'model': 'deepseek-r1:32b'}}
rc = RiskClassifier(config)

print('Tier 1:', json.dumps(rc.classify('cat src/readme.md'), ensure_ascii=False))
print('Tier 2:', json.dumps(rc.classify('Write to: src/app.ts'), ensure_ascii=False))
print('Tier 3 (sudo):', json.dumps(rc.classify('sudo chmod 777 /etc/hosts'), ensure_ascii=False))
print('Tier 3 (force push):', json.dumps(rc.classify('git push --force origin main'), ensure_ascii=False))
print('Tier 3 (rm -rf):', json.dumps(rc.classify('rm -rf /opt/taka-ma/data/'), ensure_ascii=False))
\""
```

| 観点 | 成功 | エラー |
|------|------|--------|
| `Tier 1:`（`cat`） | `tier: 1`、`action: auto_approve` | Tier 2 以上に昇格、`action` キー欠落 |
| `Tier 2:`（Write） | `tier: 2`、`action: route_to_qu-e` | Tier 1 に降格、Tier 3 に昇格 |
| `Tier 3 (sudo):` | `tier: 3`、`action: route_to_human` | Tier 2 以下に降格 |
| `Tier 3 (force push):` | `tier: 3`、`action: route_to_human` | Tier 2 以下に降格 |
| `Tier 3 (rm -rf):` | `tier: 3`、`action: route_to_human` | Tier 2 以下に降格 |

### 5. `/exam_gw` ドライラン

Slack で `/taka-ma-task プロジェクトを解析して :gemini、問題点を改修して /exam_gw` を投入する。

| 観点 | 成功 | エラー |
|------|------|--------|
| Slack 返却内容 | 分解・分類結果（`category` / `model` / `methods` / `depends_on` / `confidence`）がメッセージで返る | Slack に何も返らない、または通常のタスク完了通知が返る |
| 外部 LLM 実行 | `/opt/taka-ma/logs/sa-ru.log` に外部 LLM 実行ログが残らない | 外部 LLM 実行ログ（Claude Code / Antigravity CLI 等）が記録される |
| タスクファイル | 実行スキップを示す状態（`dry_run` 処理の done 配置 or 残置）で完了。本番タスクとしての処理を起こさない | 本番タスクとして `in_progress` → `completed` の遷移が走る |

> **NOTE**: `/exam_gw` の実装は Slack Bot がタスクファイルに `dry_run: true` をセット、sa-ru がフラグを確認して実行スキップする経路（構築手順書 03 / 05 参照）。ya-ta 側は通常通り decompose + classify を実行するだけで挙動を変えない。

## 検証項目

> **検証概要**: ya-ta が DeepSeek-R1 32B を介してタスク分解・分類・リスク判定を実行し、ya-ta.yaml に登録されたモデルへのルーティング判定 / confidence < 0.8 の light → heavy 強制 / cross-review / モデル指定解析 / `/exam_gw` ドライランが期待通り動作することを確認する。

| # | 検証項目 | 対応 |
|---|---------|------|
| 1 | ollama に DeepSeek-R1 32B が存在する | Step 1 |
| 2 | sa-ru から `TaskDecomposer` / `TaskClassifier` / `InvalidModelError` / `RiskClassifier` が import できる | 動作確認 1 |
| 3 | タスク分解: 単純な指示がサブタスク 1 件で返る | 動作確認 2 |
| 4 | タスク分解: 複合指示が複数サブタスク（`step` / `command` / `category` / `depends_on`）に分解される | 動作確認 2 |
| 5 | タスク分解: `category` が `light` / `heavy` のみ | 動作確認 2 |
| 6 | タスク分類: 「JSON→YAML 変換」→ light | 動作確認 3 |
| 7 | タスク分類: 「ログイン実装」→ heavy | 動作確認 3 |
| 8 | タスク分類: 「アーキテクチャ評価」→ heavy | 動作確認 3 |
| 9 | タスク分類: 曖昧なタスク → heavy（デフォルト） | 動作確認 3 |
| 10 | モデル指定: `:gemini` → `['gemini']` | 動作確認 3 |
| 11 | モデル指定: `:opus :gemini` → `['opus', 'gemini']`(cross-review） | 動作確認 3 |
| 12 | モデル指定: `:damini` → `InvalidModelError` + 利用可能モデル一覧 | 動作確認 3 |
| 13 | リスク分類: `cat` → Tier 1、ファイル書き込み → Tier 2 | 動作確認 4 |
| 14 | リスク分類: `sudo` / `git push --force` / `rm -rf` → Tier 3 | 動作確認 4 |
| 15 | 旧 launchd サービス（`com.taka-ma.ya-ta`）が残っていない | Step 2 |
| 16 | `ya-ta.yaml` の `models` セクションに全登録モデルが定義されている | Step 2 |
| 17 | `ya-ta.yaml` の `models` に `methods`（配列）/ `command` / `model_flag` が定義されている | Step 2 |
| 18 | `/exam_gw` 付きタスク → 分解・分類結果のみ Slack に返却、タスク未実行 | 動作確認 5 |
| 19 | `deepseek-r1:32b` に `num_ctx 32768` が焼き込まれている（`ollama ps` で CONTEXT 32768 / SIZE 約26GB、OOM 回避） | Step 1 |

## 主要 API（実装本体への索引）

本実装は [`src/ai_gateway/`](../../src/ai_gateway/) を参照。中身の説明は構築作業ではないが、検証・運用時の手掛かりとして主要シンボル・プロンプト・設定を索引化する。

### モジュール

| シンボル | 実装ファイル | 役割 |
|---------|-------------|------|
| `TaskDecomposer` | [`decomposer.py`](../../src/ai_gateway/decomposer.py) | タスク分解（DAG 生成、設計書 §8.4 / §10.2）。JSON パースエラー時は元指示 1 件 heavy fallback、confidence < 0.8 の light → heavy 強制 |
| `TaskClassifier` | [`classifier.py`](../../src/ai_gateway/classifier.py) | 難易度判定 + モデル指定解析。`parse_model()` / `classify()` / `_build_capability_prompt()`（ya-ta.yaml の `models` から capability 判定セクションを動的生成） |
| `InvalidModelError` | [`classifier.py`](../../src/ai_gateway/classifier.py) | 不正モデル指定例外。エラーメッセージに利用可能モデル一覧を含む |
| `RiskClassifier` | [`risk_classifier.py`](../../src/ai_gateway/risk_classifier.py) | y/n 承認リスク判定（Tier 1/2/3、設計書 §3.3 / §3.4）。パースエラー時は Tier 3 fallback |
| `YaTaLogger` | [`logger.py`](../../src/ai_gateway/logger.py) | 判定ログ記録（日付別 jsonl `ya-ta-decisions-{YYYY-MM-DD}.jsonl`、運用改善の学習データ） |

### プロンプト

| ファイル | 役割 |
|---------|------|
| [`prompts/categories.md`](../../src/ai_gateway/prompts/categories.md) | カテゴリ定義（light / heavy）。`decompose_task.md` / `classify_task.md` 両方が `{categories}` で参照する共通プロンプト。カテゴリ変更時はここだけ修正 |
| [`prompts/decompose_task.md`](../../src/ai_gateway/prompts/decompose_task.md) | タスク分解プロンプト |
| [`prompts/classify_task.md`](../../src/ai_gateway/prompts/classify_task.md) | タスク分類プロンプト。`{capabilities_from_ai_gateway_yaml}` は ya-ta.yaml の `models` から動的生成される（`models` にサービスを追加すれば分類プロンプトにも自動反映） |
| [`prompts/classify_risk.md`](../../src/ai_gateway/prompts/classify_risk.md) | リスク分類プロンプト（Tier 1/2/3 判定） |

プロンプト内のカテゴリ定義はモデル名ではなく特性で記述し、具体モデル名は `ya-ta.yaml` の `models` セクションで管理する（モデル変更時にプロンプトの修正不要）。

### 設定

- [`src/ai_gateway/config/ya-ta.yaml`](../../src/ai_gateway/config/ya-ta.yaml) — モデル登録（opus / sonnet / haiku / gemini / gemma 等）、`routing.category_defaults`、`fallback.max_fallback_attempts`

### 設計判断（要点）

| 判断 | 内容 |
|------|------|
| 動作方式 | sa-ru からライブラリ import（独立サービスは exit -15 クラッシュ問題を起こした、設計書 §4） |
| 判定方式 | LLM による知的判定（ルールベース・正規表現は使わない） |
| ya-ta モデル | DeepSeek-R1 32B (Q4_K_M)（推論特化、Mac mini 64GB で ~20GB） |
| ルーティング方針 | デフォルト heavy。confidence < 0.8 の light → heavy 強制（迷ったら安全側） |
| カテゴリ | light / heavy の 2 値（meta は廃止、設計書 §2.2） |
| モデル登録制 | `ya-ta.yaml` の `models` セクション。capabilities ベースでルーティング。追加時は yaml + 分類プロンプト更新のみ |
| API 障害フォールバック | `category_defaults[category]` 配列（優先度順）で次候補へ。`fallback.max_fallback_attempts` で試行回数制限 |
| 明示指定の挙動 | `:モデル名` 明示時は障害でもフォールバックしない（指定モデル尊重） |
| 強制ルーティング | マルチモーダル等のハードコード強制はしない（LLM 判断 + 運用ログから精度改善） |
| 将来統合案 | sa-ru + ya-ta を DeepSeek-V4 に統合（V4 リリース後に検討） |

詳細は [設計書](../design/design-development-system.md) §2.2 / §3.3 / §3.4 / §4 / §8.4 / §10.2 を参照。

### ルーティングロジック（概要）

```
入力: タスク記述 + モデル指定（あれば、from sa-ru）
  │
  │ (1) モデル指定解析（最優先）
  │     :モデル名 1 個 → そのモデルに直接ルーティング
  │     :モデル名 2 個以上 → cross-review（並行投入 → ya-ta 統合）
  │     未登録の :モデル名 → InvalidModelError（タスク未実行、利用可能モデル一覧通知）
  │
  │ (2) モデル指定なし → ya-ta (DeepSeek-R1 32B) が LLM で知的判定
  │     light（明らかに軽量なタスク） / heavy（それ以外、デフォルト）
  │     confidence < 0.8 の light → heavy 強制
  │
  └ (3) orchestrator が実行（フォールバック / cross-review 統合は構築手順書 05 参照）
```

### メモリ配分（Mac mini 64GB、参考）

```
sa-ru (Gemma 4 12B Q4):     8.7GB（実測 2026-06-20、重み 7.6 + KV 1.1、num_ctx 40960・q8_0）
ya-ta (DeepSeek-R1 32B Q4): ~20GB  ← sa-ru と同一プロセスだがモデルは ollama が管理
OS + バッファ:              ~26GB
```
