# 詳細設計: 検証仕様

> **位置づけ**: [設計書本体](../design-development-system.md) の詳細。本書が扱う節: §11。
> 障害を契機とする設計改訂の経緯は [revisions/](../revisions/) を参照。

連携パス別の検証項目とエンドツーエンド検証シナリオを扱う。

---

<a id="sec-11"></a>
## 11. 検証仕様

<a id="sec-11-1"></a>
## 11.1 連携パス別の検証項目

各連携パスについて、以下のコマンドで実機検証を行う。モックテスト合格は「完了」ではない。

| # | 検証項目 | 検証方法 | 合格基準 |
|---|---------|---------|---------|
| V-01 | u-zu → sa-ru タスク投入 | `/taka-ma-task "テスト"` → `/opt/taka-ma/data/tasks/` にファイル出現 | JSON ファイルが作成され、status=init |
| V-02 | sa-ru タスク取得 | V-01 の後、sa-ru ログに `accepted` 記録 | タスクファイルの status が accepted に更新 |
| V-03 | ya-ta タスク分類 | sa-ru ログにタスク分類結果が記録 | execution, depth, confidence が出力 |
| V-04 | ya-ta リスク分類 | フック受領（tool_name/tool_input）後にリスク分類が実行 | tier, reason が出力 |
| V-05 | Claude Code headless 起動 | sa-ru → SSH → MBP で `claude -p --output-format stream-json --verbose` 起動 | stream-json の `system/init` に session_id が出力 |
| V-06 | PreToolUse フック発火 | Claude Code がツール実行を要求 → フックが構造化 JSON を stdin 受領 | フックログに `tool_name`/`tool_input` を記録 |
| V-07 | Tier 1 自動承認 | ファイル読み取り操作 → フックが自動 allow | 人間介入なしで実行続行（permissionDecision:allow） |
| V-08 | Tier 2 qu-e 審査 | Write tool_use → qu-e 呼び出し | SSH 経由で review_cli.py が実行され JSON 応答 |
| V-09 | Tier 2 → Tier 3 エスカレート | qu-e deny → Slack 承認リクエスト | Slack に Block Kit ボタンが表示 |
| V-10 | Tier 3 人間承認 | Slack で Approve ボタン → フック allow | 承認ファイルの status=approved、実行続行 |
| V-11 | 承認 pending の保留 | 猶予（`hold_grace_sec`）超過まで放置 | 承認ファイルは status=pending 存置＋`held_at` 追記、フック exit 2、worker は畳まれる、タスクは `completed` ではなく `pending_approval`、`completed_steps` が永続化、並行枠が解放、Slack に保留通知 |
| V-11a | 保留からの再投入 | V-11 の後に Slack で Approve | 承認ファイルが done/ へ退避され、未了サブタスクのみ再実行（`completed_steps` の step は再実行されない）。同じ workspace で継続 |
| V-11b | 保留からの却下 | V-11 の後に Slack で Reject | 当該操作のみ deny で再開・worker は代替 or 遂行不能報告（§8.10 却下の粒度）＋Slack 通知 |
| V-11c | sa-ru 再起動を跨ぐ保留 | V-11 の後に sa-ru を再起動し、その後 Approve | 保留が失われず再投入される（状態がディスク上で自己完結していること） |
| V-20 | 抽象化不変条件 | 承認判定中核 `decide()`/`ApprovalPipeline` を grep | stream-json/フック/pexpect/allowedTools 等の CLI 固有語が現れない |
| V-12 | タスク完了通知 | タスク実行完了 → Slack に通知 | `#taka-ma` に完了メッセージ |
| V-13 | Gemini フォールバック実行 | Opus 障害時に Gemini にフォールバック | Gemini の応答が Slack に通知 |
| V-14 | Gemini 高度なマルチモーダル解析 | 高度な解析タスク → GUI 起源 tmux 経由で agy 起動（§8.6） | Gemini の応答が Slack に通知 |
| V-15 | Gemma 4 31B 実行 | inline 判定（conf ≥ 閾値）→ SSH 経由 ollama run | Gemma 4 の応答が Slack に通知 |
| V-16 | 監査ログ記録 | 全操作後にログファイル確認 | 全判定が JSONL に記録 |
| V-17 | タスク分解 | 複合指示を投入 → サブタスクに分解される | 分解脳（qwen3.8:27b）が JSON 配列を返し、step/execution/depth/depends_on が含まれる |
| V-18 | 依存関係に基づく連鎖実行 | depends_on 付きサブタスクが依存完了後に実行される | 前のステップの結果が次の入力に組み込まれている |
| V-19 | 独立サブタスクの並行実行 | depends_on が空の複数サブタスクが同時実行される | ログで同時に in_progress になっている |
| V-21 | 出口ゲート PASS | acceptance 付きタスクを機械検査 PASS まで実行（`exit_gate:` 構成あり） | 完了通知の前に独立検証が走り、通知・結果ファイルに独立検証レポート（要件別判定表・採取コマンドと実出力）が添付される |
| V-21a | 出口ゲートの遮断 | V-21 の実行時に検証エージェントへ渡したプロンプトを記録・確認 | プロンプトに worker の自己申告テキスト（results）が含まれない（回答型の回答本文を除く）。根拠文書・採取証跡・機械検査証跡のみ |
| V-21b | 出口ゲート FAIL → 差し戻し | 要件を意図的に未解消のまま機械検査 PASS になるタスクを実行 | 完了通知が出ず status=init へ再投入、worker 指示に判定表が前置される。`exit_gate.max_reinject` 超過で ⚠ 未達（failure_cause=exit_gate_failed） |
| V-21c | 出口ゲート判定不能 | 検証エージェント出力を JSON 逸脱にする（モデル停止等） | 1 回リトライ後、差し戻さず ⚠ 未達（failure_cause=exit_gate_unverified）。「完了」の語が使われない |

<a id="sec-11-2"></a>
## 11.2 エンドツーエンド検証シナリオ

再構築完了時に以下の7シナリオが全て通ることを最終検証基準とする。

**シナリオ 1: 軽量タスク（Tier 1 自動承認）**

```
1. Slack: /taka-ma-task "このJSONをYAMLに変換して: {\"a\": 1}"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: inline 判定（conf ≥ 閾値 → gemma）
4. sa-ru → SSH → MBP: ollama HTTP API（/api/generate・keep_alive で常駐）
5. Gemma 4 → 結果返却
6. sa-ru → Slack: 結果通知
```

**シナリオ 2: 重量タスク + Tier 2 承認**

```
1. Slack: /taka-ma-task "src/app.ts にログインフォームを実装して"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: agent/deep 判定（→ opus）
4. sa-ru → SSH → MBP: Claude Code headless 起動（claude -p stream-json）
5. Claude Code: Write tool_use 要求 → PreToolUse フック発火
6. フック: {tool_name:"Write", tool_input:{file_path:"src/app.ts"}} を中核へ
7. sa-ru → ya-ta: Tier 2 判定
8. sa-ru → SSH → MBP: qu-e レビュー
9. qu-e: approve → フックが permissionDecision:allow
10. Claude Code: 続行 → result で完了
11. sa-ru → Slack: 結果通知
```

**シナリオ 3: 重量タスク + Tier 3 人間承認**

```
1. Slack: /taka-ma-task "本番サーバーにデプロイして"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: agent/deep 判定（→ opus）
4. sa-ru → SSH → MBP: Claude Code headless 起動
5. Claude Code: Bash tool_use 要求（deploy.sh --production）→ PreToolUse フック発火
6. フック: {tool_name:"Bash", tool_input:{command:"deploy.sh --production"}} を中核へ（安全性 always_escalate 該当）
7. sa-ru → ya-ta: Tier 3 判定
8. sa-ru → Slack: Block Kit 承認リクエスト送信
9. ユーザー: Approve クリック
10. u-zu → 承認ファイル更新
11. sa-ru: ポーリング検知 → フックが permissionDecision:allow
12. Claude Code: 続行 → result で完了
13. sa-ru → Slack: 結果通知
```

**シナリオ 4: 承認 pending で保留し、後から承認して未了分から再投入**

```
1. シナリオ3の手順8まで同じ
2. 猶予（hold_grace_sec）内に応答なし
3. sa-ru: 承認ファイルを pending 存置のまま held_at を追記 → フックが exit 2
4. worker: ツール呼び出しを諦めて正常終了（result・is_error=false）
5. sa-ru: 済んだサブタスクの結果を completed_steps としてタスクファイルへ永続化し、
        タスクを pending_approval へ（completed にしない）→ 並行枠を解放
        → Slack に保留通知（期限が無いことを明示）
6. （時間経過。sa-ru が再起動しても保留は残る）
7. 人間: Slack で Approve → u-zu が承認ファイルを approved に更新
8. sa-ru: 決着を検知 → 承認ファイルを done/ へ退避 → タスクを status=init へ戻して再投入
9. dispatcher: 凍結プランのうち completed_steps に無い step だけを実行
        （同じ workspace に前回の成果物が残っている）
10. sa-ru → Slack: 再開通知 → 結果通知
    （7 で Reject の場合: 当該操作のみ deny で再開し、worker は代替 or 遂行不能報告 — §8.10 却下の粒度）
```

**シナリオ 5: agent/deep 推論タスク（コードベース解析・アーキテクチャ評価等）**

```
1. Slack: /taka-ma-task "プロジェクト全体のアーキテクチャを評価して"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: 分解 → サブタスク1件（agent/deep → opus）
4. sa-ru → SSH → MBP: Claude Code headless 起動（claude -p stream-json、Opus）
5. Claude Code → result で結果返却
6. sa-ru → Slack: 結果通知
```

**シナリオ 6: cross-review（複数モデル並行投入 → 統合）**

```
1. Slack: /taka-ma-task "設計をレビューして :opus :gemini"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: agent 判定 + cross-review（:opus :gemini 並行投入）
4. orchestrator: Opus（Claude Code, headless アダプタ）と Gemini 3.1 Pro（Antigravity CLI, subprocess アダプタ）を asyncio.gather で並行起動
5. 各モデルの結果を sa-ru が受信（部分成功も許容、失敗モデルは Slack 通知）
6. sa-ru → ya-ta: 分解脳（qwen3.8:27b）で結果を知的統合
7. sa-ru → Slack: 統合結果を通知
```

**シナリオ 7: 複合タスク（オーケストレーション）**

```
1. Slack: /taka-ma-task "プロジェクトを解析して、問題点を改修して"
2. u-zu → タスクファイル作成
3. sa-ru → ya-ta: 分解 →
   Step 1: プロジェクト解析 (agent/deep, depends_on: [])
   Step 2: 問題点改修 (agent/deep, depends_on: [1])
4. Step 1: sa-ru → SSH → MBP: Claude Code headless 起動 → 解析結果取得
5. Step 2: sa-ru → SSH → MBP: Claude Code headless 起動
   入力: "解析結果: {Step 1 の出力}\n上記を踏まえて改修して"
6. Claude Code: tool_use → PreToolUse フック → 承認パイプライン → 実行 → result で完了
7. sa-ru → Slack: 結果通知
```

---

