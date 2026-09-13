# 詳細設計: 軽量タスク処理モデル セットアップ

> **位置づけ**: [設計書本体](../design-development-system.md) の詳細。本書が扱う節: §7。
> 障害を契機とする設計改訂の経緯は [revisions/](../revisions/) を参照。

各マシンのメモリ配分とモデル選定、モデルの自動監視・半自動入替を扱う。

---

<a id="sec-7"></a>
## 7. 軽量タスク処理モデル セットアップ

<a id="sec-7-1"></a>
## 7.1 MBPリソース配分計画（128GB unified memory）

### 通常モード（開発時）

| コンポーネント | メモリ割当 | GPU cores | 備考 |
|--------------|-----------|-----------|------|
| Gemma 4 31B (軽量タスク) | ~20GB | 共有 | Q4_K_M量子化、256Kコンテキスト |
| qu-e (Qwen3.6-35B-A3B) | ~27GB | 共有 | Q4_K_M、MoE active 3B、実常駐27GB@262144（[是正 2026-06-25](../revisions/2026-06-25.md)） |
| Claude Code ×3 | ~6GB | — | CLI軽量、推論はAPI側 |
| Gemini 連携プロセス | ~1GB | — | API呼び出しのみ |
| Docker / OS / バッファ | ~20GB | — | |
| **予備** | **~76GB** | | Blender / 将来拡張 |

### レンダリングモード（Blender使用時）

sa-ruがBlenderプロセスを検知し、自動でモード切替:

| アクション | 内容 |
|-----------|------|
| LLM一時停止 | 稼働中の ollama モデルを停止、GPU+メモリ解放 |
| Claude Code | API通信のため継続可（GPUに依存しない） |
| Blender | GPU 40コア + 最大~101GB メモリを専有可能 |
| 復帰 | Blenderプロセス終了検知 → 次回推論リクエストで ollama が自動ロード（明示的な再起動は不要） |

> **設計方針**: 共倒れを防ぐため排他制御を採用。将来的にはマシン追加でレンダリングと開発を物理分離する

> **検知失敗の記録（[是正 2026-09-04](../revisions/2026-09-04_mbp-unreachable-outage.md)）**: Blender 検知の SSH が失敗し続けるあいだ（MBP 到達不能）、`ResourceMonitor` は失敗の開始と回復の各 1 行だけを記録し、同一状態の連投（2026-09-04: 756 回×20 行超の traceback）はしない。SSH の上限は §8.5「SSH 呼び出しの上限」が掛ける

> **停止の実装（SSOT）**: LLM停止は「`ollama ps` で稼働モデルを列挙 → 各モデルを `ollama stop <model>` で停止」で行う。引数なしの `ollama stop` は MODEL 必須で何も止めない no-op になるため、必ず稼働モデル名を `ollama ps` から取得して個別に停止する。この停止ロジックは `RemoteProcessManager.stop_ollama()` を唯一の実体とし、Blender 検知による自動停止（`ResourceMonitor`）はこれへ委譲する。将来の手動停止・アイドルスリープも同一実体を共有し、停止挙動の二重実装を避ける。再起動は不要で、停止後に次の推論リクエストが来れば ollama が自動でモデルをロードする。

<a id="sec-7-1-1"></a>
## 7.1.1 将来拡張: マシン追加によるスケールアウト

現在の2台構成は、Pyinfraのinventory追加で3台以上にスケール可能:

```
現在:  Mac mini (司令塔) ──── MBP (実行 + レンダリング兼用)

将来:  Mac mini (司令塔) ─┬── MBP (実行機: LLM + Claude Code)
                          └── Mac3 (レンダリング専用)
```

Pyinfra側の変更は inventory ファイルの追加と role の割り当てのみ。
sa-ruのオーケストレーション対象にマシンを追加するだけで、アーキテクチャの変更は不要。

<a id="sec-7-2"></a>
## 7.2 モデル選定

| 項目 | 選定 | 理由 |
|------|------|------|
| モデル | **Gemma 4 31B** (Dense 31Bパラメータ) | AIME'25 89.2%、LiveCodeBench v5 80.0%。同サイズ帯で最高性能 |
| 量子化 | **Q4_K_M** (~20GB) | 軽量タスク用途に十分な品質。予備メモリ~76GB確保 |
| コンテキスト | **256K** | Qwen3 32B（128K）の2倍 |
| 推論エンジン | **ollama** | セットアップ容易、Apple Silicon最適化済み、API互換 |
| ollama タグ | `gemma4:31b` | Q4_K_Mがデフォルト |

> NOTE: 当初 Llama 4 Scout Q8_0 → Qwen3 32B Q4_K_M（2026-03-31）→ Gemma 4 31B Q4_K_M（2026-04-08）と変更。Gemma 4 31Bが同サイズ帯でQwen3 32Bを大幅に上回るベンチマークを記録したため（詳細: docs/claims/model-swap-qwen3-to-gemma4.md）

<a id="sec-7-3"></a>
## 7.3 Mac mini側（sa-ru用）

| 項目 | 選定 | 理由 |
|------|------|------|
| モデル | **Qwen3.6-35B-A3B** | sa-ruのオーケストレーション + 人間とのテキスト/画像（vision）会話用。MoE アクティブ 3B で dense 12B より生成が速く、会話の体感待ち時間を短縮 |
| 量子化 | **Q4_K_M**（約 24GB。実常駐＝重み+KV は入替 deploy 時に §7.4 ランブックで実測し `model_capacity.yaml` へ記録） | 64GBのためメモリ節約。ya-ta(qwen3.8:27b)との共存 |
| コンテキスト | **262K**（モデル上限。実効 num_ctx は容量実測とあわせて決定） | 会話履歴＋要約の投入に十分 |
| ライセンス | **Apache 2.0** | — |
| ollama タグ | `qwen3.6:35b-a3b` | Q4_K_Mがデフォルト。qu-e（MBP）と同系モデルで運用知見を共有 |
| 推論エンジン | **ollama** | MBP側と統一 |

> NOTE: Mac miniではsa-ru(Qwen3.6-35B-A3B) + ya-ta(qwen3.8:27b) が共存するため、量子化でメモリを節約する。同居実常駐合計 ≤ RAM 予算の検算は §7.4 `evaluate_swap`
> NOTE: sa-ru のモデルは Qwen3 8B（テキスト専用）→ Gemma 4 12B（マルチモーダル、2026-06-06決定）→ Qwen3.6-35B-A3B（vision 対応、2026-07-13決定）と変更。gemma4:12b は会話 1 ターン中央値 44 秒・120 秒タイムアウト常態化が実運用で判明し、速度（アクティブ 3B）と推論品質を優先した。音声・動画入力は未実装のため要件から外し、実装時にモデル要件を再評価する。クラウド Gemini を使わずローカル維持するのは主権・オフライン可用性の確保のため
> NOTE: MBP側の軽量タスクモデルは Qwen3 32B → Gemma 4 31B に変更（2026-04-08決定）
> NOTE: 旧 `gemma4:12b` 時代の実測（重み 7.6 + KV 1.1 = 8.7GB、num_ctx 40960・q8_0・ollama 0.30.10、2026-06-20）は参考値として残す。現行値の正本は §7.4 `model_capacity.yaml`（sa-ru 役割）

<a id="sec-7-4"></a>
## 7.4 モデル自動監視・半自動入替

各役割（inline / agent / ya-ta 分解脳 / qu-e 審査）について、より新しい / 適したモデル候補と
稼働機メモリ容量への適合を洗い出し、**人間の承認を経て**モデルを入れ替える仕組み。完全自動化は
しない（モデルのスペックは一次ソースで検証し AI 出力を鵜呑みにしない方針のため。CLAUDE.md）。

**対象枠とホスト容量制約**

| 枠 | 役割モデル例 | 稼働機 | 容量制約 | 入替の実体 |
|----|------------|--------|---------|----------|
| ローカル | ya-ta 分解脳（qwen3.8:27b）/ sa-ru 会話脳（Qwen3.6-35B-A3B）/ inline（Gemma）/ qu-e（Qwen3.6） | Mac mini（ya-ta・sa-ru）/ MBP（inline・qu-e） | あり（**実常駐=重み+KV** ＋同居モデル合計 ≤ ホスト RAM 予算 − **最小余裕 `min_headroom_gb`**。「入れば良い」ではなく常用余裕の確保を合格条件とする） | config 更新 ＋ モデル pull ＋ サービス reload |
| API | agent（haiku / sonnet / opus / Gemini） | MBP（API 呼出） | なし | config 更新のみ（full_name / version） |

空きメモリ量は §4.2 / `ResourceOptimizer` の値を流用する。

**フロー（半自動 = 人間ゲート）**

候補の**登録**と最終**承認**だけが人間で、間の検証〜提示は `model_watch.py` が無人で行う（コード昇格済み）。

| 段階 | 内容 | 実装主体 |
|------|------|---------|
| 監視 | トリガ起点（定期 or 手動 CLI）。**自動スクレイピングはしない**。候補は人間がキュレートしてウォッチリスト（`config/model_watch.yaml`）へ登録し、以降を無人化する | コード（`model_watch.py`） |
| 検証 | 候補の量子化サイズ・コンテキスト長・ライセンス・モダリティを**機械可読な一次ソース**（HuggingFace API / ollama レジストリ manifest）から取得・照合する。LLM を介さない（AI 出力の混入を構造的に排除） | コード（`model_watch.py`） |
| 実測 | 候補の実常駐（重み+KV）を対象役割と同じ num_ctx で稼働機上で実測（下記プロトコル） | コード（`model_watch.py`） |
| 適合判定 | ローカル枠: 実測常駐＋同居合計 ≤ RAM 予算 − 最小余裕 `min_headroom_gb`。API 枠: 容量不問（契約・可用性のみ） | コード（`evaluate_swap`） |
| 提示 | Slack へ「役割 / 現行 → 候補 / サイズ / 容量適合 / 根拠」を **Approve / Reject ボタン**付きで提示（§8.9 / §8.10 の既存承認経路を再利用）。提案 JSON も記録 | コード（`model_watch.py`） |
| 承認 | 最終採用判断（Approve / Reject） | **人間** |
| 入替 | 承認後、`ya-ta.yaml` の該当枠を更新 → モデル pull（pyinfra の yaml 駆動）→ サービス reload → `docs/claims/` と判定ログに記録。却下時は候補を `ollama rm` で撤去 | ランブック |

**判定主体**: 候補登録・最終採用判断は人間、検証・実測・適合判定・提示はシステム（半自動）。

**候補の実測プロトコル（本番メモリ保護）**

実測は稼働機に候補モデルを一時ロードするため、通常運転を圧迫しない手順をコードで固定する:

1. **誤実機ガード**: ローカル実行（SSH 未設定の host）は実行機の明示宣言（`--on-host`）と一致しない限り拒否する（別マシン役割の候補を実行機上で誤実測・誤 pull しない。fail-closed）
2. 対象役割の現行モデルを `ollama stop` で一時解放してから候補をロードする（同時常駐させない）
3. **実測ガード**: 解放後の常駐合計＋候補の予測常駐（検証済み重み × KV 上乗せ係数）が予算を超える見込みなら、ロード自体を中止する
4. 候補は対象役割と同じ num_ctx（HTTP API options）でロードし、`ollama ps` の SIZE / CONTEXT を取得したら**即 `ollama stop`**、さらに**解放完了（ps から消える）を待つ**（解放中の再ロードはメモリ二重計上となり、keep_alive=-1 の同居モデルまで追い出される — 実機観測）
5. 現行モデルを再ロード（keep_alive 復元）し、**実測前に常駐していた全モデル**を ps(before) と照合、脱落があれば元の context で再ロードして復帰を確認する。手順 4〜5 は異常時も必ず実行する（try/finally）

**容量データの維持（deploy 時の実測記録・ランブック駆動）**

容量適合判定の入力 `model_capacity.yaml` の `size_gb` は **実常駐（重み＋KV キャッシュ）** であり、推測値を入れない（CLAUDE.md）。値は実機測定で維持する。実測・記入・入替は「決定論だが進化する操作」のため**コード固定せずランブック化**（[`docs/sa-runbooks/model-capacity-and-swap.md`](../../sa-runbooks/model-capacity-and-swap.md)）し、エージェントが deploy のたびに **Do→Check→Record** で更新する。コードに固定する不変条件は**容量不等式 `evaluate_swap`**と、無人化を要するため昇格した**候補評価パイプライン `model_watch.py`**（上表の監視〜提示）。承認後の入替と deploy 時の現行構成実測はランブックのまま。

| 項目 | 内容 |
|------|------|
| トリガ | 各 deploy（`ollama pull` / `num_ctx` 焼込の後）。冪等 |
| 測定 | 当該 host で `ollama run <model>` でロード → `ollama ps` の SIZE 列（実常駐）と CONTEXT を取得 → `ollama stop` で解放 |
| 書込 | `model_capacity.yaml` の該当 role に `context` / `size_gb`（必要に応じ `kv_gb` = size − weights）を **upsert**（既存値を実測で上書き、無ければ追加） |
| 効果 | モデル変更・`num_ctx` 変更・再デプロイのたびに容量データが実機と同期。`evaluate_swap` が正しい実常駐で判定でき、Mac mini 等の OOM 見逃しを防ぐ |
| 実装方針 | **コード（不変条件・無人化済みの操作）**: `model_monitor.py` の `evaluate_swap`（同居実常駐合計 ≤ 予算 − 最小余裕 の検算）と、候補評価パイプライン `model_watch.py`（無人化のため昇格）。**ランブック（可変・操作本体）**: deploy 時の実測（host で `ollama ps`）→ `model_capacity.yaml` 記入 → `evaluate_swap` で検算（Check）→ 記録（Record）、および承認後の入替。スロップ対策は **Do→Check→Record ＋ verify-after-act**。`ollama ps` は当該 host のみのため MBP / Mac mini 各々で実施。「無人化が要る操作」だけ個別にコード昇格する方針は維持（big-bang 改修はしない） |
| 注意 | プロダクション command center（Mac mini）でのロードは一時的にメモリを占有するため、測定は deploy の単発・直後 `ollama stop` で最小化する |

---

