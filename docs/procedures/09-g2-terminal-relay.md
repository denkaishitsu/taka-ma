# 09. G2 ターミナルリレー（Even Realities AR グラス・第 1 段）

## 目次

- [概要](#概要)
- [実行場所](#実行場所)
- [前提条件](#前提条件)
- [構築手順](#構築手順)
  - [Step 1: even-terminal の導入（MBP）](#step-1-even-terminal-の導入mbp)
  - [Step 2: Slack MCP コネクタの認可（MBP・手動）](#step-2-slack-mcp-コネクタの認可mbp手動)
  - [Step 3: リレー用プロジェクトの配置（MBP）](#step-3-リレー用プロジェクトの配置mbp)
  - [Step 4: even-terminal の起動（MBP）](#step-4-even-terminal-の起動mbp)
  - [Step 5: Even app との接続（スマホ・手動）](#step-5-even-app-との接続スマホ手動)
- [動作確認](#動作確認)
- [検証項目](#検証項目)
- [アンインストール](#アンインストール)

## 概要

G2（Even Realities AR グラス）＋ R1 リングを第 2 の人間インターフェースとして追加する。方式・契約は [設計書 §8.17](../design/design-development-system.md#817-g2even-realities-ar-グラスチャネル--claude-リレー方式) が正本。

本手順書は **第 1 段（リレー方式）** を構築する。even-terminal 上の Claude セッション（リレー Claude）が、発話を Slack MCP で u-zu 宛の会話面へ原文投稿し、返信をグラス向けに要約表示する。計画確認・Tier 3 承認の決着は選択 UI（`AskUserQuestion`）で受け、u-zu のボタンと同一のファイル契約で代行する。

sa-ru 側の改修は無い（既存の会話・承認ファイル契約に相乗りする）。**u-zu は受信入口の正規化（設計書 §8.3）のみ改修が要るため、[03-slack-bot.md](03-slack-bot.md) の再デプロイが前提**になる。Slack がアプリ経由投稿の本文末尾へ付けるアプリ帰属表記を除去し、sa-ru へ原文だけを渡すための処理である。

## 実行場所

MacBook Pro M4 Max（実行機）。Step 4 のみスマホ（Even app）。

## 前提条件

- [01-common-base.md](01-common-base.md) の共通基盤（Homebrew / Node.js）が MBP に構築済み
- [02-ssh-tunnel.md](02-ssh-tunnel.md) の SSH 双方向疎通が確立している（MBP から `ssh mac-mini` が通る）
- [03-slack-bot.md](03-slack-bot.md) の u-zu が稼働している（Bot への DM が会話キューへ流れる状態）。**上り発話の正規化（設計書 §8.3）を含む版が配備済みであること**。未配備だと発話末尾にアプリ帰属表記が残り、訂正の簡易記法が機能しない
- Claude Code CLI が MBP に導入・ログイン済み（`which claude` で確認）
- Claude Code のユーザー設定に Slack MCP コネクタ（`https://mcp.slack.com/mcp`）が登録済み。**投稿権限まで認可されていること**（Step 2 で確認・是正する）
- G2 グラスと R1 リングが Even app（iOS/Android）とペアリング済み
- Tailscale が MBP とスマホの両方でログイン済み（外出先でも接続を維持するため。宅内 LAN のみで使う場合は不要）

## 構築手順

### Step 1: even-terminal の導入（MBP）

```bash
npm install -g @evenrealities/even-terminal

# 確認
even-terminal --version
```

> npm グローバル導入のため pyinfra 管理外。導入・撤去は本手順書のコマンドを正とする。

### Step 2: Slack MCP コネクタの認可（MBP・手動）

上りの主経路は Slack MCP（`https://mcp.slack.com/mcp`）の `slack_send_message` による原文投稿である。**Slack MCP がクライアントへ露出するツールは、認可されたスコープに応じて決まる**（Slack 公式ドキュメント明記）。読取スコープだけで認可すると `slack_read_channel` / `slack_read_thread` の 2 種しか現れず、投稿できない。

| 必要な操作 | 必要スコープ（user token） |
|---|---|
| メッセージ投稿（上りの主経路） | `chat:write` |
| チャンネル / スレッド読取（下り） | `channels:history` / `groups:history` / `mpim:history` / `im:history` |
| チャンネル検索（会話面の選択） | `search:read.public` ほか |

```bash
# 登録と接続状態の確認
claude mcp list
# → slack: https://mcp.slack.com/mcp (HTTP) - ✔ Connected
```

接続できていても、Claude Code セッションで `slack_send_message` が使えなければスコープ不足である。

#### 認可のやり直し（人手・ブラウザ）

Slack MCP は **Dynamic Client Registration に対応していない**ため、Claude Code の自動 OAuth では接続できない。登録済み Slack アプリ（内部アプリで可）の **user token** を発行し、`Authorization: Bearer` ヘッダとして登録する方式を採る。

1. <https://api.slack.com/apps> で当該アプリを開く
2. **OAuth & Permissions** → **User Token Scopes（ユーザートークンのスコープ）** の表で `chat:write` の行を探す
   - 表の左端の列は **「必須」** のチェックボックスで、「はい」「いいえ」は**現在の状態表示**である（チェックすると「いいえ」→「はい」に変わる）
   - スコープが一覧に載っていても**「いいえ」（任意）のままでは発行済みトークンに含まれない**。`chat:write` の「必須」をチェックして「はい」にする
   - 既存のチェック済みスコープは変更しない。行末のゴミ箱アイコンはスコープ削除であり、押すと下りの読取が壊れる
3. 保存後、**OAuth Tokens** セクションの **Reinstall（再インストール）**を実行して再認可する（スコープはトークンに焼き込まれるため、再認可しないと増えない）
   - **トークン文字列は変わらないことがある**（Slack はローテートせず既存トークンにスコープを追加する挙動が既定）。値が同じでもスコープは増えているので、その場合は次の手順 4 は**不要**
4. トークン値が変わった場合のみ、Claude Code の登録を差し替える

```bash
claude mcp remove slack -s user
claude mcp add --transport http slack https://mcp.slack.com/mcp \
  --header "Authorization: Bearer $SLACK_MCP_TOKEN"
```

> トークンは環境変数（`.zshrc` / Keychain）から渡し、コマンド行に直書きしない。トークン値をログ・チャット・コミットへ出さないこと。

5. Claude Code を再起動し、`slack_send_message` が使えることを確認する

#### Step 2-2: Slack トークンを Keychain へ登録（人手）

リレーの中継は Slack Web API を直接叩く 1 本のスクリプト（`scripts/relay.sh`）で行う（設計書 §8.17。MCP のツールを 1 個ずつ呼ぶと呼び出しごとに LLM 推論が挟まり会話にならない）。そのため MBP 側にトークンが要る。**平文ファイルには置かず Keychain に入れる**（コミット・rsync・OSS 公開のいずれでも漏れない）。

```bash
# 対象アプリの User OAuth Token（xoxp-）を貼り付けて Enter（画面には表示されない）
security add-generic-password -s taka-ma-g2-relay -a slack -w

# 確認（値は出力されない）
security find-generic-password -s taka-ma-g2-relay -a slack -w > /dev/null && echo OK || echo NG
```

> トークンは **User OAuth Token（`xoxp-`）**。Bot Token（`xoxb-`）ではリレーがオーナー本人として投稿できず、u-zu の認可で弾かれる。

> **認可をやり直せない場合**: リレーは退避経路（`scripts/say.sh`）で動作する。会話・タスク実行は成立するが、**G2 からの発話が Slack に残らない**（Slack 側は sa-ru の返信だけが並ぶ）。恒久運用では投稿権限を認可すること。

### Step 3: リレー用プロジェクトの配置（MBP）

リレー Claude の振る舞い（会話面の選択・原文投稿・要約表示・選択 UI による決着・SSH 直読）は、リレー用プロジェクトの CLAUDE.md と補助スクリプトで定義する。定義の正本はリポジトリ [`src/g2_relay/`](../../src/g2_relay/CLAUDE.md)（仕様は [設計書 §8.17](../design/design-development-system.md#817-g2even-realities-ar-グラスチャネル--claude-リレー方式) 第 1 段）。

```bash
mkdir -p ~/DevDev/g2-relay
# --delete は正本に無いファイルを消す。配備先にしか存在しないもの
# （relay.env・ローカル権限設定・even-terminal のログ）は必ず除外する。
rsync -a --delete \
  --exclude relay.env \
  --exclude '.claude/settings.local.json' \
  --exclude '*.log' \
  ~/DevDev/taka-ma/src/g2_relay/ ~/DevDev/g2-relay/

# 確認: CLAUDE.md / relay.env.example / .claude/settings.json /
#       scripts/(relay.sh, list.sh, pending.sh, watch.sh, say.sh, decide.sh, read_done.sh) が配置されていること
ls -R ~/DevDev/g2-relay/
```

> **除外を省くと配備先のローカルファイルが消える**。`.claude/settings.local.json`（このマシン固有の Read 許可）と even-terminal のログは正本に無いため、除外しないと再同期のたびに削除される。

#### Step 3-2: relay.env の記入（初回のみ）

```bash
cp ~/DevDev/g2-relay/relay.env.example ~/DevDev/g2-relay/relay.env
# エディタで実値を記入:
#   OWNER_USER_ID    … オーナーの Slack user_id（U...）。決着の decided_by 記録に使用
#   SLACK_DM_CHANNEL … u-zu Bot との DM チャンネル ID（D...）。既定の会話面
#   TEAM_ID          … Slack ワークスペースの team_id（T...）。conversation_id 導出に使用
```

> ID の確認方法: Slack アプリで u-zu との DM を開き、チャンネル詳細からコピーする（トークン等の機密は含まない）。

### Step 4: even-terminal の起動（MBP）

```bash
cd ~/DevDev/g2-relay
PROJECT_DIR=$HOME/DevDev/g2-relay even-terminal --provider claude --tailscale --token <固定トークン>
```

- **`PROJECT_DIR` の指定が必須**。Even app は過去セッションの場所を憶えて `cwd` を送り、それが最優先される（`cd` も `--cwd` も上書きできない）。`PROJECT_DIR` を与えるとアプリのセッション一覧がそのディレクトリだけに絞られ、別プロジェクトを選べなくなる。指定しないと**無関係なディレクトリでセッションが立ち、リレー契約を読まない素の Claude が動く**（実機で発生）
- **`~/DevDev/g2-relay` に cd してから起動する**（New Session が cwd 未指定のときの既定になる）。Even app の New Session は cwd を指定せず、その場合セッションの cwd は「even-terminal を起動したディレクトリ」になる（0.8.1 実測。`--cwd` はバナー表示とセッション一覧の既定にのみ使われ、新規セッションの cwd には**効かない**）。別ディレクトリから起動するとリレー契約（CLAUDE.md）を読まない素の Claude セッションが立つ
- `--tailscale`: Tailscale アドレスにバインドする（外出先でも同一 URL で接続可能）
- `--token`: 任意の固定文字列（自分で決める接続パスワード）。省略すると再起動のたびにローテートし、Even app 側の再登録が必要になる
- `--expose pinggy` / `--expose bore` / `--expose ngrok`（公開トンネル）は**使用禁止**（設計書 §8.1 / §8.17 の通信原則）

起動すると接続 URL・トークン・QR コードが端末に表示される。

### Step 5: Even app との接続（スマホ・手動）

1. スマホの Even app を開く
2. Step 4 で表示された QR コードをスキャン（または URL とトークンを手入力）
3. G2 グラスに Claude セッションの画面が表示されることを確認する
4. New Session 後の最初の返答で、リレーとして応答している（依頼を実行せず中継する旨を返す）ことを確認する。**素の Claude として直接回答してきた場合は cwd 違いの兆候**なので、Step 4 の cd からやり直す

## 動作確認

```bash
# 1. サーバ起動確認（MBP）
# 起動ログに接続 URL / トークン / QR コードが表示されていること

# 2. SSH 直読経路の確認（MBP → Mac mini）
ssh mac-mini "ls /opt/taka-ma/data/tasks/done/ | head"
# → エラーなく一覧が返ること
```

3. G2 から短い発話を入力 → u-zu との DM に**原文のまま**投稿され、会話キュー（Mac mini `/opt/taka-ma/data/conversations/`、処理後は `done/`）に同じ text で着信すること
4. sa-ru の会話返信が同じ会話面に届き、G2 に要約表示されること
5. 自由発話（例:「着手します」）で決着が発火しない（通常の会話として中継される）こと

## 検証項目

- [ ] `even-terminal --version` が成功する
- [ ] Slack MCP で `slack_send_message` が使える（使えない場合は退避経路で動作することを確認し、投稿権限の認可を残課題として記録する）
- [ ] G2 に Claude セッションが表示され、R1 リングで操作できる
- [ ] 発話が Slack の会話面に原文で投稿され、u-zu 経由で会話キューに着信し、sa-ru の返信が同じ会話面へ返る
- [ ] 会話開始 → 計画確認 → 着手 → 進捗/完了受領 が**スマホの Slack アプリを開かずに**完結する
- [ ] 計画確認で選択 UI（着手 / やり直す / Slack で確認する）がグラスに出て、リングで選択できる
- [ ] 「着手」の**選択**でのみ確認レコード（§8.10b）が confirmed になり、`decided_by` が `g2:` 接頭辞で記録される
- [ ] 自由発話・選択 UI の 120 秒放置（skip）では exec-confirmations / approvals のレコードが書き換わらない
- [ ] 長文の完了結果を `/opt/taka-ma/data/tasks/done/` から SSH 直読で要約表示できる

## アンインストール

```bash
# even-terminal の撤去（MBP）
npm uninstall -g @evenrealities/even-terminal

# リレー用プロジェクトの撤去（MBP）
rm -rf ~/DevDev/g2-relay
```

> u-zu / sa-ru 側に本手順書由来の変更は無いため、撤去はこの 2 点で完結する。
