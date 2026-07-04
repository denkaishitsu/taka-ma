# 02. SSH / デュアルモード接続設定

## 目次

- [概要](#概要)
- [接続モード](#接続モード)
- [設計判断](#設計判断)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: 手動準備（PyInfra 実行前）](#step-1-手動準備pyinfra-実行前)
  - [Step 2: PyInfra実行 (SSH トンネルを構築)](#step-2-pyinfra実行-ssh-トンネルを構築)
  - [Step 3: PyInfra 実行後 （手動実行）](#step-3-pyinfra-実行後-手動実行)
- [動作確認](#動作確認)
  - [1. Tailscale 疎通](#1-tailscale-疎通)
  - [2. 10GbE 帯域テスト](#2-10gbe-帯域テスト)
  - [3. 双方向 SSH 疎通](#3-双方向-ssh-疎通)
- [検証項目](#検証項目)

## 概要

Mac mini (司令塔) と MBP (実行機) を、在宅時は 10GbE 直結、外出時は Tailscale VPN で接続する。
SSH 設定は Tailscale IP を使用するため、モード切替時に設定変更は不要。

## 接続モード

```
モード1: 在宅 (10GbE直結 + Tailscale)
  Mac mini ════ 10GbE直結 ════ MBP
  172.16.0.1                   172.16.0.2       ← 直結用（静的IP）
  100.x.x.1                   100.x.x.2        ← Tailscale IP（SSH用）
  → Tailscaleが同一LAN検知 → 10GbE直結パスを自動選択（速度犠牲なし）

モード2: 外出先 (Tailscale VPN)
  Mac mini ──── インターネット ~~~~ MBP
  100.x.x.1                        100.x.x.2   ← Tailscale IP（同一）
  → Tailscaleがリレー/direct接続を自動確立
```

**ポイント**: SSH config は Tailscale IP (100.x.x.x) で固定。在宅/外出を意識する必要がない。

## 設計判断

設計判断は [ADR 0001: SSH / デュアルモード接続の設計判断](../adr/0001-ssh-tunnel-design-decisions.md) を参照。

## 前提条件

- Mac mini M4 Pro / MBP M4 Max が用意されていること
- **[01-common-base.md](01-common-base.md) の Step 1（`bootstrap.sh`）が両マシンで完了していること**（Homebrew + Python + uv + Pyinfra が動作する状態）
- Tailscale アカウント（無料枠で十分）
- （10GbE 直結を使う場合のみ）Mac mini の 10GbE ポート + MBP の Thunderbolt → 10GbE アダプタ + CAT6A ケーブル

## 構築手順

### Step 1: 手動準備（PyInfra 実行前）

#### 1-1. Tailscale ログイン（両マシン、GUI 認証）

Tailscale.app 本体の導入（`brew install --cask tailscale`）と CLI の PATH 公開は **Step 2 の deploy が実施**する。ログイン（GUI 認証）はユーザーが手動で行う。

```bash
# 1) アプリを起動してログイン（GUI 認証）。CLI の `tailscale up` でもよいが、
#    `tailscale` コマンドは Step 2 の deploy が /usr/local/bin に symlink して
#    初めて使える（cask 版は CLI を app バンドル内に閉じ込め PATH に出さないため）。
open -a Tailscale          # 起動 → メニューバーからサインイン
tailscale up              # ← Step 2 の deploy 実行後のみ有効
```

> **NOTE**: `--ssh` フラグは付けない。Tailscale はネットワーク層のみに使用し、SSH 認証は従来の SSH 鍵で行う。

> **CLI が無いとき**: deploy 未実行で `tailscale: command not found` になる場合、バンドル内の実体を直接呼べる: `/Applications/Tailscale.app/Contents/MacOS/Tailscale status`。deploy 実行後は `/usr/local/bin/tailscale` 経由で素のコマンドが通る。

#### 1-2. SSH 鍵の事前生成（マシン別鍵・各マシンで自機の鍵を生成）

クラスタは**マシン別鍵**で相互認証する。各マシンは**自分専用の鍵を自機で生成**し、**秘密鍵は機外に出さない**。相手機へ渡すのは**公開鍵のみ**（公開鍵は秘密情報ではない）。

```bash
# MBP で実行（自機鍵 taka-ma-mbp を生成）
ssh-keygen -t ed25519 -f ~/.ssh/taka-ma-mbp -N "" -C "taka-ma-mbp"

# Mac mini で実行（自機鍵 taka-ma-mac-mini を生成）
ssh-keygen -t ed25519 -f ~/.ssh/taka-ma-mac-mini -N "" -C "taka-ma-mac-mini"
```

生成後、**各マシンで自機の公開鍵の中身を控える**（Step 2 で相手機に `--data peer_pubkey` として渡す）。公開鍵のみで、秘密鍵は渡さない。

```bash
# MBP で
cat ~/.ssh/taka-ma-mbp.pub        # → Mac mini の deploy に peer_pubkey として渡す
# Mac mini で
cat ~/.ssh/taka-ma-mac-mini.pub   # → MBP の deploy に peer_pubkey として渡す
```

> **NOTE（鍵をリポジトリに置かない）**: 旧版は `pyinfra/keys/taka-ma-cluster` という単一共有鍵をリポジトリに生成し両機へ配布していたが、(1) 1 つの秘密鍵が両機に存在し片方漏洩で両機分が漏れる、(2) 個別失効ができない、(3) gitignore 漏れで秘密鍵をコミットする危険、の問題がある。マシン別鍵にして秘密鍵は各機にとどめ、公開鍵だけを交換する。

#### 1-3. 物理ケーブル接続（10GbE 直結用、オプション）

10GbE 直結は **在宅時の高速化用** であり必須ではない（Tailscale VPN だけで通信は成立する）。使う場合は次の物品が必要:

| 物品 | 仕様 | 用途 |
|------|------|------|
| Thunderbolt → 10GbE アダプタ | OWC / CalDigit / QNAP 等 | MBP 側の Ethernet 接続 |
| CAT6A ケーブル | 10Gbps 対応、1-2m | 直結ケーブル |

接続手順:

1. Mac mini の 10GbE ポートに CAT6A ケーブルを接続
2. MBP に Thunderbolt → 10GbE アダプタを接続し、ケーブルをつなぐ

### Step 2: PyInfra実行 (SSH トンネルを構築)

SSH 確立前に動かすため、共通基盤（01）と同様 **各マシン上でローカル実行**（`@local`）する。相手機の情報（エイリアス・Tailscale ホスト・ユーザー）は `--data` で渡す。**sshd 変更に sudo パスワード入力が要るため実ターミナルで実行**し、相手機の Tailscale IP は先に `tailscale ip -4` で確認する。

> **NOTE**: `pyinfra` に global `--sudo` は付けない（`~/.ssh` 配下まで root 所有になり破綻する）。sshd 変更が必要な箇所だけ deploy 側で昇格するので、実行中に sudo パスワードを尋ねられたら入力する。

相手機の `peer_host` は相手機で `tailscale ip -4`、`peer_pubkey` は相手機で Step 1-2 で控えた公開鍵（`taka-ma-<相手role>.pub` の中身）を渡す。

> **NOTE（初回は CLI 未露出）**: 初回 deploy 前は `tailscale` コマンドがまだ PATH に無い。相手機の IP はバンドル内の実体で確認する: `/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4`。deploy 実行後は素の `tailscale ip -4` が通る。

```bash
# MBP（相手 = Mac mini）
pyinfra @local \
  --data role=mbp --data peer_alias=mac-mini \
  --data peer_host=<Mac mini の Tailscale IP> --data peer_user=hmt \
  --data peer_pubkey="<Mac mini の taka-ma-mac-mini.pub の中身>" \
  pyinfra/deploys/ssh_tunnel.py

# Mac mini（相手 = MBP）
pyinfra @local \
  --data role=mac-mini --data peer_alias=mbp \
  --data peer_host=<MBP の Tailscale IP> --data peer_user=youruser \
  --data peer_pubkey="<MBP の taka-ma-mbp.pub の中身>" \
  pyinfra/deploys/ssh_tunnel.py
```

このコマンドで [`pyinfra/deploys/ssh_tunnel.py`](../../pyinfra/deploys/ssh_tunnel.py) が冪等に適用される（実装の詳細は当該ソースとそのコメントを参照）。Tailscale 専用構成（10GbE 直結・静的 IP は使わない）で、適用後に得られる状態は次の通り:

- Tailscale が導入済み（既存アプリには触れない）。
- 相手機の公開鍵が `~/.ssh/authorized_keys` に登録される（相手機は自分の秘密鍵で接続可能）。
- `ssh <相手 alias>` で相手機へ接続できる（IdentityFile=自機鍵。設定は既存 `~/.ssh/config` を壊さず Include で追加）。
- sshd がパスワード認証無効・root ログイン禁止・公開鍵のみに設定される。設定は専用ドロップイン `/etc/ssh/sshd_config.d/taka-ma-cluster.conf` に置き、**既存 `/etc/ssh/sshd_config` 本体は一切変更しない**（macOS 既定の `Include /etc/ssh/sshd_config.d/*` がドロップインを読み込む）。deploy は既定 Include の存在のみ確認し、無ければ fail-fast する（その場合のみ手動で `Include /etc/ssh/sshd_config.d/*` を `/etc/ssh/sshd_config` に追記）。

> ソースは `pyinfra/deploys/` 配下が正本。手順書は**操作コマンドと得られる状態**を示し、実装はソースへリンクする（重複させない）。

> **認証方式**: 2 台クラスタは**マシン別鍵**（`taka-ma-mbp` / `taka-ma-mac-mini`）で相互認証する。各マシンは自機の秘密鍵を保持し、**相手機の公開鍵のみ**を自身の `authorized_keys` に登録する。相手機は自分の秘密鍵で本機へ接続できる。秘密鍵は機外に出ず、片方漏洩時も相手機は無事・個別失効が可能。従来の `ssh-copy-id`（パスワード認証で公開鍵を送る手順）は不要で、パスワード認証は sshd ドロップインで無効化する。

### Step 3: PyInfra 実行後 （手動実行）

#### 3-1. Tailscale ACL 設定（オプション、Web Console）

Tailscale Admin Console でアクセス制御:

```json
{
  "acls": [
    {"action": "accept", "src": ["tag:taka-ma-cluster"], "dst": ["tag:taka-ma-cluster:*"]}
  ],
  "tagOwners": {
    "tag:taka-ma-cluster": ["autogroup:admin"]
  }
}
```

## 動作確認

### 1. Tailscale 疎通

```bash
# 両マシンで IP 確認
tailscale ip -4  # → 100.x.x.x が返る

# Mac mini → MBP
ping $(tailscale ip -4)  # MBPのTailscale IP

# MBP → Mac mini
ping $(tailscale ip -4)  # Mac miniのTailscale IP
```

### 2. 10GbE 帯域テスト

> **NOTE**: 本検証は Step 1-3 で **10GbE 有線直結ケーブルを接続した場合のみ** 実施する。Tailscale VPN のみで運用する場合はスキップ。

```bash
# 疎通
ping 172.16.0.2   # Mac mini → MBP
ping 172.16.0.1   # MBP → Mac mini

# 帯域
# MBP 側
iperf3 -s
# Mac mini 側
iperf3 -c 172.16.0.2
# 期待値: ~9.4 Gbps
```

### 3. 双方向 SSH 疎通

```bash
# === Mac mini → MBP ===
ssh mbp "hostname && uname -a"          # Tailscale 経由
ssh mbp-direct "hostname && uname -a"   # 10GbE 直結

# === MBP → Mac mini ===
ssh mac-mini "hostname && uname -a"
ssh mac-mini-direct "hostname && uname -a"

# === Tailscale 接続パス確認 ===
tailscale status
tailscale ping mbp
# → "via 172.16.0.2" が出れば10GbE直結パスが使われている
```

## 検証項目

> **検証概要**: Mac mini ↔ MBP 間で SSH トンネルが期待通り構築されていることを確認する。

- [ ] Tailscale が両マシンで稼働し、同一 tailnet に参加している
- [ ] Tailscale IP 同士で ping が通る
- [ ] 物理ケーブル接続確認（10GbE）
- [ ] 静的IP設定完了（172.16.0.1 / 172.16.0.2）
- [ ] iperf3 帯域テスト（~9.4 Gbps）
- [ ] Mac mini → MBP: `ssh mbp` でログイン成功
- [ ] MBP → Mac mini: `ssh mac-mini` でログイン成功
- [ ] `ssh mbp-direct` / `ssh mac-mini-direct` でログイン成功
- [ ] `tailscale ping` で在宅時に direct path が選択されていること
- [ ] パスワード認証が無効であること（`ssh -o PasswordAuthentication=yes` で拒否される）
- [ ] `/etc/ssh/sshd_config.d/taka-ma-cluster.conf` が存在すること
- [ ] 既存の `/etc/ssh/sshd_config` が壊れていないこと
- [ ] MBP外出時に `ssh mbp` で接続可能（Tailscale VPN経由）
- [ ] WiFi経由のインターネット接続に影響がないこと
