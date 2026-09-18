"""LLM 判定点の棚卸し検査（設計書 §8.4「LLM 判定点の棚卸し検査」・#175）。

「LLM が何かを判定・生成する呼び出し点」をソースから機械抽出し、既知リスト（裏取りの
有無を注記）と突合する。リスト外の判定点が増えたら fail — 裏取りのない LLM 関門が
無自覚に増える経路を、契約フィールドのスキーマ閉包と同じ型で塞ぐ（§8.10f）。

背景（2026-09-16 実測）: 意図判定は LLM 単独の関門（裏取りなし）のまま残っており、
構造化された実依頼を chat と誤判定して捨てた。この検査があれば「裏取りなし」の判定点は
事故の前に一覧で見えていた。

fail したとき: 新しい LLM 呼び出し点を意図して追加したなら、裏取り（別系統の突合・
コード導出・決定的ガード等）の有無を判断した上で本ファイルの一覧へ追記する。
一覧への追記だけで済ませず、裏取りが無いならその旨を正直に注記すること。
実行: リポジトリの src/ を cwd（または PYTHONPATH=src）にして pytest。
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2]   # src/

# 既知の LLM 呼び出し点（ファイル単位）。値は (種別, 裏取りの注記)。
# ローカル LLM = run_ollama 呼び出し。SSH 単発 = model_flag を使う worker CLI 単発生成。
KNOWN_LLM_CALLERS = {
    # ── ローカル LLM（ollama） ──
    "ai_gateway/llm.py": (
        "定義", "run_ollama の定義そのもの（呼び出し点ではない）"),
    "ai_gateway/decomposer.py": (
        "分解", "裏取りあり: 構造検証＋フォールバック（§8.4）・検証サブタスク除去（§10.2）・"
        "出口検査は契約 acceptance が担う"),
    "ai_gateway/classifier.py": (
        "分類", "裏取りあり: execution×depth は写像テーブル・昇格で受け止め（§2.2）"),
    "ai_gateway/risk_classifier.py": (
        "リスク判定", "裏取りあり: 決定論 always_deny/escalate が前段（§3.3 (0)）・"
        "Tier2 qu-e / Tier3 人間が後段"),
    "ai_gateway/intent_classifier.py": (
        "意図判定", "裏取り一部: evidence 逐語照合・低確信昇格・fail-closed=chat（§8.4）。"
        "高確信の誤判定は #175 の構造マーカーガード（chat→execute 片方向）が受け止め。"
        "自由文の高確信誤判定は判定ログ §8.4.1 の実測で監視（残余）"),
    "ai_gateway/plan_corrector.py": (
        "計画訂正", "裏取りあり: 差分エコー再確認＝人の承認面（§10.2.1）・"
        "非訂正は空パッチで通常会話へ"),
    "ai_gateway/contractor.py": (
        "契約化", "裏取りあり: validate_contract（逐語照合・出所束縛・カタログ検査）＋"
        "入口ゲート（#173 別系統突合）＋着手確認（人）"),
    "orchestrator/conversation.py": (
        "会話脳・確定要約", "裏取りあり: 状態主張ゲート（実測全置換）・確定要約は表示専用"
        "（実行系へは契約フィールドのみ §8.10f）"),
    "orchestrator/__init__.py": (
        "worker 実行・出口ゲート", "裏取りあり: 完了は機械検査（grounding）＋遵守照合＋"
        "出口ゲートのコード導出（#169）。worker 自己申告は判定に使わない"),
    # ── SSH 単発（worker CLI・model_flag 使用）で LLM を呼ぶ側 ──
    "orchestrator/exit_gate.py": (
        "出口ゲート検証", "裏取りあり: 合否はコード導出・空判定表拒否・形式的完全性検査"),
    "orchestrator/entry_gate.py": (
        "入口ゲート突合", "裏取りあり: covered はコード参照解決のみ・未検査の明示・"
        "最終裁定は人（#173）"),
}

# model_flag を含むが LLM を「判定に使う」呼び出し点ではないファイル（許可リスト）。
# worker 実行・モデル管理の配管であり、判定の関門ではない
PLUMBING_ALLOWED = {
    "orchestrator/headless_runner.py",   # worker 実行アダプタ（判定でなく作業実行）
    "orchestrator/process_manager.py",   # SSH 実行手段
    "orchestrator/pty_wrapper.py",       # interactive アダプタ
    "slack_bot/services/model_args.py",  # モデル引数の整形
    "slack_bot/services/model_store.py", # モデル登録の保存
}


def _scan(pattern: str) -> set:
    """src 配下（tests 除く）で pattern を含む .py ファイルの相対パス集合。"""
    found = set()
    rx = re.compile(pattern)
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if rx.search(text):
            found.add(rel)
    return found


def test_no_unlisted_llm_callers():
    """run_ollama 呼び出し点が既知リストの外に増えていない（増えたら意図的に棚卸しへ追記）。"""
    actual = _scan(r"run_ollama\(")
    unknown = actual - set(KNOWN_LLM_CALLERS)
    assert not unknown, (
        f"棚卸しに無い LLM 呼び出し点が増えています: {sorted(unknown)}。"
        "裏取り（別系統の突合・コード導出・決定的ガード）の有無を判断し、"
        "test_llm_inventory_175.py の KNOWN_LLM_CALLERS へ注記つきで追記してください")


def test_no_unlisted_model_flag_users():
    """model_flag（worker CLI 単発の組立）を使うファイルが棚卸し＋配管許可の外に増えていない。"""
    actual = _scan(r"model_flag")
    allowed = set(KNOWN_LLM_CALLERS) | PLUMBING_ALLOWED
    unknown = actual - allowed
    assert not unknown, (
        f"棚卸しに無い model_flag 使用ファイルが増えています: {sorted(unknown)}。"
        "LLM 判定の関門なら KNOWN_LLM_CALLERS へ、実行配管なら PLUMBING_ALLOWED へ"
        "注記つきで追記してください")


def test_inventory_entries_still_exist():
    """棚卸しの各エントリが実在ファイルを指している（リネーム・削除の追随漏れ検出）。"""
    for rel in list(KNOWN_LLM_CALLERS) + sorted(PLUMBING_ALLOWED):
        assert (SRC / rel).is_file(), f"棚卸しのエントリが実在しません: {rel}"
