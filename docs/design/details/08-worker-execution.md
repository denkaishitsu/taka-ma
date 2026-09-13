# 詳細設計: worker 実行経路（実行アダプタ）

> **位置づけ**: [設計書本体](../design-development-system.md) の詳細。本書が扱う節: §8.5〜§8.7。
> 障害を契機とする設計改訂の経緯は [revisions/](../revisions/) を参照。

重量タスクを実行する worker CLI の起動・監視・回収と、CLI 固有部分を隔離する実行アダプタ抽象を扱う。アダプタ自体の内部設計は [08-worker-execution-adapters.md](08-worker-execution-adapters.md)。

---

<a id="sec-8-5"></a>
## 8.5 ③ sa-ru → worker CLI（重量タスク実行、実行アダプタ抽象）

agent 実行（旧 heavy）で使用する worker 実行は **3 つの実行アダプタ**に分け、CLI 固有部分をアダプタに隔離する。**特定 worker CLI（Claude Code 等）にロックインしない**ことを最上位制約とする。承認判定は CLI 非依存の中核（`ApprovalPipeline.decide(tool_name/tool_input → allow/deny)`）で共通に行い、各アダプタは「承認要求の取得」と「決定の伝達」だけを自 CLI 形式へ変換する。

> **詳細**: [08-worker-execution-adapters.md](08-worker-execution-adapters.md)（抽象化 seam A/B、実機検証結果、設計判断の根拠）。

**実行 dispatch（seam B・CLI 非依存）**: `_select_method(model_conf.methods)` が worker の `methods` 宣言で実行アダプタを選ぶ（`headless` / `pty`(interactive) / `subprocess`）。新 CLI 追加＝`methods` 宣言＋アダプタ実装のみ。

| アダプタ | 対象 | 承認要求の取得（→ 中核へ） | 決定の伝達 | 起動 |
|---|---|---|---|---|
| **headless** | Claude Code（`methods:[headless]`） | **PreToolUse フック** stdin の `{tool_name, tool_input, tool_use_id}` | フックが `permissionDecision:"allow"` / exit 2 | `claude -p --output-format stream-json --verbose --include-hook-events --settings <hook> --model <flag>`（argv 配列・SSH 経由 MBP） |
| **interactive(pty)** | 汎用対話 CLI（将来 Codex 等。`methods:[pty]`）。**現在これを宣言する登録モデルは無い**（§8.6） | interceptor の**レガシー y/n** 検出（`[y/n]`/`(yes/no)`/`Allow?`）＋ context 抽出。承認要求の信頼境界とフェイルセーフは §3.3 (3) | `WorkerPtyWrapper` が `y`/`n` を stdin 送信 | SSH + pexpect + tmux |
| **subprocess** | ollama / keychain 依存 agy（§8.6/§8.7） | per-tool 承認なし（対象外） | — | 単発 stdin |

**stream-json 読取の行長上限と異常時の後始末（headless アダプタ）:**

- worker 出力（stream-json・1 行 1 イベント）の読取バッファ上限は**既定の 64KB では不足**する: 大きいファイルを読んだ tool_result は 1 行で数百 KB になり、読取側が `Separator is not found, and chunk exceed the limit`（行長超過）で落ちる。この読取エラーが worker の「難所」と誤解釈され、haiku → sonnet → opus の**偽昇格**を起こした（[是正 2026-08-30](../revisions/2026-08-30.md)）。上限を 10MB へ拡大する（値はコード定数でなく設定管理）
- 上限をなお超えた場合は「難所（ESCALATE 相当）」ではなく**読取エラー**として記録し、昇格ラダーの発動理由にしない（インフラ起因をモデル能力と混同しない）
- 読取側の異常終了時は worker の SSH プロセスを確実に終了させる（[是正 2026-08-30](../revisions/2026-08-30.md)）

**worker 起動前の認証プリフライト（headless 起動直前・`AuthPreflight`）:**

worker ホストでのタスク失敗は (1) sa-ru → worker ホストの SSH 断、(2) worker ホスト → git remote の認証・到達性、(3) Anthropic 認証の失効のどの経路でも起き、事後のエラー文からの推測は SSH 認証エラーと Anthropic subscription エラーの混同（登録済み鍵への再作成提案という誤診断）を招いた実績がある。headless アダプタは worker 起動の直前に `AuthPreflight` で 3 経路を依存の浅い順（SSH → git → Anthropic）に検査し、最初の不合格で打ち切って worker を起動しない。

- **判定は exit code のみ**: SSH は `ssh <mbp> true`、git は workspace の `git ls-remote origin HEAD`（stdout 破棄）、Anthropic は worker CLI の最小プローブ（`claude -p ok`・stdout 破棄）。鍵・トークン本体を出力するコマンドは使わない。workspace が git repo でない / origin 未設定（新規 clone 運用）は git 検査の対象外とし不合格にしない。
- **報告は種別＋エラー実出力の該当 1 行**: 不合格時は原因経路（ssh / git / anthropic）と「どの経路の問題で・どの経路の問題ではないか」の切り分け事実、エラー出力の最終行（既知トークン形式は伏字化）を Slack へ通知する。対処提案（鍵の再作成等）は出さない。合格・対象外は無音で通過する。
- **TTL キャッシュ**: PASS は `pass_ttl_sec` 内で再検査しない（多段起動で Anthropic プローブの実推論コストを毎回払わない）。FAIL は `fail_ttl_sec` 内の再検出に cached 印を付け、昇格ラダー再突入時の重複 Slack 通知を抑止しつつ、復旧後の再試行を長く塞がない。運用値は sa-ru.yaml の `preflight` ブロックが唯一の源（コード側に既定値なし）。
- 不合格は例外としてそのまま既存の失敗経路（昇格・failed 決着・Slack 通知）へ乗る。検査自体は CLI 非依存の SSH/git 検査＋アダプタ固有の認証プローブで構成し、headless アダプタ側（Claude 固有経路）から呼ぶ。

**headless アダプタ（Claude Code）の実行フロー:**

```
0. sa-ru: AuthPreflight（SSH → git remote → Anthropic）— 不合格なら worker を起動せず種別明示で Slack 通知
1. sa-ru: workspace(/opt/taka-ma/work/{task_id}) を mkdir → claude -p "<task>" を argv 配列で SSH 起動
2. sa-ru: stream-json を逐次パース（session_id を system/init から取得、tool_use/text を蓄積）
3. 各ツール実行前: PreToolUse フック（MBP）→ SSH（ControlMaster 多重化）→ Mac mini の decide デーモン
   （常駐の中核 decide()＝安全性/スコープ/Tier1/2/3）→ allow/deny
4. 完了: result イベント受信で自己終了 → 結果を取得 → Slack 通知
   （result 無しでプロセス終了 = ハング（v2.1.163+ の5秒 grace kill）→ retry/fallback。無応答スタック検知を本経路に統合）
```

**承認保留時のアダプタ責務（§3.3 (4) / §8.10）:**

保留（hold）は**セッションの復元を前提としない**。アダプタが担うのは「ツールをブロックし、worker の実行実体を確実に畳む」ことだけで、再開に必要な文脈はアダプタの外（タスクファイルと workspace）にある。したがって**どのアダプタも特別な再開機能を要求されない**。

| アダプタ | ブロックの伝達 | worker を畳む手段 |
|---|---|---|
| **headless** | フックが exit 2 | worker はツールを諦めて自ら終了する。終了しない場合も全体上限（`run_timeout_sec`）と `ssh -tt` の SIGHUP 伝播で回収される（下記 資源回収） |
| **interactive(pty)** | `n`（拒否）を stdin 送信 | tmux セッションを kill（無応答で放置しない） |
| **subprocess** | — | per-tool 承認を持たないため保留は発生しない |

- **worker の終了状態を保留の根拠にしない**: ブロックされた worker は「指示どおり終わっただけ」＝ `is_error=false` の正常終了として返る（Claude Code 2.1.220 で haiku / sonnet / opus いずれもツール試行 1 回で終了。[是正 2026-07-27](../revisions/2026-07-27.md)）。判定根拠は §8.10 の承認レコードに一本化する。
- **「答えないこと」を拒否として使わない**: 猶予超過時は必ず能動的にブロックを伝達し、実行実体を畳む。無応答のまま生かしておくと、拒否の成立が worker 側の未応答時の既定動作に依存してしまう。

> ⚠️ **interactive(pty) 経路は現在どの登録モデルからも使われていない**（2026-07-28。`ya-ta.yaml` から `pty` を外した・§8.6）。本経路の承認配線は実機 end-to-end 検証がされておらず（[是正 2026-07-06](../revisions/2026-07-06.md)）、「承認プロンプトに誰も答えなかったとき worker がどう振る舞うか」も未測定であるため、未検証のまま有効にしておかない判断による。アダプタの実装（`WorkerPtyWrapper` / interceptor のレガシー y/n 検出）は将来の対話 CLI 向けに温存する。**再開の条件**: 無応答時の挙動を実測し fail-closed を確認すること。

**承認フックの判定実行系（decide デーモン — Mac mini 常駐）:**

フックは worker と同じ MBP で発火するが、判定中核 `decide()` が要する資源（ya-ta・承認ファイル・pipeline.yaml・監査ログ）は Mac mini 側にある。ツール呼び出しごとに SSH 接続と Python コールドスタート（依存 import・config ロード・SlackNotifier 構築）を払う方式は、承認レイテンシがツール数に比例して累積するため採らず、判定側を常駐プロセスに分離する。

- **decide デーモン（Mac mini・launchd 常駐）**: 起動時に config（ya-ta.yaml / sa-ru.yaml / pipeline.yaml）と `ApprovalPipeline`・`SlackNotifier` を一度だけ構築し、Unix ドメインソケットで判定リクエストを受ける（ポート開放なし＝通信方式 SSH の原則維持）。asyncio で並行処理し、Tier3 の人間待ち（最大 300 秒）が他 worker の判定をブロックしない。config 変更は yaml の mtime 検知で自動再ロード、crash は launchd KeepAlive で自動再起動。
- **フック＝薄いクライアント**: フックコマンドは SSH で Mac mini の decide クライアント（標準ライブラリのみ・venv / PYTHONPATH 非依存）を起動し、フック stdin（`tool_name`/`tool_input`）とタスク文脈（task_id / team_id / channel / thread_ts）をソケットへ渡して allow/deny を受ける。判定依存の import をクライアントから排し、依存解決の失敗でフック自体が壊れる事故を構造的に無くす。
- **fail-closed（全異常を exit 2 へ集約）**: クライアント・SSH・デーモンのどの段の異常（到達不可・タイムアウト・例外）も必ず exit 2（deny）で終える。exit 2 以外の非 0 終了は「フックのエラー」として Claude Code の既定権限評価に落ち、read 系ツールが承認パイプラインを素通りする（fail-open）ため、フックコマンド全体を exit 2 に集約する。

> 詳細（プロトコル・終了コード契約・タイムアウト設計・launchd・計測）は [実行アダプタ設計 §2.1](08-worker-execution-adapters.md#21-判定実行系--decide-デーモンmac-mini-常駐とフックの薄いクライアント化)。

**モデルルーティング保持**: `model_flag`（`--model <name>`）・`command` を各アダプタで保持。headless は argv 配列で組み立て、シェル文字列連結を廃す。

**終了・タイムアウト時の資源回収（リモート孤児・セッションリークの防止）:**

worker は SSH 越しに MBP 上で動く。sa-ru 側でタイムアウトや完了により実行を打ち切るとき、**リモートで動くプロセスとローカルに紐づく資源を確実に破棄する**。取りこぼすと、MBP 上に孤児プロセスや使われないセッションが積み上がり、資源を食い潰す。

- **headless（SSH 越しの `claude -p`）**: タイムアウト時にローカルの SSH クライアントを kill するだけでは、リモートの `claude -p` は切断を知らされず孤児化して走り続ける。SSH に疑似端末を割り当てておき（`-tt`）、セッションが切れたときにリモート側へ SIGHUP が伝播してプロセスが終了するようにする。
- **interactive(pty)**: 切断耐性のため tmux の detached セッション内で CLI を起動する。タスク終了時にこのセッションを明示的に閉じないと（tmux は attach が切れても detached で生存し続ける設計ゆえ）セッションがリークする。終了処理でセッションを kill する。この後始末に伴う SSH もイベントループを凍結させないよう別スレッドで行う（§10.7）。

- **SSH 呼び出しの上限**: 到達不能な相手への `ssh` が無期限に張り付くと、`to_thread` の共有スレッドプールが詰まり sa-ru の全ループが例外なく沈黙する（[是正 2026-09-04](../revisions/2026-09-04_mbp-unreachable-outage.md)）。上限は 2 層で掛ける。(1) クラスタ用 SSH client 設定（`pyinfra/templates/ssh_config.j2`・配備は手順書 02）に `ConnectTimeout 10` / `ServerAliveInterval 15` / `ServerAliveCountMax 2` / `BatchMode yes` を置き、コードを触らずに約 30 箇所の呼び出しへ一括して効かせる。(2) コード側は `subprocess.run` 等による `ssh` 呼び出しに **必ず `timeout` を付ける**（軽い操作は `sa-ru.yaml` `ssh.timeout_sec`、重い操作は `run_ssh_command` 既定 120 秒）。timeout 無しの `ssh` 呼び出しは AST 回帰テスト（`src/orchestrator/tests/test_ssh_timeout_163.py`）が検出し、混入を機械的に止める
- **ハング診断の常設（[是正 2026-09-04](../revisions/2026-09-04_mbp-unreachable-outage.md)）**: sa-ru は起動時に `faulthandler` を `SIGUSR1` へ登録し（`orchestrator/diagnostics.py`）、沈黙時に `kill -USR1 <pid>` で全スレッドの Python スタックを stderr（`sa-ru-error.log`）へ吐ける。`/opt/taka-ma-env` には `py-spy` を配備する。再起動前にこれらでスタックを採取する手順は運用手順書（`docs/operations/runbook-shutdown-restart.md`）に置く。採取なしに再起動すると原因が失われる（2026-09-04 の教訓）

> **NOTE（agy の認証制約）**: agy の認証は macOS keychain 依存で、素の SSH セッションからは読めない（**SSH 直実行不可**）。**GUI セッション起源の tmux 経由でのみ実行可**（[是正 2026-07-03](../revisions/2026-07-03.md)）。agy は subprocess アダプタ（§8.6）で実行する。

**エラーハンドリング:**

- SSH 接続失敗 → 3回リトライ（10秒間隔）、失敗で `failed` + Slack 通知
- headless: `result` 無し終了＝ハング → retry → 既存 fallback 列（ハング fallback とモデル障害 fallback は別カウンタ）
- headless: decide デーモン到達不可・判定異常 → フックが exit 2（deny・fail-closed）。旧 1 ショット判定へのフォールバックは持たない（デーモン障害の隠蔽と遅延回帰を防ぐ）。デーモンは launchd が自動再起動
- interactive: tmux セッション消失 → reconnect() 再アタッチ、EOF 異常終了 → `failed` + Slack 通知

<a id="sec-8-6"></a>
## 8.6 ④ sa-ru → Antigravity CLI（subprocess 経路）

Antigravity CLI（`agy` — Gemini CLI の後継のコーディングエージェント）固有の通信仕様。**高度なマルチモーダル解析**（動画・音声・画像の理解）の単発実行で使用する subprocess 経路を定義する。基本的な解析はローカル gemma4:31b（MBP worker）が担い（§2.4）、生成は Phase 2（生成基盤・§2.4）へ延期。

> **NOTE（2026-07-28 改訂）**: agy は現在 **subprocess 単発のみ**（`ya-ta.yaml` の `gemini.methods: [subprocess]`）。以前は interactive(pty) にも対応を宣言していたが、**pty の承認配線は実機 E2E で一度も検証されておらず**（[是正 2026-07-06](../revisions/2026-07-06.md)）、承認プロンプトに誰も答えなかったときの挙動も未測定であるため、未検証の承認経路を有効なまま残さない判断で外した。再開の条件は「無応答時の挙動を実測し fail-closed を確認すること」。用途別の参加（cross-review / fallback / 高度な解析）の全体像は **§8.4.x 相互扶助機能** を参照。

| 項目 | 仕様 |
|------|------|
| 方式 | SSH + subprocess（`RemoteProcessManager.run_model_subprocess`） |
| コマンド | **素の SSH 直実行は不可**: agy の認証は macOS keychain 依存で、SSH セキュリティセッションからは keychain を読めない（ローカル GUI では成功する対照実験で確定。[是正 2026-07-03](../revisions/2026-07-03.md)）。**GUI セッション起源の tmux 経由で実行可**（同実測）— GUI 起源 tmux サーバ内で `agy` を単発実行し、出力を回収する |
| 出力 | stdout（プレーンテキスト） |
| 主用途 | 高度なマルチモーダル解析の単発 / cross-review 参加時の並行投入 / API 障害 fallback（テキスト・コード）での順次代替 |

経路選択は orchestrator が用途に応じて動的に決める（`_select_method()`、構築手順書 05 主要 API 参照）。

> **NOTE（[是正 2026-07-28](../revisions/2026-07-28.md)）**: agy でツールが実行される条件は「**静的許可リストに合致する AND PreToolUse フックが deny を返さない**」である。フックは**許可を与える力を持たず**（`decision:"allow"` を返しても静的許可に無い操作は実行されない）、**拒否する力のみを持つ**（静的許可にある操作を deny で止められる）。したがって**フックが故障しても静的許可の範囲を超えない**。
>
> ここから、agy を per-tool 承認付き worker として使う場合の構成が決まる。**門は静的許可リスト**（`~/.gemini/antigravity-cli/settings.json` の `permissions.allow`。場所が違うと効かない）に置き、**フックは拒否専用の追加ゲート**として使う。実質のセキュリティ境界は静的許可リストであり、これを最小権限で設計することが多層防御の前提になる。フックの `allow` に依存する設計にしてはならない。
>
> **`--dangerously-skip-permissions` は使わない**（§3.1）。このフラグを付けると静的許可の門が外れてフックが唯一の門になり、フック異常（異常終了・不正 JSON・timeout・コマンド不在の 4 条件すべてで再現）でツールがそのまま実行される。フラグを使わない限りこの経路は生じない。
>
> なお素の `agy -p` は許可規則が無ければ read すら auto-deny する完全な fail-closed であり、本節の単発実行はこの性質の上に成り立つ。既存の **interactive(pty) 経路（§8.5）はフックを使わない別経路**であり、本 NOTE の権限モデルは適用されない（pty の無応答時挙動は未測定・§8.5 の ⚠️ を参照）。
>
> 詳細な実測マトリクスと再現手順は調査資料（`private/docs/agy-headless-approval-model/`）に集約する。**agy は自己更新するため、バージョンが上がったら権限モデルを再測すること**（採否判断の土台が変わる）。



**エラーハンドリング:**

- ClassifierStrategy 400 エラー → 明確なプロンプトにリライトしてリトライ（1回）
- タイムアウト → 5分で kill、`failed` + Slack 通知
- interactive アダプタのエラーハンドリングは §8.5 と共通

<a id="sec-8-7"></a>
## 8.7 ⑤ sa-ru → Gemma 4 31B（軽量タスク実行）

| 項目 | 仕様 |
|------|------|
| 方式 | SSH + ollama HTTP API（`RemoteProcessManager.run_model_subprocess` → `_run_local_model_http`） |
| コマンド | `ssh mbp "curl … http://localhost:11434/api/generate"`。**リクエスト JSON は stdin で渡す**（ssh → リモート zsh の再解釈でプロンプト本文が壊れるのを避ける） |
| 出力 | 応答 JSON の `response` フィールド |

**なぜ CLI（`ollama run`）ではないか:** CLI 単発は呼び出しごとに CLI 起動とモデルロードを払い、`keep_alive` を制御できない（sa-ru / ya-ta が §8.4 で HTTP API へ移行したのと同じ理由。inline レーンだけ CLI のまま残っていた）。「純生成の速い経路」であるはずの inline が、実測（2026-07-29 本番ログ）で 1 件 68 秒・146 秒を要していた。常駐時間は `ya-ta.yaml` の `models.gemma.keep_alive_sec` で管理する（MBP は worker と qu-e がメモリを分け合うため無期限にはしない）。ポートは開けず、SSH で MBP に入ってから MBP 自身の localhost API を叩く（通信方式は SSH のまま・§1.3）。

**エラーハンドリング:**

- ollama 未起動（Blender モード中） → Slack に通知「Blender モード中のため軽量タスクを実行できません」
- タイムアウト → 2分で kill、`failed` + Slack 通知

