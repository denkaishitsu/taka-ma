# モデル交換依頼: qu-e Qwen2.5-Coder-7B → Qwen3-Coder-30B-A3B

**日付**: 2026-05-30
**対象マシン**: MBP (<your-mbp>, 128GB)
**対象コンポーネント**: qu-e（Sentinel / Tier 2 査読・ファイル監査デーモン）

> 一次確定済み（ollama library / HF モデルカードで検証、2026-05-30）。ドラフト元: `commu/20260524/claims-draft_qu-e_qwen2.5-coder-7b_to_qwen3-coder-30b-a3b.md`

---

## 変更内容

| 項目 | 現行 | 変更後 |
|------|------|--------|
| モデル | Qwen 2.5 Coder 7B | Qwen3-Coder-30B-A3B-Instruct |
| HF リポジトリ | `Qwen/Qwen2.5-Coder-7B-Instruct` | `Qwen/Qwen3-Coder-30B-A3B-Instruct` |
| アーキテクチャ | dense 7B | **MoE 総 30.5B / active 3.3B** |
| ollama タグ | `qwen2.5-coder:7b` | `qwen3-coder:30b-a3b-q4_K_M`（`qwen3-coder:30b` が同実体） |
| 容量 (Q4_K_M) | ~4.7GB | **19GB** |
| コンテキスト長 | 32K | **262,144（256K）native**（YaRN で最大 1M） |
| ライセンス | Apache 2.0 | **Apache 2.0** |

> 一次ソース: ollama library `qwen3-coder` tags（30b-a3b-q4_K_M = 19GB / 256K）、HF `Qwen/Qwen3-Coder-30B-A3B-Instruct`（30.5B総 / 3.3B active / 262,144 native / apache-2.0）。

## 変更理由

- **適材適所**。qu-e は MBP M4 Max 128GB 常駐の査読デーモンであり、7B はハードに対し過小。
- Qwen3-Coder-30B-A3B はコーダー特化の新世代モデル。査読品質の大幅向上が見込める。
- **MoE（active 3.3B）** のため、常駐デーモンでも推論・コールドロードが軽く高速 ＝ Tier 2 査読の単発 JSON 生成という用途に最適。
- 旧 claims（`docs/claims/model-swap-qwen3-to-gemma4.md`）備考の「qu-e は現状維持（コーディング特化のため）」は、適材適所の運用方針に反するためユーザー判断で破棄。

## メモリ占有・ピーク制御

- MBP 上の同居ローカルモデルは **light（Gemma 4 31B ~20GB）のみ**。Claude Code / Gemini は API 経由でローカル重み 0。
- light + qu-e 同時ロード時のピーク ~39GB（空き ~89GB）。128GB なら余裕。
- **安全弁（任意）**: `OLLAMA_MAX_LOADED_MODELS=1` で LLM 常駐ピークを単一モデル ~20GB に固定可。動画生成等の重タスク時も空き 100GB を確保できる。128GB では必須でないが保険として設定。
- `OLLAMA_KEEP_ALIVE` を短め設定にすると、アイドル時にモデルがアンロードされ、重タスク中の常駐をさらに抑制できる。

> **配置先（設計判断・要レビュー）**: `OLLAMA_MAX_LOADED_MODELS` / `OLLAMA_KEEP_ALIVE` は ollama サーバ起動プロセスの環境変数。plist テンプレート（`pyinfra/templates/com.taka-ma.*.plist.j2` の `EnvironmentVariables`）か ollama 常駐サービスの起動環境に設定するのが自然。設計書未記載の新規項目のため、確定前にユーザー様の判断を仰ぐ。

## 作業手順（pull → 検証 → rm の順）

```bash
# 1. 新モデル導入
ssh mbp "ollama pull qwen3-coder:30b-a3b-q4_K_M"

# 2. 動作確認（査読用途の単発実行）
ssh mbp "ollama run qwen3-coder:30b-a3b-q4_K_M 'Review this code and respond in JSON: ...'"

# 3. 新モデル検証 OK 後に旧モデル削除（ref-counted blob を含め完全削除）
ssh mbp "ollama rm qwen2.5-coder:7b"
ssh mbp "ollama list | grep qwen2.5-coder"   # 出力が無ければ抹消完了
ssh mbp "du -sh ~/.ollama/models"            # ~4.7GB 減を確認
```

> フォールバックを先に消さないため、必ず新モデル検証後に削除する。

## 影響範囲（実ファイル grep 済）

| ファイル | 箇所 | 変更内容 |
|---------|------|---------|
| `src/sentinel/config/qu-e.yaml` | `model` | 新タグ `qwen3-coder:30b-a3b-q4_K_M` へ変更 |
| `pyinfra/deploys/sentinel.py` | `ollama pull qwen2.5-coder:7b` | 新タグへ変更（**yaml 駆動化**するため、最終的には yaml が SSOT） |
| `docs/procedures/07-sentinel.md` | モデル名・pull・サイズ・検証項目 | 新モデル・新サイズ（19GB）へ更新 |
| `docs/procedures/05-orchestrator.md` | 構成図ラベル | 図ラベル更新 |
| `src/orchestrator/config/sa-ru.yaml` | コメント | コメント更新（任意） |
| `design-development-system.md` | モデル配置一覧 | qu-e モデル記載更新 |

## 備考

- light（Gemma 4 31B）・ya-ta（DeepSeek-R1 32B）は**据置**。
- DeepSeek-V4（V4-Flash 158B Q4~88GB / V4-Pro 862B）は Mac mini 64GB に載らず、`04-ai-gateway.md` の「将来統合案: ya-ta を V4 に統合」は現ハードでは保留（API 化 or HW 増強が前提）。
- 公式 DeepSeek-R2 は存在しない（検索で出る R2 は第三者の R1 fine-tune）。
- 関連メモリ: `project_model_lineup_2026_05.md`。
</content>
</invoke>
