# リソース最適化通知 処理フロー（設計書 §8.14）

設計書 [§8.14 qu-e → sa-ru（リソース最適化通知）](design-development-system.md) に対応する処理フロー図。
qu-e（MBP）が推奨 heavy 並行数を算出して sa-ru（Mac mini）へ SSH push し、sa-ru が heavy 並行数上限を動的更新する経路を示す。

ノードには関数名（必要に応じて `関数名() — 処理概要` 形式）を記載。関数名は構築手順書 05（sa-ru）/ 07（qu-e）およびソースで grep して該当箇所へ移動できる。

- qu-e 側実装: [`src/sentinel/main.py`](../../src/sentinel/main.py) / [`resource_optimizer.py`](../../src/sentinel/resource_optimizer.py)
- sa-ru 側実装: [`src/orchestrator/__init__.py`](../../src/orchestrator/__init__.py) / [`concurrency.py`](../../src/orchestrator/concurrency.py)

```mermaid
flowchart TD
    subgraph QUE["qu-e（MBP）— src/sentinel/"]
        A["resource_notify_loop()<br/>main.py（notify_interval_sec=30 の永久ループ）"]
        B["ResourceOptimizer.notify_payload()<br/>resource_optimizer.py — level 分類 + 下記を内部呼出"]
        B2["recommended_heavy_instances()<br/>resource_optimizer.py — psutil でメモリ率→推奨数"]
        C{"recommended != last_sent ?<br/>resource_notify_loop()"}
        D["_push_resource_notify()<br/>main.py — ssh で json 書込"]
        A --> B --> B2 --> C
        C -->|"変化あり"| D
        C -->|"変化なし"| A
        D -->|"成功時 last_sent 更新"| A
    end

    J[/"recommended-notify json（uuid.json）<br/>/opt/taka-ma/data/resource-notify"/]
    D -->|"SSH push（NF-01: 通信は SSH）"| J

    subgraph SARU["sa-ru（Mac mini）— src/orchestrator/"]
        E["Orchestrator.run()<br/>__init__.py — ResourceNotifyHandler + Observer 起動"]
        F["ResourceNotifyHandler.on_created()<br/>__init__.py — json 読込 → done/ 退避"]
        G["DynamicConcurrencyLimiter.set_limit()<br/>concurrency.py — _limit 更新 + notify_all()"]
        H["_worker_heavy()<br/>__init__.py — heavy キュー消費の永久ループ"]
        I{"_active &gt;= _limit ?<br/>DynamicConcurrencyLimiter.acquire()"}
        K["_execute_heavy_with_release()<br/>__init__.py — finally: await release()"]
        E -.->|"watchdog で notify_dir 監視"| F
        F -->|"run_coroutine_threadsafe(set_limit)"| G
        H --> I
        I -->|"満杯: await wait()"| I
        I -->|"空き: _active += 1 → heavy 起動"| K
        K -->|"完了: release() → notify_all()"| H
        G -.->|"上限変更（待機を起こす / 新規を絞る）"| I
    end

    J -->|"watchdog 検知"| F

    style A fill:#E1F5EE,color:#000
    style H fill:#E1F5EE,color:#000
    style C fill:#FAEEDA,color:#000
    style I fill:#FAEEDA,color:#000
    style J fill:#E6F1FB,color:#000
    style G fill:#EAF3DE,color:#000
```

## ノード → 実体の対応

| ノード | ファイル | 関数 |
|--------|---------|------|
| A / D | `src/sentinel/main.py` | `resource_notify_loop()` / `_push_resource_notify()` |
| B / B2 | `src/sentinel/resource_optimizer.py` | `notify_payload()` / `recommended_heavy_instances()` |
| J | （SSH 転送される JSON） | `resource_optimization.notify_dir`（sa-ru）= `o_moi_notify_dir`（qu-e） |
| E / F / H / K | `src/orchestrator/__init__.py` | `Orchestrator.run()` / `ResourceNotifyHandler.on_created()` / `_worker_heavy()` / `_execute_heavy_with_release()` |
| G / I | `src/orchestrator/concurrency.py` | `DynamicConcurrencyLimiter.set_limit()` / `acquire()`（`release()` も） |

配色は CLAUDE.md 作図ルール準拠（永久ループ=薄緑、分岐=薄オレンジ、独立データ=薄青、上限反映=成功緑、いずれも文字色 #000）。
