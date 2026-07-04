# Security Policy

## 日本語

taka-ma は個人による実験的プロジェクトであり、**無保証・ベストエフォート**での対応です（[LICENSE](LICENSE) の AS-IS 条項に従います）。修正・対応を保証するものではありませんが、報告は確認します。

### 脆弱性の報告

- GitHub の **Security Advisories**（リポジトリの Security タブ →「Report a vulnerability」）を推奨します。公開前に非公開でやり取りできます。
- 公開リポジトリへの**秘匿情報の混入**（鍵・トークン・個人情報など）を見つけた場合も、同じ経路でご連絡ください。

### 前提

- 本リポジトリは**モデルの重み（weights）や API キーを同梱していません**。認証情報は各利用者が自身の環境で管理します。
- マシン間通信は SSH のみ・外部通信は Private Slack channel に限定する設計です。詳細は [設計書](docs/design/design-development-system.md) を参照してください。

---

## English

taka-ma is a personal, experimental project provided **AS-IS with no warranty**; handling is best-effort only. Fixes are not guaranteed, but reports will be reviewed.

### Reporting a vulnerability

- Preferred: GitHub **Security Advisories** (repository Security tab → "Report a vulnerability"), which allows private disclosure.
- If you find **secrets** (keys, tokens, personal data) accidentally committed to the public repository, please report them the same way.

### Notes

- This repository ships **no model weights and no API keys**. Credentials are managed by each user in their own environment.
- By design, inter-machine traffic is SSH-only and external communication is limited to a private Slack channel. See the [design docs](docs/design/design-development-system.md).
