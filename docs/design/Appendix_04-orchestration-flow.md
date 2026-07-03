# sa-ru オーケストレーション処理フロー

構築手順書04 Step 8 の [`src/orchestrator/__init__.py`](../../src/orchestrator/__init__.py) 骨格に対応するフロー図。
ノードには関数名（必要に応じて関数内の処理概要を `関数名() — 処理概要` 形式で）、アローに値を記載。
関数名は構築手順書 04 内で grep して該当箇所へ移動可能。

## 起動

```mermaid
flowchart LR
    A["run()"] -->|"asyncio.gather()"| B["dispatcher"]
    A -->|"asyncio.gather()"| W1["worker_light<br>（キュー待機）"]
    A -->|"asyncio.gather()"| W3["worker_heavy<br>（キュー待機）"]
```

3つの非同期関数を同時起動。ワーカーはキューにアイテムが入るまで待機。

## データフロー(メイン処理)

```mermaid
flowchart TD
    B["_dispatcher()<br>永久ループ（POLL_INTERVAL）"]
    B -->|"日付変更時"| CL["_daily_cleanup()<br>done/ ローテート"]
    CL --> B

    B -->|"ポーリング"| C["task_q.claim()<br>init を picked → accepted 予約（FileQueue）"]
    C -->|"なし"| B
    C -->|"(ファイルパス, task dict)"| ST["_update_status()<br>init → in_progress"]
    ST --> D["decomposer.decompose()<br>DeepSeek-R1 がサブタスク分解"]
    D -->|"[{step, command, category,<br>depends_on}, ...] × 1〜N件"| EX{"_dispatcher() — dry_run？"}
    EX -->|"yes (/exam_gw)"| FORM["_format_exam_result()<br>判定結果のみ Slack 通知<br>completed"]
    FORM --> B
    EX -->|"no"| E["_dispatcher() — asyncio.create_task()<br>_execute_chain を非同期起動<br>（dispatcher はブロックしない）"]
    E --> B

    style B fill:#E1F5EE,color:#000
    style D fill:#EEEDFE,color:#000
```

## データフロー(サブタスク並行起動)

`_execute_chain()` が全サブタスクを `asyncio.gather` で一斉起動する。
依存のないサブタスクは即座に実行され、依存のあるサブタスクは `await futures[dep]` で待機する。

```mermaid
flowchart TD
    F["_execute_chain()<br>全サブタスクの Future 生成"]
    F --> G["_execute_chain() — asyncio.gather(*pending_tasks,<br>return_exceptions=True)<br>全サブタスクを一斉起動<br>※ 失敗タスクがあっても他のタスクは中断しない"]

    G --> S1["subtask 1<br>depends_on: [ ]<br>→ 即座に実行"]
    G --> S2["subtask 2<br>depends_on: [ ]<br>→ 即座に実行"]
    G --> S3["subtask 3<br>depends_on: [1, 2]<br>→ await futures[1], futures[2]"]
    G --> S4["subtask 4<br>depends_on: [3]<br>→ await futures[3]"]

    S1 -->|"完了 → futures[1].set_result()"| S3
    S2 -->|"完了 → futures[2].set_result()"| S3
    S3 -->|"完了 → futures[3].set_result()"| S4

    style S1 fill:#E6F1FB,color:#000
    style S2 fill:#E6F1FB,color:#000
    style S3 fill:#FAEEDA,color:#000
    style S4 fill:#FAEEDA,color:#000
```

## データフロー(連鎖実行 — 各サブタスクの処理経路)

```mermaid
flowchart TD
    F["_execute_chain()<br>全サブタスクの Future 生成<br>asyncio.gather で一斉起動"]
    F -->|"サブタスクごと"| G["_execute_subtask_in_chain()"]

    G -->|"depends_on あり"| H{"_execute_subtask_in_chain() — await futures[dep]<br>依存先の完了待ち"}
    G -->|"depends_on 空"| I["_execute_subtask_in_chain() — Slack通知 → キュー投入"]

    H -->|"依存先が成功"| DEP["_execute_subtask_in_chain() — 依存結果を<br>command に組み込む"]
    H -->|"依存先が失敗"| SKIP["_execute_subtask_in_chain() — cascading skip<br>futures[step] に例外セット"]
    DEP --> I

    I -->|"queue_item"| ENQ["_enqueue()<br>category でキュー振り分け"]

    ENQ -->|"light"| QL["queue_light"]
    ENQ -->|"heavy"| QH["queue_heavy"]

    style SKIP fill:#FCEBEB,color:#000
    style ENQ fill:#FAEEDA,color:#000
```

## データフロー(ワーカー実行 — 配列フォールバック / cross-review)

```mermaid
flowchart TD
    QL["queue_light"] -->|"get()"| W1["_worker_light()<br>create_task（並行制限なし）"]
    QH["queue_heavy"] -->|"get()"| W3["_worker_heavy()<br>Semaphore acquire"]

    W1 --> EX["_execute_worker_task()"]
    W3 -->|"create_task"| REL["_execute_heavy_with_release()<br>try/finally で Semaphore 解放<br>※ 例外時も枠を返却し後続を詰まらせない"]
    REL --> EX

    EX -->|"_model が 2 つ以上のリスト"| CR["_execute_cross_review()<br>並行投入 → ya-ta で統合"]
    EX -->|"_model 単一 or 未指定"| CAND["_execute_worker_task() — 候補リスト構築<br>明示指定→[user_specified] のみ<br>未指定→category_defaults を<br>max_fallback_attempts で制限"]

    CAND --> LOOP["_execute_worker_task() — 候補ループ<br>for idx, model_name in enumerate(candidates)<br>is_fallback = idx > 0"]

    LOOP -->|"_select_method() → subprocess"| SUB["process_mgr.run_model_subprocess<br>(Antigravity CLI 単発 / Gemma / 外部サービス)"]
    LOOP -->|"_select_method() → pty"| PTY["_run_worker_pty()<br>汎用 WorkerPtyWrapper + y/n承認パイプライン<br>(Claude Code / Antigravity CLI 対話 / Codex 等)"]

    SUB -->|"output"| OK["_execute_worker_task() — result_future.set_result()"]
    PTY -->|"output"| OK

    SUB -->|"例外"| FB["_execute_worker_task() — Slack 通知<br>「{model} 障害」<br>continue → 次候補へ"]
    PTY -->|"例外"| FB
    FB --> LOOP

    LOOP -->|"全候補失敗 (light)"| UP["_execute_worker_task() — heavy に昇格<br>→ _enqueue() 再投入"]
    UP -->|"再投入<br>(heavy 候補配列で再フォールバック)"| QH
    LOOP -->|"全候補失敗 (heavy)"| FAIL["_execute_worker_task() — result_future.set_exception()"]

    OK -.->|"後続"| NEXT_OK(["結果回収・完了判定図<br>OK ノードへ"])
    FAIL -.->|"後続"| NEXT_FAIL(["結果回収・完了判定図<br>FAIL ノードへ"])

    style UP fill:#FAEEDA,color:#000
    style FAIL fill:#FCEBEB,color:#000
    style OK fill:#EAF3DE,color:#000
    style CR fill:#EEEDFE,color:#000
```

## データフロー(cross-review — 並行投入と統合)

```mermaid
flowchart TD
    CR["_execute_cross_review(item, models)<br>※ 明示指定扱い、個別 fallback なし"]
    CR --> RUNS["_execute_cross_review() — asyncio.gather(*[_run_one(m) for m in models])<br>各モデルを並行投入<br>各々が heavy Semaphore を個別取得"]

    RUNS --> R1["_run_one('opus')<br>_select_method(use_case='cross_review') → pty<br>→ _run_worker_pty"]
    RUNS --> R2["_run_one('gemini')<br>_select_method(use_case='cross_review') → subprocess<br>→ run_model_subprocess"]
    RUNS --> RN["_run_one(...)<br>(N モデル)"]

    R1 -->|"(model, output|Exception)"| GATHER["results: list[(model, output|Exception)]"]
    R2 --> GATHER
    RN --> GATHER

    GATHER --> INTEG["_integrate_cross_review(command, results)<br>ya-ta（DeepSeek-R1 32B）で知的統合<br>部分成功許容、失敗モデルは Slack 通知"]
    INTEG --> OK["result_future.set_result(integrated_output)"]
    OK -.->|"後続"| NEXT_OK(["結果回収・完了判定図<br>OK ノードへ"])

    style RUNS fill:#EEEDFE,color:#000
    style INTEG fill:#FAEEDA,color:#000
    style OK fill:#EAF3DE,color:#000
```

## データフロー(結果回収・完了判定)

```mermaid
flowchart TD
    OK["result_future.set_result(output)"] --> RES["_execute_subtask_in_chain() — results[step] = output<br>futures[step].set_result()<br>→ 依存先の await が解除"]
    FAIL["result_future.set_exception()"] --> SKIP["cascading skip<br>→ 依存先の await が例外で解除"]

    RES --> DONE{"_execute_chain() — 全サブタスク成功？<br>(failed_steps が空？)"}
    SKIP --> DONE

    DONE -->|"全成功"| COMP["_execute_chain() — status → completed<br>Slack に結果通知<br>done/{日付}/ に移動"]
    DONE -->|"1つでも失敗"| NOTI["_execute_chain() — status → failed<br>_notify_failure()<br>元の指示 + 各Step成否を Slack 通知<br>done/{日付}/ に移動"]

    style COMP fill:#EAF3DE,color:#000
    style NOTI fill:#FAECE7,color:#000
    style SKIP fill:#FCEBEB,color:#000
```
