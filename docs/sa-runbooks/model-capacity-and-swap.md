# ランブック: モデル容量の実測記録 / モデル入替（設計書 §7.4）

**これは「ランブック」＝エージェント（または人）がそのまま順に実行・検証・記録する手順書である。**
コード化するのは不変条件（容量不等式）だけ＝[`src/ai_gateway/model_monitor.py`](../../src/ai_gateway/model_monitor.py) の `evaluate_swap`。
実測・記入・入替は「決定論だが進化する操作」のため本ランブックで実施する（コード固定しない＝拡張性を保つ）。

## 基本原則: Do → Check → Record
1. **Do**: 操作する（実測・記入・入替）。
2. **Check**: 決定論で検算する（`evaluate_swap` の予算判定／書込後の read-back 一致）。スロップ・ハルシ対策の要。
3. **Record**: 推測でなく**実測した事実**を残す（`model_capacity.yaml`／`docs/claims/`／判定ログ）。

各手順は **verify-after-act**（やった直後に必ず確認）と **rollback**（失敗時の戻し）を含む。

---

## ランブック A: 容量の実測記録（measure → record）

`model_capacity.yaml` は「設定」ではなく**容量適合判定の入力＝実測スナップショット（派生記録・手編集しない）**。
モデル選択の権威は各コンポーネント config（ya-ta/qu-e/sa-ru.yaml）。本ランブックはその記録を実機実測で更新する。

**前提**: 対象モデルがその host の ollama に pull 済み。`ollama ps` は**当該 host 上**でのみ正しく出る（MBP / Mac mini それぞれで実施）。

### Do
1. 対象 host で最小生成しモデルをロード:
   `ollama run <model> "hi" >/dev/null`（または API `/api/generate` num_predict=1）
2. 実常駐と context を取得:
   `ollama ps`  → SIZE 列（例 `26 GB`）= 実常駐、CONTEXT 列（例 `32768`）= num_ctx

### Check（検算・スロップ対策）
3. 値の妥当性: `size_gb ≒ weights_gb + kv_gb`。weights_gb は claims/一次ソースの量子化重み。乖離が大きければ測り直す。
4. 予算検算: 同居合計＋本値 ≤ host 予算 か。
   `python -m ai_gateway.model_monitor --role <役割> --candidate <model> --size-gb <実常駐> --rationale "実測"`
   → ❌ 予算超過なら num_ctx 縮小や配置見直しを検討（記入はするが要警告）。

### Record
5. `model_capacity.yaml` の `roles.<役割>` を**実測値で upsert**（無ければ追加）。記入フィールド:
   `host` / `model` / `context` / `weights_gb` / `kv_gb`(= size−weights) / `size_gb`(実常駐)
6. **verify-after-act**: 書いた直後にファイルを読み戻し、記入値と `ollama ps` が一致することを確認。
7. ロード解放: `ollama stop <model>`（command center のメモリを占有し続けない）。

### Rollback
- 記入を誤ったら直前値へ戻す（git diff で確認）。実機は再 `ollama stop`/`run` で復旧可能。

---

## ランブック B: モデル入替（swap）

**前提**: ①候補の実常駐を**ランブック A で実測済み** ②`evaluate_swap` で `fits=True`（または num_ctx 調整で収まる）③人間が Slack で承認（§8.9/§8.10）。

### Do
1. **権威の更新**: 対象コンポーネント config の model を書き換え（例 `ya-ta.yaml` の `model:`、`qu-e.yaml` の `model:`）。
   - num_ctx を絞る場合は併せて設定（焼込が要るモデルは deploy が PARAMETER num_ctx を適用、構築手順書 04 §1-2）。
2. **配備（pull＋reload）**: 該当 host へ pyinfra deploy を再実行（yaml 駆動）:
   `pyinfra <host> pyinfra/deploys/<component>.py`

### Check
3. 新モデル稼働: `ollama list | grep <new>` ／ 役割サービスが正常起動（launchctl）。
4. 実常駐が予算内: ランブック A の Do/Check を新モデルで実行し `evaluate_swap` 再判定。

### Record
5. ランブック A の Record で `model_capacity.yaml` を新モデルの実測値へ更新。
6. `docs/claims/` と判定ログに「現行→候補／実測サイズ／容量適合／根拠」を記録。

### Rollback
- 異常時は config の model を旧値に戻し deploy 再実行（旧モデルが残っていれば即復旧。消す場合は新モデル検証後）。

---

## 実装メモ（コード化の境界）
- **コード（不変条件・固定）**: `model_monitor.evaluate_swap`（容量不等式の検算）のみ。
- **ランブック（可変・本書）**: 実測・記入・入替。エージェントが実行し、Check と Record で正しさを担保。
- 将来、無人化が必要になった操作だけを個別にコードへ昇格する（big-bang 改修はしない）。
