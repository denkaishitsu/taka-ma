# モデル交換依頼: qu-e Qwen3-Coder-30B-A3B → Qwen3.6-35B-A3B

**日付**: 2026-06-25
**対象マシン**: MBP (<your-mbp>, 128GB)
**対象コンポーネント**: qu-e（Sentinel / Tier 2 査読・ファイル監査デーモン）
**タスク**: 台帳 #60（ya-ta は据置・qu-e のみ入替へスコープ縮小）

> 一次確定済み（HF モデルカード直接取得 / ollama library tags、2026-06-24）＋ MBP 実機実測（ollama ps、2026-06-25）。

---

## 変更内容

| 項目 | 現行 | 変更後 |
|------|------|--------|
| モデル | Qwen3-Coder-30B-A3B | Qwen3.6-35B-A3B |
| HF リポジトリ | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | `Qwen/Qwen3.6-35B-A3B` |
| アーキテクチャ | MoE 総 30.5B / active 3.3B | **MoE 総 35B / active 3B（256 experts）** |
| ollama タグ | `qwen3-coder:30b-a3b-q4_K_M` | `qwen3.6:35b-a3b-q4_K_M` |
| 重み (Q4_K_M, ollama list) | 18GB | **23GB** |
| 実常駐 (ollama ps SIZE) | 33GB @ 262144 | **27GB @ 262144** |
| KV (= 常駐 − 重み) | 14GB | **4GB** |
| コンテキスト長 | 262,144（256K）native | **262,144（256K）native**（最大 1,010,000） |
| ライセンス | Apache 2.0 | **Apache 2.0** |

> 一次ソース: HF `Qwen/Qwen3.6-35B-A3B`（35B総 / active 3B / 262,144 native / apache-2.0）、ollama library `qwen3.6` tags（`35b-a3b-q4_K_M` = 23GB / 256K）。

## 実機実測（MBP, 2026-06-25, ollama 0.30.10）

qu-e の `reviewer._generate` は `/api/generate` を **num_ctx 無指定**で呼ぶため、ollama はモデル既定 262144 でロードする。新旧とも同一経路（num_ctx 無指定）でロードし `ollama ps` の SIZE / CONTEXT 列を実測:

| モデル | 重み(disk) | 実常駐 SIZE | CONTEXT |
|--------|-----------|------------|---------|
| 旧 qwen3-coder:30b-a3b-q4_K_M | 18GB | 33GB | 262144 |
| 新 qwen3.6:35b-a3b-q4_K_M | 23GB | **27GB** | 262144 |

- 旧の実測（33GB @ 262144）は既存 `model_capacity.yaml` の記録値と一致 → 記録の妥当性を検証済み。
- **新モデルは実常駐 27GB ＜ 旧 33GB ＝ 6GB のメモリ削減**。

## 容量適合（evaluate_swap）

`python -m ai_gateway.model_monitor --role que --candidate qwen3.6:35b-a3b-q4_K_M --size-gb 27`:

```
[モデル入替提案] 役割=que（mbp）
  現行: qwen3-coder:30b-a3b-q4_K_M → 候補: qwen3.6:35b-a3b-q4_K_M（27GB）
  容量: 同居 36GB ＋ 候補 27GB ≤ 予算 116GB ? → ✅ 収まる（余裕 +53GB）
```

→ MBP（RAM 128 − reserve 12 = 予算 116GB）で light（Gemma 4 31B 実常駐 36GB）と同居して 63GB、余裕 +53GB。**容量適合 OK**。

## 変更理由

- Qwen3.6 は Qwen3.5 系の次世代 open-weight。agentic コーディング・repository-level reasoning を強化（HF カード）。同 active 3B クラスで査読品質の向上が見込める。
- 実常駐がむしろ減る（33→27GB）ため、MBP のメモリ予算をさらに緩める。
- ya-ta（DeepSeek-R1 32B）は据置。役割が推論/分解であり推論特化モデルが適合、Qwen3.6-27B はコーディング/vision 特化で用途不一致のため #60 から除外（2026-06-24 ユーザー判断）。

## 反映済みファイル

- `src/sentinel/config/qu-e.yaml`（`model:`＝SSOT）
- `src/ai_gateway/config/model_capacity.yaml`（`roles.que` 実測値）
- `src/orchestrator/config/sa-ru.yaml`（参照コメント）
- `docs/design/design-development-system.md`（§2.6 / 役割表 / 図 / §4.1 / §5 / メモリ表 / §7.4 枠表 / 目次アンカー）
- `docs/procedures/07-sentinel.md`・`00-overview.md`・`05-orchestrator.md`

## 未実施（後日・別Go）

- 本番 deploy / qu-e daemon reload（`pyinfra mbp pyinfra/deploys/sentinel.py`）。今回は構築手順をゼロから回す前提のため未実施。
- 実機検証後のモデル実体は、今回は計測目的のため旧 `qwen3-coder:30b-a3b-q4_K_M`・新 `qwen3.6:35b-a3b-q4_K_M` とも削除（ディスク解放）。次回構築時に手順書 07 Step 1 が新タグを pull する。
