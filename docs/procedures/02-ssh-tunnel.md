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

`pyinfra` で `brew install --cask tailscale` した後、初回起動と Tailscale アカウントへのログインが必要。

```bash
# GUI から起動してログイン、または CLI で
tailscale up
```

> **NOTE**: `--ssh` フラグは付けない。Tailscale はネットワーク層のみに使用し、SSH 認証は従来の SSH 鍵で行う。

#### 1-2. SSH 鍵の事前生成

PyInfra は `pyinfra/keys/taka-ma-cluster` から鍵を配置するため、事前に生成しておく。

```bash
ssh-keygen -t ed25519 -f pyinfra/keys/taka-ma-cluster -C "taka-ma-cluster"
```

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

```bash
pyinfra mac-mini pyinfra/deploys/ssh_tunnel.py
pyinfra mbp     pyinfra/deploys/ssh_tunnel.py
```

[`pyinfra/deploys/ssh_tunnel.py`](../../pyinfra/deploys/ssh_tunnel.py) が下記を冪等に実行する:

| 内容 | 実装 |
|------|------|
| Tailscale インストール | `brew.casks(casks=["tailscale"])` |
| SSH 鍵の配置（`~/.ssh/taka-ma-cluster`） | `files.put(src="keys/taka-ma-cluster")` |
| SSH config テンプレート展開（ロール別） | `files.template(src="templates/ssh_config_{role}.j2")` |
| sshd_config.d 設定（Include 方式、パスワード認証無効・root ログイン禁止） | `files.template(src="templates/taka-ma-cluster-sshd.conf.j2")` |
| 10GbE 直結用静的 IP 設定 | `server.shell(networksetup -setmanual ...)` |

### Step 3: PyInfra 実行後 （手動実行）

#### 3-1. 初回 ssh-copy-id（双方向、パスワード認証必須）

PyInfra で鍵は配置済だが、相手マシンの `~/.ssh/authorized_keys` への追加は **初回はパスワード認証** で行う必要がある。

`<USER>` は接続先マシンのログインユーザー名、`<TAILSCALE_IP>` は接続先マシンの Tailscale IP（接続先マシンで `tailscale ip -4` を実行、または `tailscale status` で一覧表示して確認）。

```bash
# Mac mini → MBP / 初回、MBP/ユーザーアカウントのパスワードを求められる 
ssh-copy-id -i ~/.ssh/taka-ma-cluster.pub <MBP_USER>@<MBP_TAILSCALE_IP> #例 MBP-User@100.x.x.2 

# MBP → Mac mini / 初回、Mac mini/ユーザーアカウントのパスワードを求められる
ssh-copy-id -i ~/.ssh/taka-ma-cluster.pub <MAC_MINI_USER>@<MAC_MINI_TAILSCALE_IP> #例 Mac-Mini-User@100.x.x.2
```

#### 3-2. Tailscale ACL 設定（オプション、Web Console）

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
