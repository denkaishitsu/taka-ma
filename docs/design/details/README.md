# 詳細設計書（details）

> **位置づけ**: [基本設計書](../design-development-system.md) が定める骨格の、部位別の仕様。
> 節番号（§）は設計書全体で一意で、全節の総目次は [design-development-system.md](../design-development-system.md) にある。

| 文書 | 扱う範囲 | 節 |
|------|---------|-----|
| [03-approval-pipeline.md](03-approval-pipeline.md) | 承認の中核（Tier 1/2/3）、リスク判定、承認フロー、監査ログ | §3 |
| [04-sentinel.md](04-sentinel.md) | 守護プロセス qu-e の使用モデルと役割 | §4 |
| [06-infrastructure.md](06-infrastructure.md) | IaC・リポジトリ構造・インストール来歴と LIFO アンインストール | §6 |
| [07-task-models.md](07-task-models.md) | 各マシンのメモリ配分・モデル選定・モデル自動監視・半自動入替 | §7 |
| [08-conversation-gateway.md](08-conversation-gateway.md) | 会話ゲートウェイ（人の依頼 → 実行契約）。会話と実行の分離、着手確認、意図のドリフト検出、受け渡し契約、決定的実行 | §8.3・§8.10b・§8.10e〜g |
| [08-task-routing.md](08-task-routing.md) | ya-ta のタスク分解・分類・リスク判定・契約化・判定ログ | §8.4 |
| [08-worker-execution.md](08-worker-execution.md) | worker CLI の起動・監視・回収、実行アダプタ抽象 | §8.5〜§8.7 |
| [08-worker-execution-adapters.md](08-worker-execution-adapters.md) | 実行アダプタ（subprocess / interactive / headless）の内部設計と実機根拠（§8.5 の付属文書） | — |
| [08-sentinel-paths.md](08-sentinel-paths.md) | sa-ru ⇄ qu-e の通信経路（Tier 2 審査・監査・文脈共有・資源最適化通知） | §8.8・§8.11〜§8.14 |
| [08-resource-optimization-flow.md](08-resource-optimization-flow.md) | リソース最適化通知の関数名つきフロー図（§8.14 の付属文書） | — |
| [08-slack-interface.md](08-slack-interface.md) | Slack 通知・承認結果・制御コマンド・Socket Mode と sa-ru の死活監視・G2 チャネル | §8.9・§8.10・§8.10c/d・§8.16・§8.17 |
| [10-orchestration.md](10-orchestration.md) | DAG 実行、並行制御、結果の受け渡し、常駐ループの堅牢性 | §10 |
| [10-orchestration-flow.md](10-orchestration-flow.md) | オーケストレーションの関数名つきフロー図（§10 の付属文書） | — |
| [11-verification.md](11-verification.md) | 連携パス別の検証項目と E2E 検証シナリオ | §11 |

障害を契機とする設計改訂の**経緯**は [revisions/](../revisions/) にある。仕様の正本は本ディレクトリ側。
