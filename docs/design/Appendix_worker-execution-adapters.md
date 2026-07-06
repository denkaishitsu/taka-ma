# worker 実行アダプタ抽象化 — subprocess / interactive(pty) / headless（Claude Code は headless アダプタの一実装）

> **正本との対応**: 設計書本体 [§8.5 worker CLI（実行アダプタ抽象）](design-development-system.md#85-③-sa-ru--worker-cli重量タスク実行実行アダプタ抽象) に対応する詳細。本体が正本、本 Appendix は **実行アダプタ（CLI 固有）** の詳細（3層抽象の全体・cap1-10 実機根拠・レビュー対応表）を担う。
>
> **承認判定の中核（CLI 非依存）は本 Appendix の対象外**。`decide()`/`PendingApproval`/`Decision`/Tier1-3/§8.10 待ち・handler 返却契約は設計書本体 [§3 承認パイプライン設計](design-development-system.md#3-承認パイプライン設計) を参照。本 Appendix は「アダプタが CLI 固有の入出力を中核の型へ変換する」層だけを扱う。

対象: worker 実行方式（PTY+スクレイピング → 実行アダプタ抽象）の再設計。設計書本体 §2.1/§3/§8.5/§8.9/§8.10 と手順書の worker 実行記述を本方式へ改定する。**本体 §sections は改定済み（本 Appendix と双方向リンク）**。

**最上位の絶対制約（本設計の憲法）**: worker 実行・承認機構は**特定の worker CLI（Claude Code 等）にロックインしてはならない**。Claude Code の headless/stream-json/PreToolUse フックは Claude 固有機能であり、それを**アーキテクチャの中心に据えてはならない**。CLI 固有部分は必ず「アダプタ」に隔離し、中核（承認判定・実行 dispatch）は CLI 非依存で保つ。将来の Codex・別 CLI・別 LLM を、アダプタ追加だけで差し込めること。

**改訂履歴**:
- v1: headless + 承認時 `--resume`/`--allowedTools` 緩和ループを主軸。
- v2: 敵対的レビューで致命穴4件（収束保証なし/継続再実行未保証/過剰許可窓/safe-command バイパス）を指摘。追加実機検証で **PreToolUse フックが唯一これらを閉じる**と確認し、安全層を「フック同期ゲート」へ変更。
- v3（本版）: 「フック中心」が **Claude ロックイン**になっている欠陥を指摘され、**実行アダプタ抽象化**を最上位に据え直し。Claude headless+フックを「アダプタの1実装」に格下げし、承認判定中核（`ApprovalPipeline.decide`）と実行 dispatch（`_select_method`）を CLI 非依存の抽象境界として定義。

## 0. 一次根拠（実機検証・claude 2.1.201・2026-07-05）

本設計は実機観測に基づく。検証項目と結果は次のとおり。

| 検証項目 | 結果 |
|---|---|
| permission-mode の挙動 | headless `-p` は非許可ツールを対話プロンプト無しで即・構造化拒否。`default`と`manual`は非許可ツールに同一挙動（当初前提を実機修正） |
| permission_denials の構造 | `result.permission_denials=[{tool_name, tool_use_id, tool_input}]` |
| --disallowedTools | ツール自体を消す（deny 機構には使わない） |
| --resume + --allowedTools | 継続は成立するが scoped rule 破綻等でフォールバックに降格 |
| scoped rule 粒度 | `Write(path)`不成立・`Bash(echo SAFE)`無差別許可。安全性を rule 粒度に依存不可 |
| --verbose | stream-json に必須 |
| PreToolUse フック | 全ツール（safe な echo 含む）実行前に発火。stdin に構造化 tool_input 受領。exit 2 でブロック・非ハング正常終了。`--include-hook-events`で観測可 |
| フック force-allow | `permissionDecision:allow`で default-deny を上書き実行。フックが allow/deny を権威決定。allowedTools 依存消滅 |

## 1. 抽象化アーキテクチャ（最上位・Claude ロックイン禁止）

worker 実行を**3層**に分け、CLI 固有部分をアダプタに閉じ込める。

```
┌─────────────────────────────────────────────────────────────┐
│ 実行 dispatch（CLI 非依存）  _select_method(methods) → 実行アダプタ選択 │
│   methods: subprocess | interactive(pty) | headless                │
└─────────────────────────────────────────────────────────────┘
        │ 各アダプタは共通 IF: run(task, model_flag, workspace) → output
        ▼
┌───────────────┬───────────────────────┬───────────────────────┐
│ subprocess    │ interactive(pty)       │ headless               │
│ アダプタ       │ アダプタ（汎用対話 CLI）  │ アダプタ（Claude 専用）  │
│ 単発 stdin     │ WorkerPtyWrapper +     │ headless_runner +      │
│ (ollama/agy)   │ interceptor(レガシー y/n)│ stream-json + フック    │
└───────┬───────┴───────────┬───────────┴───────────┬───────────┘
        │ 承認が要るアダプタは、CLI 固有の入出力を                  │
        │ 構造化 PendingApproval / Decision に「変換」して中核へ渡す │
        ▼                   ▼                       ▼
┌─────────────────────────────────────────────────────────────┐
│ 承認判定 中核（CLI 完全非依存・唯一の判定点）                        │
│   ApprovalPipeline.decide(PendingApproval{tool_name, tool_input}) │
│     → Decision{allow: bool, reason: str}                          │
│   Tier1 安全性チェック/自動 · Tier2 qu-e · Tier3 人間(§8.10 ポーリング)      │
└─────────────────────────────────────────────────────────────┘
```

### 1.1 抽象の2境界（seam）

- **seam A — 承認判定中核**: `ApprovalPipeline.decide(PendingApproval) → Decision`。**どの CLI から来ようと、構造化 {tool_name, tool_input} を受け allow/deny を返すだけ**。Tier1/2/3・安全性チェック・§8.10 はここに集約し、CLI の存在を一切知らない。ここが抽象の本体。**中核の詳細は本 Appendix ではなく設計書本体 [§3 承認パイプライン設計](design-development-system.md#3-承認パイプライン設計)**。本 Appendix は seam B とアダプタ（CLI 固有）を扱う。
- **seam B — 実行 dispatch**: `_select_method(methods)` が worker の `methods` 宣言で実行アダプタを選ぶ。新 CLI 追加＝`methods` 宣言＋アダプタ実装のみ。

### 1.2 アダプタ（CLI 固有・プラガブル）

各アダプタは「CLI 固有の承認入出力」を**中核の型へ双方向変換**する変換器を持つ。中核ロジックは複製しない。

| アダプタ | 対象 worker | 承認要求の取得（→ PendingApproval） | Decision の返し方 | 撤去/新設 |
|---|---|---|---|---|
| **subprocess** | ollama / agy（keychain・GUI tmux） | 単発実行・**per-tool 承認なし**（対象外） | — | 現行維持 |
| **interactive(pty)** | 汎用対話 CLI（agy 対話・将来 Codex 等） | interceptor の**レガシー y/n 検出**（YN/YES_NO/ALLOW）で stdout から抽出 | `WorkerPtyWrapper` が y/n キー送信 | **温存**（Claude 固有の Ink 検出のみ撤去） |
| **headless** | **Claude Code**（stream-json+フック） | **PreToolUse フック stdin の構造化 JSON** | フックが `permissionDecision:allow`/exit 2 | **新設** |

### 1.3 ロックイン禁止の担保（レビュー観点）

- 中核 `decide()` に `stream-json`・`permission_denials`・`--allowedTools`・フック・pexpect 等の**CLI 固有語が一切現れない**こと（grep で検証可能な不変条件）。
- Claude 固有処理は `headless` アダプタ内に閉じる。`interactive` アダプタの Claude 固有部分（Ink `MENU_CURSOR`/`TRUST_DIALOG`）だけを撤去し、**汎用対話の抽象（レガシー y/n）は壊さない**。
- 新 CLI（例 Codex が独自 headless プロトコルを持つ）→ 新アダプタ＋変換器を足すだけで、中核も他アダプタも不変。

## 2. headless アダプタ（Claude Code 専用の1実装）

Claude worker を非対話ヘッドレス 1 プロセスで実行し、各ツールを PreToolUse フックが同期ゲートする。承認 resume ループは持たない。

```
起動: claude -p "<task>" --output-format stream-json --verbose --include-hook-events \
        --permission-mode default --settings <hook_settings.json> --model <flag>
      （cwd = タスク専用 workspace /opt/taka-ma/work/{task_id}）
各ツール実行前: Claude → PreToolUse フック（stdin: {tool_name, tool_input, tool_use_id, cwd}）
      フック（薄いクライアント）→ SSH → decide デーモン（Mac mini 常駐・§2.1）
      デーモンが PendingApproval に変換 → ApprovalPipeline.decide() → Decision
        allow → permissionDecision:"allow"(exit0) / deny・全異常 → exit 2（未実行）
完了: result イベントで自己終了。result 無し終了＝ハング → retry/fallback（§8）
```

ハード制約（実機）: `stream-json`は`--verbose`必須／フック観測は`--include-hook-events`／フック注入は`--settings`／フック timeout は Tier3 の300秒超（例310秒）。

実装本体: `src/orchestrator/headless_runner.py`（`WorkerHeadlessRunner`＝stream-json 読取、`build_hook_settings`＝フック settings 生成）／`src/approval-pipeline/decide_daemon.py`（Mac mini 常駐の判定サーバ＝Claude 固有の変換器を含む。フック payload→`PendingApproval`、`Decision`→allow/deny 応答）／`src/approval-pipeline/decide_client.py`（フックの薄いクライアント。標準ライブラリのみ）。

### 2.1 判定実行系 — decide デーモン（Mac mini 常駐）とフックの薄いクライアント化

**背景（旧方式の 2 欠陥）**: 旧方式はフックごとに `ssh <mini> python3 decide_cli.py` を 1 ショット起動していた。

1. **遅延の累積**: 毎ツールで SSH handshake＋Python コールドスタート＋依存 import（yaml / slack_sdk / ai_gateway）＋config 2 本ロード＋SlackNotifier 構築を払い、承認レイテンシがツール数に比例して累積する。
2. **フック異常の fail-open**: 判定プロセスが import 失敗等で exit 1 すると、Claude Code のフック仕様上ブロック（exit 2）ではなく「フックのエラー」として既定権限評価に落ち、read 系ツールが承認パイプラインを素通りし得る。実際に `slack_notifier`（orchestrator 配下）が PYTHONPATH に無く、フックが全ツール呼び出しで exit 1 になる断線が発生した（decide_cli 内の fail-safe deny は import 段の失敗には無力）。

**構成**:

```
MBP（worker 側）                      Mac mini（判定側）
claude -p …
  └ PreToolUse フック:
    ssh <mini> decide_client … || exit 2
       └── SSH（ControlMaster 多重化）──→ decide_client（標準ライブラリのみ）
                                            └ UDS /opt/taka-ma/data/decide.sock
                                          decide_daemon（launchd 常駐・asyncio）
                                            ├ config / ApprovalPipeline / SlackNotifier を常駐ロード
                                            └ ApprovalPipeline.decide() → {allow, reason}
```

**プロトコル**: 1 接続 = 1 判定。クライアントがフック stdin の JSON にタスク文脈（argv の task_id / team_id / channel / thread_ts / instance_id）を併せた 1 行 JSON を送信し、デーモンが `{"allow": bool, "reason": str}` の 1 行 JSON を返して切断する。ソケットは Unix ドメイン（ポート開放なし。ソケットへの到達自体が SSH 経由＝通信方式の原則維持）。

**終了コード契約（フックコマンド全体・fail-closed）**:

| 事象 | 出力 | exit |
|---|---|---|
| allow | stdout に `permissionDecision:"allow"` JSON | 0 |
| deny（判定結果） | stderr に理由 | 2 |
| デーモン到達不可 / 応答タイムアウト / クライアント例外 | stderr に理由 | 2 |
| SSH 失敗・リモート起動失敗（exit 255 / 127 等） | — | フックコマンド末尾の `|| exit 2` 集約で 2 |

exit 0（allow）と exit 2（deny）以外でフックコマンドが終わる経路を持たない。判定不能＝deny を終了コード契約で保証し、fail-open の穴（上記欠陥 2）を閉じる。

**タイムアウト設計（内側 < 外側）**: デーモンの 1 判定上限 305 秒（`asyncio.wait_for`。Tier3 人間待ち最大 300 秒＋ハンドラ処理の余裕）＜ クライアントの応答待ち 308 秒 ＜ フック timeout 310 秒。判定側のハング（ya-ta 障害等）もクライアント側で必ず exit 2 に確定させ、フック timeout 経路（挙動が Claude 側実装依存）に委ねない。

**並行性**: デーモンは接続ごとに asyncio タスクで並行処理し、Tier3 待ち中も他 worker の判定は進む。`ApprovalPipeline` は 1 インスタンスを共有する（handlers / classifier は per-request の可変状態を持たず、Tier3 は request_id 別の承認ファイルで分離＝本体 §8.10）。

**障害の波及範囲（fail-closed の単位は 1 判定リクエスト）**: デーモンはタスク状態を持たない（stateless。Tier3 の待ち状態も §8.10 の承認ファイル＝ディスク上）。worker タスクの実体は MBP 側の `claude -p` プロセスであり、判定側の障害でタスクが消失することはない。

| 障害 | 波及範囲 | 挙動 |
|---|---|---|
| 1 判定内の例外（ya-ta 障害・config 不正等） | その 1 ツール呼び出しのみ | 接続ごとの asyncio タスク内で捕捉し、その接続にだけ deny を返す。デーモンは落とさず、並行中の他判定は継続 |
| デーモンプロセス crash（launchd 再起動） | その瞬間に判定中だった呼び出しのみ | 各クライアントが接続断で exit 2（deny）確定。worker プロセスは deny を受けて続行。再起動後の新規判定は正常 |
| crash を跨いだ Tier3 人間待ち | 当該呼び出しのみ | deny で確定。Slack に残る承認ボタンは押しても効かない（安全側・実害なし） |

**運用（launchd）**: label `com.taka-ma.decide-daemon`・`KeepAlive=true`（crash 自動再起動）。plist の環境変数で PYTHONPATH＝`approval-pipeline : ya-ta : orchestrator` を与える（`SlackNotifier` は orchestrator 配下。旧方式はこれが欠けて import 断線した）。config 再ロード＝リクエスト受信時に ya-ta.yaml / sa-ru.yaml / pipeline.yaml の mtime を照合し、変化していればパイプラインを再構築（デーモン再起動不要）。

**SSH 多重化（ControlMaster）**: フックの ssh に `-o ControlMaster=auto -o ControlPath=<socket> -o ControlPersist=600` を付け、handshake を初回のみに抑える。`~/.ssh/config` は書き換えず、フックコマンド内で完結させる。

**段階導入と計測**: 導入前（decide_cli コールドスタート）→ ControlMaster のみ → デーモン、の 3 点で「1 ツール呼び出しあたりの承認レイテンシ（Tier1 相当の即時判定）」を実機計測し、改善を数値で記録する（§15）。

実測（2026-07-05・MBP→Mac mini・always_deny 即時判定・各 3 回の中央値。SSH 接続＋判定プロセス起動のオーバーヘッドのみで、ya-ta / qu-e / Tier3 の判定時間は含まない）:

| 方式 | 1 判定あたり | 対旧比 |
|---|---|---|
| 旧 1 ショット（SSH 都度接続＋Python コールドスタート） | 0.26 秒 | 1.0x |
| ControlMaster のみ（接続再利用＋コールドスタート） | 0.09 秒 | 2.9x |
| デーモン（接続再利用＋常駐判定） | 0.05 秒 | 5.2x |

**decide_cli.py の扱い**: 撤去（1 ショット判定の入出力変換はデーモンへ吸収）。旧方式を温存すると到達不可時に「遅い経路へのフォールバック」を誘発してデーモン障害を隠蔽するため、到達不可は fail-closed（deny）に一本化する。

## 3. アダプタの入力変換（scrape 廃止 → 中核の PendingApproval）

各アダプタが自 CLI の承認要求を、中核の `PendingApproval{tool_name, tool_input, tool_use_id}` に変換する。**変換だけがアダプタの責務**で、変換後の判定（安全性チェック・classifier・qu-e）は中核（本体 §3）が扱う。

| アダプタ | 承認要求の取得 | → PendingApproval への変換 |
|---|---|---|
| headless（Claude） | PreToolUse フック stdin の JSON | `tool_name`/`tool_input`/`tool_use_id` をそのまま採る（変換不要・構造化済み） |
| interactive(pty) | レガシー y/n 検出＋context 抽出（旧 `extract_command` の Run:/Write to: 逆走査はこのアダプタ内に残す） | 抽出文字列を `tool_name`（推定）＋`tool_input` に整形 |

> 旧実装は stdout scrape の単一 `command` 文字列（`InterceptedPrompt.command`）を全 consumer が扱っていた。新方式では**中核が構造化 `PendingApproval` のみを受け**、scrape は interactive アダプタ内部の一手段に閉じる。中核から scrape 依存を除去する点は中核 Appendix §3 を参照。

## 4. 承認中核との接続（詳細は中核 Appendix）

承認判定（安全性チェック・スコープ・Tier1/2/3・§8.10 待ち・handler 返却契約・レコード schema）は **CLI 非依存の中核**であり、本 Appendix の対象外。詳細は設計書本体 §3。

本 Appendix（アダプタ）が中核と接続するのは次の2点のみ:
- **入力変換**: 自 CLI の承認要求を `PendingApproval{tool_name, tool_input, tool_use_id}` に変換して `decide()` へ渡す（headless=フック stdin、interactive=レガシー y/n の context 抽出）。
- **出力変換**: 中核が返す `Decision{allow, reason}` を自 CLI の伝達手段へ変換（headless=`permissionDecision:allow`/exit 2、interactive=`y`/`n` 送信）。

## 5. セキュリティモデル（headless アダプタが閉じる穴）

- 全ツールがフックを通る（実機確認）→ safe-command（echo 等の内蔵自動許可）が承認を素通りするバイパスを閉じる。
- フックは exact `tool_input` で判定（実機確認）→ allowedTools の finicky な rule 文法に非依存。過剰許可の窓が存在しない。
- フックは default-deny を上書き allow 可（実機確認）→ 事前許可リスト（base_rules）不要。`--permission-mode` は default、判定はフック→中核に一元化。
- `--dangerously-skip-permissions` は不使用維持。フック未設定/失敗時は fail-safe に deny。
- フック異常は exit 2 に集約（fail-closed・§2.1）。exit 2 以外の非 0 はフックエラーとして既定権限評価に落ち read 系が素通りし得るため、「判定不能＝deny」を終了コード契約で保証する。

## 6. interactive(pty) アダプタ — 汎用対話抽象の温存

**interceptor の撤去は全撤去ではなく Claude 固有部分のみに精緻化**する。

- **温存**: `WorkerPtyWrapper`（起動/send_task/approve/deny の pty 制御）と interceptor の**レガシー y/n 検出**（`PATTERNS` = YN/YES_NO/ALLOW、`extract_command`、`strip_ansi`）。これは agy 対話・将来 Codex 等の汎用対話 CLI アダプタとして機能し続ける。approve/deny はこのアダプタ内で `Decision` → y/n キー送信に変換する。
- **撤去**: interceptor の**Claude 固有 Ink 検出**（`classify_menu`/`MENU_CURSOR`/`_MENU_OPTION_RE`/`_TRUST_DIALOG_RE`/`_TOOL_PERMISSION_RE`/`PromptType.MENU`/`TRUST_DIALOG`）。Claude は headless アダプタへ移るため不要。
- Claude は `methods: [pty]` → `methods: [headless]` に変更。agy の `methods: [pty, subprocess]` は維持。

## 7. 非 Claude worker（subprocess アダプタ）のスコープ

- **agy（keychain・stdin 不可）**: `run_model_subprocess`→`_run_in_gui_tmux` を変更しない。`agy -p` 単発で per-tool 承認は無く、**対象外**（安全性はタスク分解/ルーティング段で担保）。interceptor 撤去の影響なし。
- **Gemini(API stdin)/ollama**: 直 ssh subprocess を変更しない。
- 分岐は `_select_method` と `keychain_auth`。

## 8. 無応答検知（court）の吸収

- 完了 = `result` イベント（無音ヒューリスティック `_IDLE_QUIET_SEC` 廃止）。
- ハング = `result` 無し終了（v2.1.163+ の5秒 grace kill）→ retry → 既存 fallback 列。ハング fallback とモデル障害 fallback（`max_fallback_attempts`）は**別カウンタ**で混同回避。
- livelock（拒否ツールの無限再要求）は単一プロセス＋インラインフックで構造的に消滅。過長時は per-process timeout。

## 9. cross_review 経路と `_select_method`

- `_execute_cross_review._run_one` の pty 分岐を、seam B 経由で headless アダプタ呼び出しに置換（Claude の場合）。
- `_select_method` に headless 分岐を追加（現状 `methods=["headless"]` は最終 `return "pty"` に落ちる）。`if "headless" in methods: return "headless"` を追加。
- heavy_limiter 占有: Tier3 人間待ちの長期スロット占有 → Tier3 待ち中の解放 or 到達数上限を §10.4/実装で対処。

## 10. モデルルーティング保持

ya-ta の `model_flag`（`--model`）・`command` を保持。headless アダプタは argv 配列で組立（`["claude","-p",task,"--model",name,"--output-format","stream-json","--verbose","--include-hook-events","--settings",hook]`）、シェル文字列連結を廃す。起動は SSH 経由 MBP（配列を安全にクォート）。

## 11. workspace 生成・trust

- cwd 生成再配置: 現行 `WorkerPtyWrapper.start()` 内 `mkdir -p` を、各実行アダプタ共通の**起動前 workspace 準備**（`_workspace_for(task_id)` を mkdir）へ移す。
- trust ダイアログ: headless で fresh workspace がハングしないか **test で明示確認**。ハング時は起動前 trusted 登録。「ハングなし」の担保対象。

## 12. 設計上の懸念と対応

| 懸念 | 対応 |
|---|---|
| 承認ループの収束保証（無限ループ・無限課金） | resume ループ廃止で構造的消滅（§8） |
| 承認後の再実行保証・継続プロンプト生成 | フックのインライン同期で resume 継続自体が不要になり消滅 |
| trust ダイアログのハング | §11 起動前 trust ＋ test 確認 |
| 過剰許可の窓 | フックが exact tool_input 判定（§5）で窓が存在しない |
| safe-command の承認素通り | フックが全ツール発火（§5）で捕捉 |
| Claude ロックイン | §1 抽象化3層・seam A/B。Claude は headless アダプタの1実装。中核 decide は CLI 非依存 |
| 権限機構が人間 y/n を待って allow/deny を返せるか | フックが待って返す＝充足 |
| handler の pty 直呼び | handler は `Decision` を返し物理伝達はアダプタ（§4）＝pty 切離し |
| cross_review・`_select_method` の headless 分岐 | §9 |
| §8.10 レコード schema の整合 | `command`整形文字列＋任意`tool_input`（§3・§4） |
| 事前許可リスト（base_rules）依存 | フック force-allow で依存消滅（§5） |
| qu-e の Write/Edit diff レビュー能力維持 | `--mode diff`維持（§3） |
| workspace mkdir の再配置 | 起動前準備へ再配置（§11） |
| retry/fallback 設計 | §8 で規定 |
| session_id 採取元 | resume 非主軸で影響縮小・使う場合 init から追跡（§13） |
| classifier プロンプト再調整 | operation 書式に合わせ`classify_risk.md`（§3） |
| heavy_limiter の長期占有 | §9 で設計判断 |
| 承認レイテンシのツール数比例累積 | §2.1 decide デーモン常駐＋ControlMaster 多重化 |
| フック判定プロセスの異常が fail-open になる | §2.1 終了コード契約（全異常を exit 2 へ集約） |

## 13. フォールバック（主軸にしない）

- `--resume`+`--allowedTools` 動的付与（v1 主軸）: フック優位のため降格。crash recovery/将来の分割実行で温存。使用時 session_id は system/init から追跡。
- `--permission-prompt-tool`（MCP 同期 allow/deny）: フックで足りるため不採用。

## 14. 改定が必要な設計書・手順書セクション

### 設計書 `design-development-system.md`
§2.1（PTY→アダプタ抽象＋headless）/ §3.1（基本方針: 実行アダプタ抽象・skip-permissions 不使用維持）/ §3.2（技術スタック: pexpect/expect→アダプタ別。headless は asyncio subprocess+stream-json+フック、interactive は pexpect 温存）/ §3.3（判定入力を構造化 PendingApproval へ）/ §3.4（承認フロー図: アダプタ→中核 decide）/ §8.4（classify 入力出所）/ §8.5（最大改定: 実行アダプタ3種の定義・Claude=headless・agy 分離）/ §8.9（Slack 構造化提示）/ §8.10（Decision の返し方・ポーリング維持）。要精読: §9.1/§9.2/§10.4。

### 手順書 `docs/procedures/`
05-orchestrator（実行アダプタ dispatch・headless runner・検証#3）/ 06-task-models（Claude methods pty→headless・起動コマンド・検証#4/#5）/ 08-approval-pipeline（中核 decide＋アダプタ変換・interceptor は Ink のみ撤去・handler 返却契約・テスト `test_interceptor.py` はレガシー y/n 分を維持）/ 04-ai-gateway（検証#17 methods）/ 00-overview。

### 本設計ノートの正本への統合
本 Appendix は設計書本体 §8.5 と双方向 markdown リンクで紐づく（既存 Appendix 慣習）。詳細フロー・実機検証結果・設計判断は本 Appendix、本体 §sections は方式サマリ＋抽象化3層（seam A/B）を記す。ファイル名・見出しは Claude 固有語（headless/stream-json）を主語にせず、抽象（実行アダプタ）を主語にする（ロックイン禁止の徹底）。

## 15. テストフェーズで確定する実機検証項目

1. 本番 fresh workspace の trust 非ハング（「ハングなし」の担保）。
2. フック timeout ≥310秒 で Tier3 300秒待ちが切れない。
3. フック → Tier3 §8.10 → approved→force-allow→実行、reject→exit2→未実行 の end-to-end。
4. SSH 経由 argv 配列＋フック settings パス解決。
5. ハング時 grace kill → retry → fallback。
6. cross_review の headless 化と heavy_limiter 占有実測。
7. **抽象化の不変条件**: 中核 `decide()`/`ApprovalPipeline` に CLI 固有語（stream-json/フック/pexpect/allowedTools 等）が grep で現れないこと。interactive アダプタのレガシー y/n が Claude 撤去後も機能すること。
8. **fail-closed**: decide デーモン停止・ソケット不在・SSH 不達のそれぞれでフックが exit 2 となり全ツールが deny されること（read 系の素通りが無いこと）。
9. **並行性**: 複数 worker の同時判定で、Tier3 人間待ち中のリクエストが他の判定をブロックしないこと。
10. **レイテンシ計測**: 導入前（1 ショット コールドスタート）／ControlMaster のみ／デーモン、の 3 点で 1 ツール呼び出しあたりの承認レイテンシを実測し、改善を数値で記録すること。
