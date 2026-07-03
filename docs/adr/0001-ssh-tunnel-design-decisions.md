# ADR 0001: SSH / デュアルモード接続の設計判断

**Status**: Accepted

**Context**: Mac mini (司令塔) と MBP (実行機) を、在宅時は 10GbE 直結、外出時は Tailscale VPN で接続する。SSH 設定の方針、認証方式、セキュリティ設定について判断が必要。

**実装**: [構築手順書 02-ssh-tunnel.md](../procedures/02-ssh-tunnel.md)

## 判断一覧

| 判断 | 内容 | 理由 |
|------|------|------|
| Tailscale の役割 | ネットワーク層（VPNトンネル）のみ | `--ssh` を使うと Pyinfra 等の互換性リスク。認証は SSH 鍵に分離 |
| SSH 認証 | 従来の SSH 鍵（ed25519） | Pyinfra, scp, rsync 等の全ツールとの互換性を確保 |
| sshd_config | Include 方式で追加設定 | 既存設定の上書きを防止 |
| ListenAddress | 制限しない | Tailscale IP + 10GbE IP + localhost の複数経路があるため。パスワード認証無効 + 鍵認証のみで保護 |
| SSH 鍵 | 双方向 | Mac mini → MBP (プロセス制御) + MBP → Mac mini (qu-e 監査レポート等) |

## Consequences

- **Pro**: Pyinfra, scp, rsync 等の標準 SSH ツールがそのまま使える
- **Pro**: 在宅・外出のモード切替時に SSH config を変更不要（Tailscale IP で固定）
- **Con**: Tailscale の SSH 機能 (`--ssh`) は使わないため、Tailscale Admin Console での SSH ログ監視は不可
- **Con**: 初回の SSH 鍵交換 (`ssh-copy-id`) は手動でパスワード認証が必要
