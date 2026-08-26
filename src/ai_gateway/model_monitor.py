"""モデル自動監視・半自動入替の「容量適合判定」だけを担う不変条件コード（設計書 §7.4）。

何のためのモジュールか:
    ある役割（light / ya-ta 分解脳 / qu-e 審査）のモデルを新候補へ入れ替えたいとき、
    その候補が稼働機の RAM 予算に収まるか（容量適合）を **決定論的に検算** し、人間が承認/却下できる
    「提案」を組み立てる。最終採用は人間（半自動）。

このモジュールが担う範囲（＝コードに固定すべき不変条件）:
    - 容量設定（config/model_capacity.yaml）の読み込み
    - 候補の容量適合判定（同一稼働機の同居モデル実常駐合計 ≤ 予算 の不等式検算）
    - 承認提示用の提案データ生成

担わない範囲:
    - 候補の検証・実測・Slack 提示 → model_watch.py（§7.4 監視〜提示の無人化。本モジュールを検算に使う）
    - deploy 時の現行構成の実測と model_capacity.yaml への記入
      → docs/sa-runbooks/model-capacity-and-swap.md（ランブック A・Do→Check→Record）
    - 候補モデルの発見（キュレートしたウォッチリスト config/model_watch.yaml へ人間が登録）
    - 承認後の入替実行（ya-ta.yaml 更新 → pyinfra deploy で pull → reload。ランブック B）

設計判断（2026-06-06）:
    実測・記入・入替は「決定論だが進化する操作」のためコード固定せずランブック化した。
    コードに残すのは滅多に変わらない不変条件（容量不等式）のみ。スロップ対策はランブック側の
    verify-after-act ＋ 本コードの検算（Check）で担保する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# heavy（opus / gemini）は API 実行でローカル RAM を消費しないため、容量管理の対象外。
# 容量適合の判定対象はローカルモデルの役割（model_capacity.yaml の roles に載るもの）のみ。

DEFAULT_CAPACITY_PATH = Path(__file__).parent / "config" / "model_capacity.yaml"


@dataclass(frozen=True)
class SwapProposal:
    """1 件のモデル入替提案。人間が承認/却下を判断するための材料。

    acceptable=False（予算超過・余裕不足）の提案も「却下材料」として作られる（黙って捨てない）。
    合格条件は「物理的に収まる」ではなく「最小余裕 min_headroom_gb を残して収まる」
    （§7.4。マシンが余裕を持って作業できない入替は不合格）。
    """

    role: str               # 対象の役割（light / ya-ta / que 等）
    host: str               # その役割が動く稼働機
    current_model: str      # 現行モデル
    candidate_model: str    # 入替候補モデル
    candidate_size_gb: float # 候補の実常駐 GB（重み+KV、同 context で実測した入力）
    coexisting_gb: float    # 同一稼働機で同居する他役割モデルの合計サイズ
    budget_gb: float        # その稼働機の利用可能 RAM（ram_gb − reserve_gb）
    min_headroom_gb: float  # 予算内にさらに残すべき常用余裕（hosts.<host>.min_headroom_gb）
    fits: bool              # 候補＋同居合計が予算に物理的に収まるか
    headroom_gb: float      # 収めた後に残る余裕（負なら超過量）
    acceptable: bool        # 合格条件: fits かつ headroom_gb >= min_headroom_gb
    rationale: str          # 採用根拠（ベンチ・claims リンク等、人間が与える）


def load_capacity(path: Path = DEFAULT_CAPACITY_PATH) -> dict:
    """容量設定 YAML を読み込んで dict で返す。yaml は遅延 import（純関数を依存なしで使えるように）。"""
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def coexisting_size_gb(capacity: dict, host: str, exclude_role: str) -> float:
    """指定稼働機で同居する、exclude_role 以外の役割モデルのサイズ合計。

    入替判定では「入れ替える役割」自身の現行サイズは置き換わって消えるので合計から除く。
    """
    total = 0.0
    for role, info in capacity["roles"].items():
        if role == exclude_role or info["host"] != host:
            continue
        total += float(info["size_gb"])
    return total


def host_budget_gb(capacity: dict, host: str) -> float:
    """稼働機の利用可能 RAM（実装メモリ − OS 等の予約）。"""
    h = capacity["hosts"][host]
    return float(h["ram_gb"]) - float(h["reserve_gb"])


def host_min_headroom_gb(capacity: dict, host: str) -> float:
    """稼働機に最低限残すべき常用余裕。未設定の旧データは 0（従来の「収まれば可」と等価）。"""
    return float(capacity["hosts"][host].get("min_headroom_gb", 0))


def evaluate_swap(capacity: dict, role: str, candidate_model: str,
                  candidate_size_gb: float, rationale: str = "") -> SwapProposal:
    """役割 role を candidate_model へ入れ替えた場合の容量適合を判定し、提案を作る。

    判定式: 候補サイズ ＋ 同一稼働機の同居モデル合計 ≤ 予算 なら fits=True、
    さらに残余裕 ≥ min_headroom_gb で acceptable=True（これが提案の合格条件）。
    role が容量管理対象（roles に存在＝ローカルモデル）でない場合は KeyError を送出する。
    """
    info = capacity["roles"][role]                 # 対象外（API heavy 等）なら KeyError で弾く
    host = info["host"]
    coexisting = coexisting_size_gb(capacity, host, exclude_role=role)
    budget = host_budget_gb(capacity, host)
    min_headroom = host_min_headroom_gb(capacity, host)
    used_after = coexisting + float(candidate_size_gb)
    headroom = budget - used_after
    return SwapProposal(
        role=role,
        host=host,
        current_model=info["model"],
        candidate_model=candidate_model,
        candidate_size_gb=float(candidate_size_gb),
        coexisting_gb=coexisting,
        budget_gb=budget,
        min_headroom_gb=min_headroom,
        fits=used_after <= budget,
        headroom_gb=headroom,
        acceptable=used_after <= budget and headroom >= min_headroom,
        rationale=rationale,
    )


def format_proposal(p: SwapProposal) -> str:
    """提案を人間向けの 1 メッセージに整形する（Slack 提示・CLI 出力共通）。"""
    if p.acceptable:
        verdict = "✅ 余裕を残して収まる"
    elif p.fits:
        verdict = f"⚠️ 収まるが常用余裕 {p.min_headroom_gb:g}GB を確保できない（不合格）"
    else:
        verdict = "❌ 予算超過"
    sign = "+" if p.headroom_gb >= 0 else ""
    return (
        f"[モデル入替提案] 役割={p.role}（{p.host}）\n"
        f"  現行: {p.current_model} → 候補: {p.candidate_model}（{p.candidate_size_gb:g}GB）\n"
        f"  容量: 同居 {p.coexisting_gb:g}GB ＋ 候補 {p.candidate_size_gb:g}GB "
        f"≤ 予算 {p.budget_gb:g}GB − 最小余裕 {p.min_headroom_gb:g}GB ? "
        f"→ {verdict}（余裕 {sign}{p.headroom_gb:g}GB）\n"
        f"  根拠: {p.rationale or '（未記載）'}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: 役割・候補・実測サイズ・根拠を受け取り、容量適合の提案を出力する（不変条件の検算）。

    例: python -m ai_gateway.model_monitor --role light --candidate qwen3-coder:30b \\
            --size-gb 33 --rationale "Coding ベンチ上回り（claims/...）"
    （--size-gb は候補の実常駐 GB＝重み+KV。実測はランブック / model_watch で取得して渡す。）
    終了コード: 合格（acceptable＝最小余裕を残して収まる）なら 0、それ以外は 1
    （提案自体はどちらも出力する）。
    """
    import argparse
    parser = argparse.ArgumentParser(description="モデル入替の容量適合判定（設計書 §7.4）")
    parser.add_argument("--role", required=True, help="対象の役割（light / ya-ta / que 等）")
    parser.add_argument("--candidate", required=True, help="入替候補モデル名")
    parser.add_argument("--size-gb", type=float, required=True,
                        help="候補の実常駐 GB（重み+KV、同 context で実測した値を渡す）")
    parser.add_argument("--rationale", default="", help="採用根拠（ベンチ・claims リンク等）")
    args = parser.parse_args(argv)

    capacity = load_capacity()
    proposal = evaluate_swap(capacity, args.role, args.candidate, args.size_gb, args.rationale)
    print(format_proposal(proposal))
    return 0 if proposal.acceptable else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
