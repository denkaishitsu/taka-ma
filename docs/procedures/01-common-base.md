# 01. 共通基盤 (Homebrew, Python, Pyinfra)

## 目次

- [概要](#概要)
- [対象マシン](#対象マシン)
- [構築手順](#構築手順)
  - [Step 1: 手動準備 （PyInfra 実行前）](#step-1-手動準備-pyinfra-実行前)
  - [Step 2: PyInfra実行 (共通基盤を構築)](#step-2-pyinfra実行-共通基盤を構築)
- [手動補足](#手動補足)
  - [スリープ無効化（両マシン）](#スリープ無効化両マシン)
- [検証項目](#検証項目)

## 概要

Mac mini と MBP の両方に共通で必要なベース環境を構築する。

## 対象マシン

- Mac mini M4 Pro (64GB)
- MBP M4 Max (128GB)

## 構築手順

> **NOTE**: Pyinfra を動かすには Python が、Python を入れるには Homebrew が必要。
> Pyinfra 自身を Pyinfra で導入できないこの**前提依存の連鎖**を解消するため、Step 1 だけは `bootstrap.sh` で手動実行する。

### Step 1: 手動準備 （PyInfra 実行前）

#### bootstrap.sh を実行（各マシンで 1 回、手動）

実体: [`scripts/bootstrap.sh`](../../scripts/bootstrap.sh)（Homebrew → Python 3.12 → uv → Pyinfra の順で導入）

```bash
# 各マシンで実行
chmod +x scripts/bootstrap.sh
./scripts/bootstrap.sh
```

> **NOTE（/opt 所有権）**: macOS の `/opt` は root 所有のため、一般ユーザーでは `/opt/taka-ma` / `/opt/taka-ma-env` を作成できない。`bootstrap.sh` は内部で `sudo mkdir -p /opt/taka-ma /opt/taka-ma-env && sudo chown -R "$(whoami)" ...` を実行して所有権を用意する（**sudo パスワードを求められる**）。この準備が無いと後続の venv 作成・`files.directory` が `Permission denied` で失敗する。

### Step 2: PyInfra実行 (共通基盤を構築)

各マシン上で**ローカル実行**する（`@local`）。SSH クラスタ（02）への依存を避けるため、共通基盤は各機が自分自身に適用する。

```bash
# 各マシンで実行（自マシンに適用）
pyinfra -y @local pyinfra/deploys/common.py
```

> **NOTE（実行モデル）**: 旧版は `pyinfra mac-mini ...` / `pyinfra mbp ...` と記載していたが、これはホストを定義した**インベントリ**が前提で、かつ制御ノード→対象機の SSH（02 の成果）を必要とする。共通基盤（01）を SSH 確立前に通すため、本手順は各機ローカルの `@local` 実行に統一する。制御ノードから遠隔一括適用したい場合はインベントリ整備後に 02 完了を前提として行う。

[`pyinfra/deploys/common.py`](../../pyinfra/deploys/common.py) が下記を冪等に実行する:

| 内容 | 実装 |
|------|------|
| Brewfile 相当パッケージ一括インストール（python@3.12 / uv / git / jq / curl / wget / ollama / iperf3 / htop / btop / node / gh / tmux） | `brew.packages` |
| Tailscale インストール | `brew.casks` |
| Python 仮想環境 `/opt/taka-ma-env` 作成（既存ならスキップ） | `server.shell` |
| ollama サービス起動・有効化 | `server.shell`（`brew services start ollama`）※ pyinfra 3.x の `brew` には `service`/`services` 操作が無いため `server.shell` で起動・自動起動登録する |
| `/opt/taka-ma/{config,logs,data,models}` 作成 | `files.directory` |

> **NOTE**: 各コンポーネント固有の Python パッケージは、当該コンポーネントの `pyinfra/deploys/*.py` で `pip.packages` により個別管理する（コンポーネント単位で依存を明確化、バージョン固定）。

## 手動補足

PyInfra ではカバーできない作業。

### スリープ無効化（両マシン）

自律型開発環境は常時稼働が前提のため、両マシンでスリープを無効化しておくことを推奨する。

```bash
sudo pmset -a sleep 0
sudo pmset -a disablesleep 1

# 確認
pmset -g | grep sleep
# → sleep 0 であること
```

## 検証項目

> **検証概要**: 本検証は、両マシンに共通基盤（Homebrew / Python 3.12 / uv / pyinfra / ollama・ディレクトリ階層・スリープ抑止）が期待通り整備され、以降の各コンポーネント構築の前提条件が満たされていることを確認する。失敗時は当該項目の Step に戻って原因を切り分ける。

- [ ] `brew --version` が動作する
- [ ] `python3.12 --version` が動作する
- [ ] `uv --version` が動作する
- [ ] `pyinfra --version` が動作する
- [ ] `brew services list` で ollama が started
- [ ] `ollama list` が動作する
- [ ] `/opt/taka-ma/` 以下のディレクトリ（config / logs / data / models）が存在する
- [ ] Python 仮想環境 `/opt/taka-ma-env` が有効化できる
- [ ] 両マシンで `pmset -g | grep sleep` が `sleep 0` であること
