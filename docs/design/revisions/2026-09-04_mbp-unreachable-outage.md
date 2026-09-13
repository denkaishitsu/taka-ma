# 是正記録 2026-09-04: MBP 到達不能時の無言縮退と sa-ru ハング

> **位置づけ**: 障害を機に設計を変えた経緯の記録。変更後の仕様そのものは詳細設計書（下表「設計反映マップ」の移送先）が正本。

**Status**: Accepted（2026-09-05 の検討でユーザー合意。実装は #taka-ma/163 配下）

**Context**: 2026-09-04、ユーザーが MBP（実行機）を携行して自宅 LAN を離れた間、司令塔 mac-mini から MBP への SSH が約 7 時間通らなかった。ハーネスはこの状態を「実行不可」と即答せず、ローカル LLM へ無言で縮退して誤った契約と長い待ちを生み、さらに断が回復した後も sa-ru が沈黙したまま復帰しなかった。

**タスク**: 親 #taka-ma/163、子 163.1（SSH 上限・診断）→ 163.2（到達性ゲート・縮退廃止）→ 163.3（心拍・自己復帰・ログ規律）。着手順もこの順。

## 実測（判定根拠）

| 事実 | 根拠 |
|------|------|
| mac-mini → MBP（Tailscale 経由の SSH・22/tcp）が 11:32〜18:46 接続タイムアウト | mac-mini `sa-ru.log`（ResourceMonitor 失敗 756 回、`ya-ta.contractor` の `Operation timed out`） |
| MBP → mac-mini も 11:00〜18:46 ほぼ全回 `ssh timeout` | MBP `qu-e.log` network 項目（毎時 108 回） |
| MBP はスリープしておらず起動中 | `pmset -g log` にスリープ/復帰なし、バッテリー駆動 10:03〜18:47（83%→66%）、10:45 蓋開放。qu-e ヘルスチェックが 30 秒周期で無空白 |
| 契約化 opus CLI が SSH 不達で 2 回失敗 → ローカル qwen3.8:27b へ縮退 | `sa-ru.log` 12:03:57 / 12:05:12 / 16:59:14 / 17:00:29 の `契約化 CLI が 2 回とも呼び出し失敗 → ローカル縮退` |
| 縮退契約は対象リポ名を誤記（`obsidian-auto-trader`）、完了条件なし | Slack スレッド（#obsidian-auto-stock-trader 2026-09-04 17:03 の着手確認） |
| 発話から着手確認まで 5〜6 分、30 秒ごとの進捗連投 10 件以上 | 同スレッド。内訳: TCP タイムアウト約 75 秒 × 2 + ローカル生成 |
| 会話返信も qwen3.6 が生成（「会話履歴を閲覧する権限がない」等） | `sa-ru.log` 12:00:06 ほか。Claude には一度も到達していない |
| sa-ru は 9/4 18:46:59 を最後に全 8 ループが例外なく沈黙、プロセスは生存 | `sa-ru.log` 最終行、`launchctl list` PID 生存、`sample` で全スレッドがロック待ち。9/5 05:32 手動 `kickstart -k` まで復帰せず |
| 同種の SSH 断は 8/15・8/16・8/31・9/1・9/3 にも発生、自宅 LAN 内でも深夜に断続 | `sa-ru.log` の日別 ResourceMonitor 失敗数、`qu-e.log` 9/4 00:00〜10:20 の断続 timeout |
| 正常時の到達性検査コストは 0.23〜0.26 秒、会話 1 ターン約 50 秒 | 9/5 実測 `ssh mbp true` × 3、9/3 21:57 の会話ログ |

## 判断一覧

| 判断 | 内容 | 理由 |
|------|------|------|
| 縮退の廃止 | worker CLI の呼び出し自体の失敗（SSH 不達・認証失効）でローカル `ya-ta.model` へ縮退しない。fail-closed で「実行機（MBP）到達不能のため契約化・実行不可。復旧後に再送してください」と一行で返す | 縮退契約は「無い」より悪い（誤リポ名・完了条件なし）。障害を隠して人の時間を奪った。設計書 §8.4「縮退」項および §8.4 表の「CLI 不達時のみローカル dense へ縮退」を改訂する |
| 契約化前の到達性ゲート | `AuthPreflight` に ssh 検査だけを行う入口を設け、`Contractor.contract` の前に呼ぶ。Anthropic プローブは契約化前には走らせない | 0.25 秒・10 分に 1 回（`pass_ttl_sec`）の負担で、不達時は 15 秒以内の即答になる。既存キャッシュ（キー `ssh`・`fail_ttl_sec`）を共用し新機構を作らない |
| 到達性の機械付与 | sa-ru が MBP の最終疎通時刻を保持し、不達中の会話返信の先頭に固定文「MBP 到達不能（最終疎通 HH:MM）」を付ける | ローカル脳に到達性を推測・創作させない（権威はフィールド・実測回答の原則 §8.10g と同じ） |
| SSH 上限の一括化 | `ssh_config.j2` のクラスタ用 Host に `ConnectTimeout 10` / `ServerAliveInterval 15` / `ServerAliveCountMax 2` / `BatchMode yes` | 約 30 箇所の ssh 呼び出しに配備で一括して効く。コード側は timeout 無しの subprocess 呼び出し（`process_manager.py` の tmux kill / ollama run ほか）を個別に塞ぎ、AST の回帰テストで再混入を止める |
| 診断手段の常設 | sa-ru 起動時に `faulthandler.register(SIGUSR1, all_threads=True)`。`/opt/taka-ma-env` に py-spy を配備。runbook に「再起動前にスタック採取」を追記 | 今回 Python スタックが取れず、ハング機序（スレッドプール枯渇の疑い）を断定できなかった。再発時に確定させる |
| 心拍（2 探針） | イベントループ上の専用コルーチン 1 本が 30 秒ごとに (a) 時刻を `sa-ru.heartbeat` へ上書き (b) `to_thread` で空関数を投げ 10 秒で戻らなければ「スレッドプール枯渇」を記録 | (a) はイベントループ閉塞、(b) は 9/4 型（ループは生きているが to_thread が全部詰まる）を検知。各ループの周回を数える案は、`queue.get()` で待機中の暇なループを誤検知するため不採用 |
| 検知主体と復帰 | **§8.16（u-zu の Socket Mode 死活監視）と同じ型を採る**: sa-ru プロセス内の daemon スレッドが心拍の前進を監視し、閾値（5 分）を超えたら CRITICAL ログ後に `os._exit` で異常終了 → launchd `KeepAlive` が再起動する。外部 launchd ジョブは追加しない | 既存パターンとの整合。daemon スレッドはイベントループにもスレッドプールにも依存しないため、両方が死んでいても動く。新しい常駐ジョブ・配備物を増やさない |
| 再起動の抑制 | 再起動回数を `sa-ru.restart-count` へ永続化し、1 時間に 3 回を超えたら自己終了せず CRITICAL と Slack 通知（初回と打ち切り時のみ）に切り替える。通知に MBP 最終疎通時刻を併記 | 長時間の到達不能中に原因側の修正が不十分で再ハングした場合の「kick され続ける」往復を止める。到達不能そのものは心拍を止めないため、原因側修正後は kick が起きないことが前提 |
| ログ規律 | 心拍はファイル上書きのみでログ 0 行。`resource_monitor` の失敗と qu-e の「ヘルスチェック: healthy」を状態遷移時のみ 1 行に改める | 9/4 は同一 traceback を 756 回×20 行超、qu-e は平常報告を 1 日 2,880 行書いており、肝心の沈黙が埋もれた。監視を増やしてもログを増やさない |
| 設計→コードの順序 | 各子タスクは設計書の該当 § を先に改訂し、review-verify で設計⇄コードの対応を検証してから実装する | 継ぎ接ぎ・場当たりの是正を再び継ぎ接ぎで行わないため |

## 設計反映マップ（子タスクが改訂する箇所）

| 子タスク | 設計書・配備物 | 改訂内容 |
|---------|--------------|---------|
| 163.1 | [details/08-worker-execution.md](../details/08-worker-execution.md) §8.5（資源回収・SSH 呼び出しの上限）、[docs/procedures/02-ssh-tunnel.md](../../procedures/02-ssh-tunnel.md)、[ADR 0001](../../adr/0001-ssh-tunnel-design-decisions.md) | クラスタ SSH の上限値を仕様化。timeout 無し ssh の禁止を明文化 |
| 163.1 | [docs/operations/runbook-shutdown-restart.md](../../operations/runbook-shutdown-restart.md) | ハング時は `kill -USR1` でスタック採取 → py-spy → 再起動、の順を追記 |
| 163.2 | [details/08-task-routing.md](../details/08-task-routing.md) §8.4 表「LLM バックエンド」行、§8.4「契約化の呼び出し」の「縮退」項、§8.4「昇格・縮退」箇条（1209 行付近）、§8.4.1 判定ログ | 縮退の廃止、fail-closed の返信文、到達性ゲート、判定ログの項目置換（縮退率 → 不達停止件数） |
| 163.2 | [details/08-conversation-gateway.md](../details/08-conversation-gateway.md) §8.3 会話（返信の機械付与）、§8.10g 実測回答 | 「MBP 到達不能（最終疎通 HH:MM）」の固定行付与 |
| 163.3 | [details/08-slack-interface.md](../details/08-slack-interface.md) §8.16.1「sa-ru の死活監視」、[details/06-infrastructure.md](../details/06-infrastructure.md) §7.1 リソース監視、[docs/procedures/05-orchestrator.md](../../procedures/05-orchestrator.md) / [07-sentinel.md](../../procedures/07-sentinel.md) | 心拍 2 探針・daemon スレッド・自己終了・再起動抑制・通知条件・ログ規律。qu-e ヘルスチェックの記録条件 |
| 163.3 | `sa-ru.yaml` | `liveness.probe_interval_sec` / `probe_timeout_sec` / `stale_threshold_sec` / `check_interval_sec` / `heartbeat_path` / `restart_count_path` / `restart_limit_per_hour` を yaml 唯一の源として追加（コード側既定値なし・§8.16 と同じ規約。ブロック名は既存の `heartbeat`（§10.8 進捗通知）と衝突するため `liveness`） |

## 完了条件（分離実測で担保する）

| 子タスク | 検査 |
|---------|------|
| 163.1 | mac-mini から到達不能ホストへの ssh が 10 秒で失敗する。timeout 無し ssh 検出テストが現行コードで PASS。`kill -USR1` で sa-ru の全スレッドスタックが `sa-ru-error.log` に出る |
| 163.2 | MBP 不達を模擬（`ssh_host` を到達不能ホストへ差替）した分離テストで、発話→返信が 20 秒以内、縮退契約が生成されない、返信先頭に到達不能行がある。到達可能時の会話 1 ターン所要が現状比 +1 秒以内 |
| 163.3 | 実プロセスでイベントループを塞いだ分離実測（`time.sleep` で閉塞）で、閾値＋確認周期以内に CRITICAL 記録→`exit 1`（2026-09-05 実測: 閾値 3 秒＋周期 1 秒で 5 秒後に exit 1）。配備後は心拍ファイルの `ts` が `probe_interval_sec` ごとに更新され、`kill -USR1` でスタックが採れることを実機で確認（2026-09-05 実施）。到達不能を模擬した 30 分の実測で再起動 0 回（心拍は止まらない）。平常 1 時間の `sa-ru.log` / `qu-e.log` 増分が各 10 行以内。**注**: `SIGSTOP` による検証は不可（SIGSTOP は監視スレッドも含む全スレッドを止めるため、プロセス内監視では原理的に検出できない。当初の記述は誤り） |

## 未検証事項（正直に残す）

- 9/4 のハング機序は「timeout 無し ssh でスレッドプールが枯渇し、全ループの `to_thread` が順番待ちで止まった」とする仮説であり、Python スタック未取得のため断定していない。163.1 の診断手段で再発時に確定させる
- MBP 側で経路が無かった原因（携帯回線に未接続か、Tailscale の中継が張れなかったか）は Tailscale のログが空で未特定。自宅 LAN 内の深夜の断続 timeout も原因未特定（本 ADR の範囲外、別タスク候補）
- `orchestrator` 以外のモジュールの ssh 呼び出しの timeout 有無は未実測（163.1 で実測する）

## Consequences

- **Pro**: 到達不能が「即答して止まる」に変わり、誤った契約と長い待ちが消える
- **Pro**: 沈黙が 1 分以内に検知され、人が気づかなくても復帰する。再起動の往復は上限で止まる
- **Pro**: 監視を増やしてもログは増えず、異常だけが残る
- **Con**: ローカル契約化という退路が無くなるため、MBP 不達中は契約化・実行が一切できない（意図した制約。会話のみ継続）
- **Con**: 自己終了型の復帰は実行中タスクを失敗として終わらせる。ハングしている時点で進んでいないため許容する

## 詳細設計書から移した記述（原文）

| 反映節 | 詳細設計書 | 原文 |
|--------|-----------|------|
| §8.3 | [conversation-gateway](../details/08-conversation-gateway.md) | 是正記録 2026-09-04 |
| §8.3 | [conversation-gateway](../details/08-conversation-gateway.md) | 2026-09-04 実測: 不達中にローカル脳が「会話履歴を閲覧する権限がない」等を創作した |
| §8.10f | [conversation-gateway](../details/08-conversation-gateway.md) | 是正記録 2026-09-04 |
| §8.4 | [task-routing](../details/08-task-routing.md) | CLI 不達時は fail-closed・縮退しない — 是正記録 2026-09-04 |
| §8.4 | [task-routing](../details/08-task-routing.md) | 契約化の前・是正記録 2026-09-04 |
| §8.4 | [task-routing](../details/08-task-routing.md) | 縮退の廃止・是正記録 2026-09-04 |
| §8.4 | [task-routing](../details/08-task-routing.md) | 上記「到達性ゲート」「不達は fail-closed」・是正記録 2026-09-04 |
| §8.5 | [worker-execution](../details/08-worker-execution.md) | 全経路共通・是正記録 2026-09-04 |
| §8.5 | [worker-execution](../details/08-worker-execution.md) | 2026-09-04 実測: MBP 外出中の 7 時間の断のあと 18:46 から翌朝まで沈黙 |
| §8.5 | [worker-execution](../details/08-worker-execution.md) | 是正記録 2026-09-04 |
| §8.16.1 | [slack-interface](../details/08-slack-interface.md) | 心拍 2 探針・自己終了復帰・是正記録 2026-09-04 |
| §7.1 | [infrastructure](../details/06-infrastructure.md) | 是正記録 2026-09-04 ログ規律 |
| §8.4 | [task-routing](../details/08-task-routing.md) | 理由: 2026-09-04 に縮退契約が誤ったリポジトリ名・完了条件なしの計画を生み、5 分の待ちとともに障害を隠した（縮退契約は「無い」より悪い）。 |
