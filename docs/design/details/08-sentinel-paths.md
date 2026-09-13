# 詳細設計: qu-e 連携経路（審査・監査・資源最適化）

> **位置づけ**: [設計書本体](../design-development-system.md) の詳細。本書が扱う節: §8.8・§8.11〜§8.14。
> 障害を契機とする設計改訂の経緯は [revisions/](../revisions/) を参照。

sa-ru と qu-e のあいだの通信経路。Tier 2 コードレビュー、監査アラート、ファイル変更アラート、
タスクコンテキスト共有、リソース最適化通知を扱う。qu-e 自体の役割は [04-sentinel.md](04-sentinel.md)。

---

<a id="sec-8-8"></a>
## 8.8 ⑥ sa-ru → qu-e（Tier 2 コードレビュー）

| 項目 | 仕様 |
|------|------|
| 方式 | SSH + CLI subprocess |
| コマンド | `ssh mbp "cd /opt/taka-ma/qu-e && PYTHONPATH=/opt/taka-ma/qu-e /opt/taka-ma-env/bin/python sentinel/review_cli.py --mode command --input '{command}' --context '{context_json}'"` |
| 出力 | stdout に JSON 1行 |

**review_cli.py** は qu-e に新規追加する CLI エントリポイント。既存の `reviewer.py` の `review_command()` / `review_diff()` をラップする。

**レスポンス形式:**

```json
{"decision": "approve", "reason": "安全な読み取り操作", "risk_score": 0.1}
```

```json
{"decision": "deny", "reason": "rm -rf を含む破壊的操作", "risk_score": 0.95}
```

```json
{"decision": "escalate", "reason": "判定困難、人間確認を推奨", "risk_score": 0.6}
```

**判定後のアクション:**

| qu-e 判定 | アクション |
|--------------|-----------|
| approve | PTY ラッパーに `y` を送信 |
| deny | Tier 3 にエスカレート（Slack で人間に確認） |
| escalate | Tier 3 にエスカレート |

**エラーハンドリング:**

- SSH 接続失敗 → Tier 3 にエスカレート（安全側に倒す）
- JSON パースエラー → Tier 3 にエスカレート
- タイムアウト（`sa-ru.yaml` `approval.tier2_timeout_sec`） → **1 回だけ再試行**し、再失敗で Tier 3 にエスカレート

**審査の可用性（タイムアウト起因の Tier 3 濫発の抑止）:**

タイムアウトによる Tier 3 化は「操作が危険」ではなく「審査不能」である。値は warm 実測 14.7 秒（2026-07-29）に対し 120 秒で妥当だが、cold（審査モデルのロード）・他モデルとの競合時の分布が未実測のまま際どい。対策は値の延長ではなく構造で行う:（[是正 2026-08-30](../revisions/2026-08-30.md)）

- **審査モデルの常駐化**: qu-e の審査モデルを `keep_alive` で常駐させ、cold ロードを審査経路から除去する（`qu-e.yaml` 設定。§7.1 の常駐予算に算入）
- **再試行 1 回**: 上表のとおり。一過性の詰まりを人間承認へ波及させない
- **文言の是正**: タイムアウト起因の Tier 3 リクエストの Risk 欄は「審査不能（qu-e 応答なし）のため人間確認」と表示し、「危険と判定された」と誤読させない


<a id="sec-8-11"></a>
## 8.11 qu-e → sa-ru（監査アラート）

| 項目 | 仕様 |
|------|------|
| 方式 | sa-ru による定期ポーリング |
| 監視対象 | `/opt/taka-ma/logs/qu-e-health.json`（MBP 上） |
| ポーリング間隔 | 60秒 |
| 取得方法 | `ssh mbp "cat /opt/taka-ma/logs/qu-e-health.json"` |

- qu-e は既存の 30秒間隔ヘルスチェック結果をファイルに書き出す
- sa-ru が SSH 経由で定期取得し、`warning` / `critical` 時に Slack 通知

<a id="sec-8-12"></a>
## 8.12 qu-e file_audit → sa-ru（ファイル変更アラート）

| 項目 | 仕様 |
|------|------|
| 監視対象 | **静的ルート**（`qu-e.yaml` `file_audit.watch_paths`、既定 `/opt/taka-ma`）を起動時に再帰監視する。加えて、**実行中タスクの実開発リポジトリ**（§8.13 `workspace` が静的ルート外を指すとき）を**タスク期間中だけ動的に監視へ登録し、終了時に解除**する（下記「動的監視（実開発リポジトリ）」）。symlink を静的ルート内へ張る方式は不可（watchdog/FSEvents は symlink 先のイベントを検知しない。[是正 2026-07-14](../revisions/2026-07-14.md)） |
| 検知トリガ | watchdog（FSEvents、event-driven）。作成・更新・削除に加え**リネーム（moved）も検知**し、移動先パスを新規変更として監査する（無視される名前で作成→目的パスへリネームする検知回避を防ぐ）。移動元パスは削除として扱う。ただし後述「原子的書き込みの集約」により、正当な atomic write（tmp 作成→本体へ rename／本体を消して書き直す）は削除アラートに化けさせない |
| 原子的書き込みの集約（削除誤検知の抑止） | エディタや `claude` worker は「一時ファイルへ書く→本体へ rename」「本体を一旦削除→再作成」で保存する。この途中経過を素朴に拾うと、正当な保存が**本体パスの削除**アラートに化ける。これを防ぐため、**同一パスに対する delete が debounce ウィンドウ内で create / moved（into=本体パス）と対になった場合は、両者を 1 件の modify（更新）に集約**して判定する。ウィンドウ超過まで再作成が来なければ**真の削除**として監査する。検知回避（無視名→監査名 rename）は移動先パスの監査で従来どおり担保されるため、本集約で緩まない |
| 判定対象 | 静的フィルタ通過分のみ（`ignore_patterns` ∪ `.gitignore` ∪ **システム制御プレーン** ∪ **一時成果物の既定除外**）。ただし `.gitignore` 自身の変更は静的フィルタの対象外＝**常に監査**する（除外ルールを書き換える変更こそ監査対象） |
| 一時成果物の既定除外 | worker が作業中に作る一時ファイル（`.tmp-*/` ディレクトリ配下・`*.tmp`）は既定の ignore_patterns に含める（[是正 2026-08-30](../revisions/2026-08-30.md)）。除外はパターン固定・コード側定数で、`.gitignore` 書き換え監査の思想（除外ルールの変更は監査する）とは競合しない |
| 同一タスクの連続アラート集約 | 同一 `task_id` からの deny / escalate アラートは 60 秒窓でダイジェスト集約し、1 通（件数＋パス列挙＋代表理由）で通知する。1 件ずつのボタン付き連投は、本当に見るべき承認の注意を薄める（[是正 2026-08-30](../revisions/2026-08-30.md)） |
| システム制御プレーンの除外 | sa-ru が worker 起動のたびに workspace へ配置する制御ファイル（例: PreToolUse フック設定 `.taka-hook-settings.json`）は、**外部からの改変ではなく sa-ru 自身が毎タスク生成・上書きする自己生成物**であり、監査対象にすると全タスクで同一パスの escalate/deny アラートを量産する。task_context ディレクトリ（§8.13）と同格の制御プレーンとして絶対パス prefix で除外する。ユーザ成果物と異なりパスが固定・既知のため、`.gitignore` 書き換え監査（上記）の思想とは競合しない |
| `.gitignore` 同期 | 判定実行時に mtime チェックで再読み込み |
| `.gitignore` の適用限界（fail-closed） | ワイルドカード等のみで構成される**過大パターン**（例: `*`、`**`）はリポジトリ丸ごとの監査バイパスになるため適用しない（警告ログを残して無視）。`!` 否定（再包含）は「否定パターンに一致するパスは除外しない＝監査する」と**安全側（監査を増やす方向）**で近似する |
| 判定者 | qu-e |
| 判定結果 | approve / deny / escalate |
| diff 要約 | LLM 判定と通知の根拠には**変更内容を含む diff**を用いる（件数のみの stat 要約では判定材料にならない）。肥大化防止のためサイズ上限で切り詰め、git 管理外・取得不能時は「種別: パス」の最小要約に縮退する |
| ノイズ抑制 | 同パス連続変更を debounce で集約。集約状態（タイマー台帳）の管理は**イベントループ上で直列化**し、watchdog ワーカースレッドとの競合による二重監査・取り消し漏れを防ぐ |
| 通知トリガ | **`approve` と明示されたときのみログ記録で完結。それ以外（`deny` / `escalate` / 未知・異常な判定・判定不能）はすべて人間へ通知する（fail-closed）** |
| 通知経路 | qu-e → sa-ru → Slack（SSH push） |
| 通知タイミング | 即時 |
| 通知宛先 | タスク実行中: `channel_id` + `thread_ts` で Thread。タスク非実行中: `channel_id`（`SLACK_CHANNEL_ID` フォールバック）で別投稿 |
| 通知ペイロード | 識別子（`audit_log_id`, `task_id`）/ 対象（`path`）/ 判定（`decision`）/ 根拠（判定理由・qu-e confidence・diff サマリ）/ コンテキスト（`command`, `status`） |
| ボタン | Approve / Reject（Block Kit） |
| 押下後経路 | **Approve と Reject で分岐する**。Approve は「監査済み」を確定する**定型処理**（下記）で、LLM 実行を伴わないため §8.3 のタスク経路に乗せない。Reject は revert（ファイル変更取り消し）の判断に LLM を要するため §8.3 のタスク投入経路を再利用する（専用経路は新設しない） |
| 保存形式 | jsonl |
| 保存先 | `/opt/taka-ma/logs/file-audit/` |
| ファイル名 | `file-audit-{YYYY-MM-DD}.jsonl`（日付別） |
| 保持期間 | `retention_days: 90`（`qu-e.yaml` で切替可） |
| ローテーション | 起動時 + 日次に削除（`retention_days` 超過ファイル） |
| レコード ID | `id` フィールド（jsonl 側）＝ アラート JSON の `audit_log_id` ＝ アラートファイル名 `{audit_log_id}.json`。Slack ボタン callback は `audit_log_id` を値に持ち、承認ハンドラは sa-ru ローカルのアラートファイルを名前で引き当てる（上記「承認レコードの参照元」参照） |

> **注記**: 監査ログ jsonl は現状「人間の事後追跡用」。SIEM 連携等の長期用途は将来再検討。

**監査判定の fail-closed 原則**:

監査は「危険な変更を無音で通す」ことを最悪の失敗とみなし、判定が確定的に安全（`approve`）と言えない限り必ず人間へ escalate する。

- **判定結果の正規化**: qu-e LLM の応答は `approve` / `deny` / `escalate` のいずれかに正規化する（前後空白除去・小文字化）。この 3 値以外（大文字 `DENY`、`block`、キー欠落、判定フィールド不在）はすべて `escalate` に倒す。「approve と明示された時だけ承認確定、それ以外は人間へ」を唯一の分岐基準とする。
- **異常出力の扱い**: LLM 応答が JSON オブジェクト（dict）でない（配列・文字列・スカラ）、または必須項目（判定・理由）を欠く場合も `escalate`。判定材料が壊れているのに承認へ倒さない。
- **例外の非握り潰し**: qu-e への到達不能・応答パース失敗・監査処理中の予期せぬ例外は、記録もアラートも出さずに握り潰してはならない。「監査できなかった変更」も人間へ escalate 通知する（監視が沈黙したまま危険変更が通る経路を塞ぐ）。
- **監査レコードの固定キー保全**: 監査レコードの識別・突合キー（`id` / `path` / `task_id` / `timestamp` / `event`）は、LLM 応答由来のフィールドで上書きされてはならない。上書きを許すと後段の承認突合（レコード参照）が不能になり、監査の改竄経路にもなる。

**承認レコードの参照元（クロスホスト）**:

qu-e が書く監査 jsonl（`保存先` 参照）は **MBP ローカルの監査証跡**であり、Slack 承認ハンドラ（u-zu、Mac mini 側）はこれを直接 `open` しない（別マシンのローカルディスクを読めないため、従来は承認押下時に常に「レコード無し」で停止していた）。

承認ボタン押下時のレコード参照は、qu-e が sa-ru へ push 済みの**アラート JSON を唯一の参照元**とする。アラートは sa-ru ローカルの `alert_dir` に `{audit_log_id}.json` として着信し、Slack 転送後は `{alert_dir}/done/{audit_log_id}.json` に退避されて履歴として残る。承認に必要な全フィールド（`path` / `channel_id` / `thread_ts` / `command` / `status` 等）を含むため、MBP への SSH 読み出しは不要。

**Approve / Reject 押下時の動作主体**:

| ボタン | 動作 | 主体 | LLM 実行 |
|--------|------|------|---------|
| Approve | 監査済みを確定（アラートレコードへ承認マーク／jsonl 追記） | u-zu → qu-e（定型処理） | なし |
| Reject | jsonl 追記 | qu-e | なし |
| Reject | プロセス停止 | sa-ru（process_manager） | なし |
| Reject | revert（ファイル変更取り消し） | ya-ta が振り分けた LLM | あり |

**Approve（定型処理・LLM 非経由）**: Approve は「この変更は問題ない」と人が確定する操作であり、判断は既に人が下している。したがって自然言語コマンドとして worker LLM に投げず（投げると LLM が指示文を再解釈し、思考ダンプを出力するなどの逸脱が起きる）、u-zu が `audit_log_id` でアラートレコードを引き当て、承認済みマークの記録（jsonl 追記）を機械的に行う。qu-e への反映が要る場合も定型 API（LLM 非経由）で行う。

**Reject（§8.3 経由・LLM あり）**: Reject は revert（どう取り消すか）の判断に LLM を要するため、§8.3 の経路で `/opt/taka-ma/data/tasks/` にタスクファイルを作成し、ya-ta が分解 → 振り分け先 LLM が実行する。プロセス停止（sa-ru）と jsonl 追記（qu-e）を伴う。

**動的監視（実開発リポジトリ）**:

開発の本来の場は静的ルート下の作業ディレクトリではなく、実開発リポジトリ（例: MBP `~/DevDev/xxx` の git clone）である。ここでの変更も「コミット前に qu-e が監査する」を成立させるため、file_audit の監視対象をタスク単位で動的に広げる。

- **登録**: qu-e は task_context（§8.13）の受信で、`workspace` が静的ルート外の実パスを指すとき、そのパスを `observer.schedule`（再帰）で監視へ追加する。静的ルート配下は既に監視済みのため登録しない（二重イベント防止）。
- **解除**: 終了系 status（`completed` / `failed`）の受信で登録を解除する。同一リポジトリを複数タスクが並行使用している間は解除しない（参照カウント。最後のタスクの終了で解除）。
- **帰属**: 変更パスの task_id 帰属は従来どおり `workspace` 接頭辞の最長一致（§8.13）で行う。動的登録されたパスもこの照合に自然に乗る。
- **登録失敗の fail-closed**: 監視登録に失敗した場合（パス不在等）、その期間の変更は監査されないまま沈黙する。これを無音で通さず、escalate アラートとして人間へ通知する。
- **再起動耐性**: qu-e 再起動時は task_context の起動時初期スキャン（§8.13）が実行中タスクの `workspace` を再読込するため、動的監視も復元される。

**コミット前ゲート（pre-commit 監査）**:

file_audit の watchdog 監査は「変更の検知」であり、コミット操作そのものを堰き止めない。実開発リポジトリでは「コミット前に qu-e 監査を通す」を git pre-commit フックで強制する。

| 項目 | 仕様 |
|------|------|
| ゲート方式 | git pre-commit フック。staged diff（`git diff --cached`）を qu-e LLM が審査し、**approve のみコミット続行（exit 0）。deny / escalate / 判定不能・LLM 不達はコミット中断（exit 1、fail-closed）** |
| 実体 | フック本体: `sentinel/hooks/pre-commit`（sh）→ `sentinel/commit_audit_cli.py`（1 ショット CLI。Tier 2 の `review_cli.py` と同型・推論直列化ロック共有） |
| 導入 | qu-e が動的監視の**登録時と解除時**（タスク終了時。タスク中に clone されたリポジトリを拾う）に、`workspace` が git リポジトリで `.git/hooks/pre-commit` 不在なら自動配置する。**既存フックは上書きしない**。自動導入の要否は `qu-e.yaml` `file_audit.commit_gate.install_hook` |
| 記録 | file_audit と同じ監査 jsonl に `event="commit"` で追記（同一 `log_dir`・retention 共通）。Slack 通知はしない（コミット実行者＝人間の端末 or worker が exit code と出力で即時に結果を受け取るため、非同期通知は不要） |
| worker コミットとの関係 | worker（headless）の `git commit` は PreToolUse フック → Tier 判定でも審査される（§3、既存）。pre-commit ゲートはそれと独立に、**人間の手元コミットを含む全コミット**を堰き止める最終ゲート（worker コミットは二重審査になるが、推論直列化ロックの範囲で許容） |
| 迂回 | `git commit --no-verify` は git 仕様上迂回可能。ただし変更自体は watchdog 監査（本節）で検知・判定済みであり、迂回は「ゲートの無効化」ではなく「監査済み変更の未ゲートコミット」に留まる |
| 基盤不在時 | 監査基盤（`/opt/taka-ma/qu-e`・venv）が存在しない場合、フックは警告を出して素通しする（アンインストール後にユーザーのリポジトリのコミットを恒久ブロックしない）。基盤が存在する環境での判定不能（ollama 不達・パース失敗等）は fail-closed で中断する |
| 新規 clone | clone 済みリポジトリの指定が基本形。新規 clone はタスク内で worker が行う（sa-ru は `workspace` ディレクトリの存在のみ保証する。§8.13）。clone 完了後のフック導入はタスク終了時の自動配置が拾う |

<a id="sec-8-13"></a>
## 8.13 sa-ru → qu-e（タスクコンテキスト共有）

| 項目 | 仕様 |
|------|------|
| 方式 | sa-ru が SSH push でタスク情報を qu-e に通知 |
| 通知タイミング | `in_progress` 遷移時 / `completed`・`failed` 遷移時 |
| 内容 | `task_id`, `command`, `channel_id`, `team_id`, `thread_ts`, `status`, `workspace` |
| 用途 | qu-e が file_audit 判定時に「実行中タスクの指示範囲」判定材料。`workspace` は動的監視（§8.12）の登録・解除にも用いる |
| `workspace` の決定 | 既定はタスク専用作業ディレクトリ `{workspace_base}/{task_id}`。**実開発リポジトリの明示指定**があるときはその絶対パス（下記）。worker（headless / pty）はこの `workspace` を cwd として起動する |
| 実開発リポジトリの明示指定 | **workspace の権威は受け渡し契約のフィールド（§8.10f）**。契約化パスの脳判定が会話全文脈（過去発話・要約に書かれたパス含む）から特定し、検証（下記 fail-closed）を経てフィールドに載る — ユーザーの再発話を要求しない（[是正 2026-08-22](../revisions/2026-08-22.md)）。`repo:/絶対/パス` 記法と**自然文のリポジトリ指定**（`#Repo ~/DevDev/...`「リポジトリ: /path」等）の正規表現抽出は**契約化パスへの入力の 1 つ**に格下げして維持する（決定的で速い経路。記法指定は脳判定より優先）。自然文候補が検証を通らないときは会話を止めず `repo:` 記法での再指定を促す。着手確認レコード → 確定タスク `workspace` → dispatcher → §8.13 push と伝搬する |
| 指定のセッション持続 | 抽出は ready を発火させた最終発話に限らず**ユーザー発話ごと**に行い、検証済みの値を会話セッションへ持続させる（セッション永続化ファイルにも保存。再起動・TTL 経過後も回復）。同一セッションで複数回指定されたときは最後の指定が勝つ。「冒頭でリポジトリ指定 → 後の発話で着手」の自然な流れで指定が落ちない |
| `repo:` パスの検証（fail-closed） | パスは SSH コマンド文字列・worker の cwd に乗るため、**絶対パス・安全文字（英数 `. _ - /`）のみ・`..` 成分不可**を sa-ru 側で強制し、不一致は着手させずユーザーへ差し戻す。`~/` 前置きは worker ホスト（MBP）の HOME（`sa-ru.yaml` `task_context.worker_home` が唯一の供給元）へ**展開してから**同じ検証に通す。`worker_home` 未設定時は従来どおり差し戻す（誤ったホームで展開しない安全側。qu-e 側は防御的に expanduser して照合する） |
| 着手確認での明示 | 着手確認の提示文に `workspace:` 行を**常に**出す。明示指定があればそのパス、無ければ「未指定（既定の空作業場）」と `repo:/絶対パス` での指定方法を明示する（未指定のまま空 workspace で worker が走ることに人間が着手前に気づけるようにする） |
| `workspace` の存在保証 | sa-ru は `in_progress` push と**同一 SSH コマンド内で先に `mkdir -p {workspace}`** を実行する。qu-e はこの push を受けて動的監視を登録するため、登録時点でのディレクトリ存在が順序として保証される（新規 clone 運用ではこの空ディレクトリへタスク内で worker が clone する） |
| パス→task_id 帰属 | qu-e は file_audit の変更パスを `workspace` 接頭辞で照合し、**並行実行中の複数タスクから正しい task_id を特定**する（最長一致優先）。一致なしかつ in_progress が複数のときは曖昧として帰属せず、フォールバック通知に委ねる |
| 起動時初期スキャン | qu-e は起動時に受信ディレクトリの**既存 task_context ファイルを読み込んでから**監視を開始する（読み込み規則はイベント受信時と同一: 終了系 status は保持しない）。qu-e 停止中・再起動中に push された文脈を取りこぼすと、実行中タスクの変更が匿名（`status=none`）と誤判定されアラートが濫発するため |
| workspace の後始末（rotation） | 既定 workspace（`{workspace_base}/{task_id}`）は clone したリポジトリ・生成物を含み 1 件で数百 MB になり得る唯一の無管理蓄積源のため、qu-e が retention 管理する。判定根拠はディスクに残る task_context レコードの**終了 status（completed/failed）＋レコード mtime（終了 push 時刻）**で、retention 超過分の workspace 実体とレコードを削除する（jsonl rotation と同じ起動時＋日次周期）。**削除しないもの**: 実行中タスク（終了 status でない・メモリ store に実行中として残る task_id）、`repo:` 明示指定の実開発リポジトリ（workspace_base 外＝ユーザー資産。レコードのみ掃除）、symlink・`..` で workspace_base 外を指すパス（realpath 照合）、レコードを持たない orphan（削除根拠なし。件数のみログ可視化）。削除は file_audit（§8.12）へ**自己操作として抑制宣言してから**行う（rmtree の大量削除イベントを外部改変と誤認した escalate の量産防止。抑制は TTL で自然消滅）。運用値（基底・保持日数・抑制 TTL）は `qu-e.yaml` `workspace_rotation` が唯一の源 |

> **解消済み**: §8.12 通知宛先の「タスク実行中: `channel_id` + `thread_ts` で Thread 投稿」を実現するため、本セクション payload に `thread_ts` を追加（sa-ru `_push_task_context` → qu-e `task_context` → file_audit アラート）。これにより実行中タスクへの file_audit アラートが同一スレッドへ Thread 返信される。

<a id="sec-8-14"></a>
## 8.14 qu-e → sa-ru（リソース最適化通知）

| 項目 | 仕様 |
|------|------|
| 目的 | MBP メモリ使用率に応じた worker LLM（agent レーン）並行実行数の動的調整。§4.2「リソース最適化」の正式通信経路 |
| 方式 | qu-e → sa-ru の SSH push（§8.12 と同方式） |
| データ源 | qu-e `HealthChecker.check_all()` の判定 + `ResourceOptimizer.recommended_heavy_instances()` の算出 |
| 送信トリガ | 推奨並行数が現行値から変化したとき（メモリ使用率しきい値の跨ぎ）。再算出間隔は `notify_interval_sec` |
| ペイロード | `recommended_heavy_instances`（推奨並行数）/ `memory_usage`（使用率）/ `level`（normal / warning / critical） |
| 送信先 | sa-ru の `resource_optimization.notify_dir`（Mac mini）。qu-e が `ssh` で json を書き込み、sa-ru が watchdog で即時検知 |
| sa-ru 側機構 | `max_heavy_instances` は実行時可変のため `DynamicConcurrencyLimiter`（`asyncio.Semaphore` 代替）で制御。受信値で `set_limit()` を呼ぶ。減少時は実行中タスクを強制終了せず、新規 heavy 起動を抑制（OOM 回避）。増加時は待機中タスクへ即時開放（throughput 最大化） |
| 並行数の権威値 | 上限は qu-e.yaml `resource_optimization.max_heavy_instances`。sa-ru は起動時 ya-ta.yaml `concurrency.max_heavy_instances` をブートストラップ値として用い、以後 qu-e の通知で駆動される（両者は揃える） |
| しきい値設定 | `qu-e.yaml`。`level` は `health_check.thresholds`（memory_warning / memory_critical）、並行数の増減境界は `resource_optimization`（scale_up / scale_down） |

> **補足**: 旧実装では `HealthChecker` / `ResourceOptimizer` は判定のみで sa-ru へ未通知（advisory only）だった。本パスで sa-ru へ反映し §4.2 の役割を実体化した。§8.11（ヘルスアラートのポーリング → Slack 警告）とはデータ源を共有するが、用途（人間への警告 vs 並行数の自動調整）が異なる独立経路。

**処理フロー図**: 関数名・ファイル名つきの全体フローは [08-resource-optimization-flow.md](08-resource-optimization-flow.md) を参照。

