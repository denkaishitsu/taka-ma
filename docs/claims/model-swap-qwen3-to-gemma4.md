# モデル交換依頼: Qwen3 32B → Gemma 4 31B

**日付**: 2026-04-08
**対象マシン**: MBP (<your-mbp>)
**対象コンポーネント**: 軽量タスク処理（ollama）

---

## 変更内容

| 項目 | 現行 | 変更後 |
|------|------|--------|
| モデル | Qwen3 32B Q4_K_M | Gemma 4 31B Q4_K_M |
| サイズ | ~20GB | 20GB |
| コンテキスト | 128K | 256K |
| ライセンス | Apache 2.0 | Apache 2.0 |

## 変更理由

Gemma 4 31B（2026-04-02リリース）が同サイズ帯で Qwen3 32B を大幅に上回るベンチマークを記録したため。

| ベンチマーク | Qwen3 32B | Gemma 4 31B | 差分 |
|-------------|-----------|-------------|------|
| AIME'25 | 72.9% | 89.2% | +16.3pt |
| LiveCodeBench v5 | 65.7% | 80.0% | +14.3pt |
| コンテキスト長 | 128K | 256K | 2倍 |
| Arena AI Elo | N/A | 1452 (オープンモデル3位) | - |

## 作業手順

MBPで以下を実行:

```bash
ollama pull gemma4:31b
# 動作確認
ollama run gemma4:31b "Hello, respond in one sentence."
# 旧モデル削除
ollama rm qwen3:32b
```

## 影響範囲

- 設計書（design-development-system.md）のモデル配置一覧を更新する必要あり
- 構築手順書03（05-task-models.md）の記載変更が必要
- o-moi の process_manager がモデル名を参照している場合、設定変更が必要

## 備考

- Gemma 4 26B A4B（MoE, 18GB, 推論高速）も候補として検討したが、31B Dense を選定
- qu-e（Qwen 2.5 Coder 7B）は現状維持（コーディング特化のため）
