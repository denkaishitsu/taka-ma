# Appendix A. Slack 設定 完全手順（u-zu / 03-slack-bot）

[03-slack-bot.md](03-slack-bot.md) の Step 1（Slack App 作成）・Step 3（トークン配置・チャンネル・Owner 登録）を、**知識ゼロでも順番通りに辿れば全ての値が揃う**よう、Slack 管理画面のクリック単位まで分解した完全版。

---

## 0. ゴール — 最終的に集める「4 つの値」

u-zu（Slack Bot）を起動するには、下表の **4 値**を集めて Mac mini に置けばよい。**この 4 つが揃うことが T03 の Slack 側ゴール**。各値は決まったステップでしか発行されない（だから途中では「まだ無い」のが正常）。

| # | 値の例 | 名前 | どのステップで発行 | 最終的な置き場所 |
|---|--------|------|------------------|----------------|
| 1 | `xapp-1-...` | App-Level Token | Step 3（Socket Mode） | `.env` の `SLACK_APP_TOKEN` |
| 2 | `xoxb-...` | Bot User OAuth Token | **Step 7（Install 後に初めて発行）** | `.env` の `SLACK_BOT_TOKEN` |
| 3 | `C0XXXXXXX` | Channel ID | Step 9（チャンネル作成後） | `.env` の `SLACK_CHANNEL_ID` |
| 4 | `U0XXXXXXX` | あなたの Slack user ID | Step 10（プロフィールから） | `users.yaml` の owner |

> 控えるときは手元のメモ帳に「番号＝値」で貼っておく。**4 つ揃ってから**最後に一括で Mac mini に書き込む（Step 11）。値は Anthropic（私）には貼らない。

---

## 1. 前提

- Slack ワークスペースの管理者アカウントでブラウザにログイン済み
- Mac mini 側は deploy 済み（u-zu のコード・launchd・`.env.example` 配置済み。トークン未配置で起動待ち状態）

---

## 2. App を作成（値は発行されない）

1. https://api.slack.com/apps を開く
2. 右上「**Create New App**」→「**From scratch**」
3. **App Name**: `taka-ma`
4. **Pick a workspace**: 対象ワークスペースを選択 →「**Create App**」
5. 「Basic Information」画面に遷移する。以降、左サイドバーで各ページを開く。

> このあと開く URL の `apps/` の直後があなたの **App ID**。直リンクは `https://api.slack.com/apps/＜App ID＞/...`。

---

## 3. Socket Mode を有効化 →【値1: `xapp-` を取得】

1. 左サイドバー「**Settings**」内の「**Socket Mode**」（単独リンク。直リンク `.../socket-mode`）
2. 「**Enable Socket Mode**」トグルを **ON**
3. App-Level Token 生成ダイアログ:
   - **Token Name**: `taka-ma-socket`（ただのラベル。後から変更不可だが機能に無関係）
   - **Add Scope**: `connections:write`
   - 「**Generate**」
4. 表示された **`xapp-...`** を【値1】として控える（この画面でしか全文表示されない）

> 補足: Token Name は見分け用のラベルにすぎない。`.env` のキー名 `SLACK_APP_TOKEN` の方はコード固定で変更不可。

---

## 4. Bot Token Scopes を 9 個追加（値は発行されない）

1. 左サイドバー「**Features → OAuth & Permissions**」
2. 下方「**Scopes → Bot Token Scopes**」→「**Add an OAuth Scope**」で 9 個を 1 つずつ:

| Scope | 用途 |
|-------|------|
| `chat:write` | メッセージ送信 |
| `chat:write.customize` | Bot 名/アイコンのカスタマイズ |
| `groups:history` | Private Channel のメッセージ読取 |
| `groups:read` | Private Channel 情報取得 |
| `app_mentions:read` | @メンション検知 |
| `im:history` | DM メッセージ読取 |
| `commands` | スラッシュコマンド |
| `files:write` | ファイルアップロード |
| `reactions:write` | リアクション追加 |

> **必ず Install（Step 7）より前に**全部入れる。後から足すと再インストールが必要になる。

---

## 5. Event Subscriptions を 3 個（値は発行されない）

1. 左サイドバー「**Features → Event Subscriptions**」
2. 「**Enable Events**」を **ON**（Socket Mode が ON なので **Request URL 欄は出ない＝入力不要**）
3. 「**Subscribe to bot events**」→「**Add Bot User Event**」:

| Event | 用途 |
|-------|------|
| `app_mention` | @taka-ma で呼び出し |
| `message.groups` | Private Channel メッセージ監視 |
| `message.im` | DM 受信 |

4. 右下「**Save Changes**」

---

## 6. Slash Commands を 11 個（値は発行されない）

1. 左サイドバー「**Features → Slash Commands**」
2. 「**Create New Command**」を 11 回。Socket Mode のため **Request URL 不要**（必須エラーが出る場合のみ任意の `https://example.com` を入れる。実配送は WebSocket 経由）:

| Command | Short Description |
|---------|-------------------|
| `/taka-ma-task` | 相談を開始 |
| `/taka-ma-go` | 会話を締めて着手 |
| `/taka-ma-status` | システム状態を確認 |
| `/taka-ma-approve` | 承認リクエストに応答 |
| `/taka-ma-stop` | 緊急停止 |
| `/taka-ma-start` | サービス復旧 |
| `/taka-ma-ollama-stop` | ollama 手動停止 |
| `/taka-ma-logs` | ログを取得 |
| `/taka-ma-blender` | Blender モード切替 |
| `/taka-ma-user` | ユーザー管理 |
| `/taka-ma-model` | モデル管理 |

3. 各コマンドで「**Save**」

---

## 7. ワークスペースへインストール →【値2: `xoxb-` を取得】

1. 左サイドバー「**Settings → Install App**」（または「OAuth & Permissions」上部）
2. 「**Install to Workspace**」→ 権限確認 →「**Allow**」
3. インストール後、「**OAuth & Permissions**」ページ上部「**OAuth Tokens for Your Workspace**」に
   **「Bot User OAuth Token」`xoxb-...`** が出現 → 【値2】として控える

> 【値2】はインストール前には**存在しない**。ここで初めて発行される。

---

## 8. （任意）アイコン・表示名の設定

「Basic Information → Display Information」で App アイコン/説明を設定可（任意）。

---

## 9. Private Channel 作成 ＋ Bot 招待 →【値3: Channel ID】

1. Slack で Private Channel を作成（例 `taka-ma`、🔒 Private）
2. チャンネル内で `/invite @taka-ma` を実行し Bot を招待
3. **Channel ID 取得**: チャンネル名クリック →「チャンネル詳細」最下部の **`C0XXXXXXX`** → 【値3】として控える
   - 別法: ブラウザでチャンネルを開き URL 末尾 `.../C0XXXXXXX`

---

## 10. 自分の Slack user ID →【値4: `U0...`】

1. Slack で自分のアイコン → プロフィール →「**︙（その他）**」→「**メンバー ID をコピー**」
2. **`U0XXXXXXX`** → 【値4】として控える

---

## 11. 4 値を Mac mini に書き込む（ここで初めて機密を配置）

ここまでで【値1〜4】が手元に揃っているはず。MBP のターミナルから、`< >` を実際の値に置換して実行する。**値は私（Anthropic）には貼らない。**

`.env`（値1〜3）:
```bash
ssh mac-mini "cat >> /opt/taka-ma/config/.env" << 'EOF'
SLACK_BOT_TOKEN=<値2: xoxb-...>
SLACK_APP_TOKEN=<値1: xapp-...>
SLACK_CHANNEL_ID=<値3: C0...>
EOF
ssh mac-mini "chmod 600 /opt/taka-ma/config/.env"
```

`users.yaml`（値4 を owner に）:
```bash
ssh mac-mini "cat > /opt/taka-ma/config/users.yaml" << 'EOF'
# users.yaml — Slack Bot のロールベース認可設定
users:
  <値4: U0...>:
    name: "owner-name"
    role: owner
EOF
ssh mac-mini "chmod 600 /opt/taka-ma/config/users.yaml"
```

テンプレート正本: [`src/slack_bot/config/users.yaml.example`](../../src/slack_bot/config/users.yaml.example)。ロール定義は [`docs/operations/u-zu/slack-bot.md`](../operations/u-zu/slack-bot.md)。

---

## 12. 起動と接続確認（構築者が実行）

4 値の配置が済んだら launchd を再起動して接続を確認:

```bash
ssh mac-mini "launchctl kickstart -k gui/\$(id -u)/com.taka-ma.u-zu"
ssh mac-mini "tail -20 /opt/taka-ma/logs/u-zu.log"   # → 'Connected to Slack'（T03-V2）
```

以降は [03-slack-bot.md](03-slack-bot.md) §検証項目（V2〜V22）に従う。

---

## 進捗チェックリスト（この順で埋まれば完了）

- [ ] Step 2: App `taka-ma` 作成
- [ ] Step 3: Socket Mode ON →【値1 `xapp-`】控えた
- [ ] Step 4: Bot Token Scopes 9 個追加
- [ ] Step 5: Event Subscriptions 3 個
- [ ] Step 6: Slash Commands 11 個
- [ ] Step 7: Install →【値2 `xoxb-`】控えた
- [ ] Step 9: Channel 作成 + 招待 →【値3 `C0...`】控えた
- [ ] Step 10: 自分の【値4 `U0...`】控えた
- [ ] Step 11: `.env` + `users.yaml` 記入（chmod 600）
- [ ] Step 12: launchd 再起動 → `Connected to Slack` 確認
