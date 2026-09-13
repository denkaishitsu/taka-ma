# 詳細設計: インフラ・IaC・モデル資源配分

> **位置づけ**: [設計書本体](../design-development-system.md) の詳細。本書が扱う節: §6。
> 障害を契機とする設計改訂の経緯は [revisions/](../revisions/) を参照。

pyinfra による冪等配備、インストール来歴と LIFO アンインストールを扱う。

---

<a id="sec-6"></a>
## 6. IaC（Infrastructure as Code）方針

<a id="sec-6-1"></a>
## 6.1 採用技術

| 項目 | 選定 | 理由 |
|------|------|------|
| IaCツール | **Pyinfra** | Python製agentless構成管理。Ansibleの10倍速。pexpect等とスタック統一 |
| パッケージ管理 | **Homebrew (Brewfile)** | macOS標準。Pyinfraから呼び出し |
| バージョン管理 | **GitHub** | IaCコード・設計書・構築手順書を一元管理 |

<a id="sec-6-2"></a>
## 6.2 リポジトリ構造

```
taka-ma/
├── docs/design/                          # 設計書（3 層）
│   ├── design-development-system.md      # 設計書本体（基本設計＋総目次）
│   ├── details/                          # 部位別の詳細設計書
│   ├── revisions/                        # 障害を機に設計を変えた経緯
├── docs/procedures/                      # 各コンポーネント構築手順書
│   ├── 01-common-base.md
│   ├── 02-ssh-tunnel.md
│   ├── 03-slack-bot.md
│   ├── 04-ai-gateway.md
│   ├── 05-orchestrator.md
│   ├── 06-task-models.md
│   ├── 07-sentinel.md
│   └── 08-approval-pipeline.md
├── pyinfra/
│   ├── deploys/
│   │   ├── common.py                     # 共通基盤（Homebrew, Python, venv, ディレクトリ）
│   │   ├── ssh_tunnel.py                 # SSH/Tailscale 設定
│   │   ├── orchestrator.py               # sa-ru 本体
│   │   ├── ai_gateway.py                 # ya-ta
│   │   ├── slack_bot.py                  # u-zu
│   │   ├── sentinel.py                   # qu-e daemon
│   │   ├── approval_pipeline.py          # 承認パイプライン
│   │   ├── task_models.py                # task_models（MBP のローカル LLM 群）
│   │   └── _manifest.py                  # インストール来歴の記録ヘルパ
│   ├── lib/
│   │   ├── install_manifest.py           # マニフェスト読み書き
│   │   └── uninstall.py                  # 逆順（LIFO）アンインストール runner
│   ├── templates/                        # launchd plist / sshd conf テンプレート
│   └── keys/                             # SSH 鍵（taka-ma-cluster、git 管理外）
├── scripts/
│   ├── bootstrap.sh                      # 初回セットアップ（Homebrew→Python→uv→Pyinfra）
│   └── stub_audit.py                     # stub 検出の監査ヘルパ
├── Brewfile                              # Homebrew依存パッケージ
└── README.md
```

**デプロイ先構造（2 層）**

各コンポーネントは `/opt/taka-ma/<コンポーネント名>/<役割名パッケージ>/` の 2 層構造で配備する（コンポーネント名と役割名を明示的に分離）:

| コンポーネント | デプロイ先 |
|--------------|-----------|
| sa-ru | `/opt/taka-ma/sa-ru/orchestrator/`（承認パイプライン `approval-pipeline/` を同梱） |
| ya-ta | `/opt/taka-ma/ya-ta/ai_gateway/` |
| qu-e | `/opt/taka-ma/qu-e/sentinel/` |
| u-zu | `/opt/taka-ma/u-zu/slack_bot/` |

- 設定ファイルは各コンポーネント配下の `config/`（例: `/opt/taka-ma/sa-ru/config/sa-ru.yaml`、`/opt/taka-ma/qu-e/config/qu-e.yaml`）
- 共有データ・ログ・環境変数は横断で `/opt/taka-ma/{data,logs,config}/`

<a id="sec-6-3"></a>
## 6.3 運用コマンド

```bash
# 初回: Pyinfra のインストール（各マシンで 1 回）
./scripts/bootstrap.sh

# 構築: 各コンポーネントを pyinfra で冪等デプロイ（順序・対象ホストは構築手順書 01〜08 を参照）
pyinfra <host> pyinfra/deploys/<component>.py
# 例) pyinfra mac-mini pyinfra/deploys/common.py

# 全環境撤去: インストール・マニフェストを逆順（LIFO）で再生
/opt/taka-ma-env/bin/python /opt/taka-ma/lib/uninstall.py            # dry-run
/opt/taka-ma-env/bin/python /opt/taka-ma/lib/uninstall.py --apply    # 実撤去
```

<a id="sec-6-4"></a>
## 6.4 構築順序

依存関係に基づく構築順:

```
01. SSH/トンネル設定       ← 最初（マシン間接続の基盤）
02. 共通基盤               ← Homebrew, Python, Pyinfra
03. Gemma 4 31B ローカル推論  ← ローカルモデル基盤
04. sa-ru本体           ← オーケストレーター
05. ya-ta             ← ルーティング
06. u-zu              ← 人間インターフェース
07. qu-e daemon        ← 監視・検証
08. Gemini 連携     ← API連携
09. y/n承認パイプライン     ← 最後（全コンポーネント連携）
```

<a id="sec-6-5"></a>
## 6.5 インストール来歴の記録とアンインストール

本システムは「正確に入れて、正確に消せる」ことを設計要件とする（OSS 配布前提）。構築の各ステップ（pyinfra の自動操作・ユーザーの手動操作の両方）を完了ごとに **インストール・マニフェスト** へ構造的に記録し、アンインストールはこのマニフェストを **逆順（LIFO）で再生** して撤去する。

**記録対象と記録元**

| 種別 | 記録元 | 方式 |
|------|--------|------|
| 自動ステップ | pyinfra 各オペレーションの `changed` 結果 | デプロイ時に構造化（JSON 等）でマニフェストへ追記 |
| 手動ステップ | ユーザーが会話で実施・完了報告する操作（Slack App 登録・API キー入力等） | 構築主体の AI が会話内で完了確認し、同じマニフェストへ追記 |

構築主体は基本 AI エージェントであり、手動部分も会話で完了確認が取れるため、自動・手動の双方を一つの来歴として残せる。

**マニフェストの保存先と形式**

- 各マシンの `/opt/taka-ma/data/install-manifest.jsonl`（追記式 JSONL、1 行 = 1 ステップ）。構築は host ごとに走るため、マニフェストもマシン単位で保持する。
- ローカル保管・外部送信しない。機微情報（SSH 鍵パス・トークン・API キー値）は記録せず、種別・宛先のみとする。

**レコード・スキーマ（1 ステップ）**

```json
{
  "seq": 12,
  "ts": "2026-06-03T10:21:33+09:00",
  "host": "mac-mini",
  "source": "pyinfra",
  "component": "sa-ru",
  "operation": "files.directory /opt/taka-ma/sa-ru",
  "target": "/opt/taka-ma/sa-ru",
  "teardown": { "op": "files.directory", "path": "/opt/taka-ma/sa-ru", "present": false },
  "status": "completed"
}
```

- `source`: `pyinfra`（自動）/ `manual`（ユーザー操作）。
- `seq`: 記録順。アンインストールはこの降順（LIFO）で `teardown` を実行する。
- `teardown`: 撤去に必要な対称オペレーション（`files.*(present=False)` / `launchctl bootout` / `ollama rm` 等）。

**記録タイミング**

| 種別 | 誰が | タイミング |
|------|------|-----------|
| 自動（pyinfra） | 構築する AI | 各 deploy の完了時、オペレーションの `changed` 結果を解析してマニフェストへ追記 |
| 手動（ユーザー操作） | 構築する AI | 会話で完了確認した時点で、同じマニフェストへ追記 |

**アンインストール（逆順撤去）**

- マニフェストを `seq` 降順（LIFO）で再生し、各レコードの `teardown` を実行する。
- 常駐サービスの停止を最優先（launchd `KeepAlive` の自動再起動を止める）。
- 共有資源（汎用 Homebrew パッケージ等）・外部資産（Slack App・API キー・Tailscale）は `teardown` に含めず、利用者の明示判断に委ねる。
- 俯瞰と手動手順は [構築手順書 00](../../procedures/00-overview.md#アンインストール方法と仕組み) を参照。

<a id="sec-6-6"></a>
## 6.6 配備元ガード（未マージ配備の停止）

未マージ worktree からの pyinfra 配備は「main に無いコミットの内容」を実機へ書き、他タスクのマージ済み修正を静かに巻き戻しうる（`private/docs/incidents/2026-08-14-wave1-B-regression-report.md` 検証1。[是正 2026-07-31](../revisions/2026-07-31.md)）。再発防止として、全 deploy は読み込み時に **配備元ガード** を通す。

- 実装は `pyinfra/deploys/_guard.py` の `ensure_merged_head()`。各 deploy が `ensure_brew_path()` と同位置（読み込み時・オペレーション宣言より前）で呼ぶ。
- 検査: 配備元リポジトリの HEAD コミットが **main（`origin/main` またはローカル `main` のいずれか）に含まれる**こと。未マージなら配備全体を即時エラー停止する。
- 検査不能（git 不在・リポジトリ外・main 参照なし）も黙って通さず停止する（フェイルクローズ）。
- 迂回は明示フラグ **`TAKA_MA_ALLOW_UNMERGED=1`**（環境変数・値 `"1"` のみ有効）に限る。迂回時はその旨を stderr に明示する。
- 判定ロジックは純粋関数 `evaluate()` に分離し、pyinfra 無しで単体テスト可能（`pyinfra/tests/test_deploy_guard.py`）。

---

