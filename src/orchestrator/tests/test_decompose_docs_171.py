"""分解入力への対象文書抜粋の添付（設計書 §8.4「分解入力」・#171）の振る舞いテスト。

grep では潰せない振る舞いを分離実行で担保する:
  - 採取はコードの固定コマンドのみ（不正パスの棄却・probe 失敗のスキップ・上限切り詰め）
  - 1 件も採れなければ None（呼び出し側は従来どおり要約のみで分解 — 分解を止めない）
  - PlanService.build の docs 透過と、docs 無し時の従来呼び出し形の互換
  - decomposer が抜粋を別ブロックとしてプロンプトへ添える（無ければ従来と同一）
実行: リポジトリの src/ を cwd（または PYTHONPATH=src)にして pytest。
"""
import json

from orchestrator.exit_gate import collect_doc_excerpts, numbered_cat_command
from orchestrator.plan import PlanService


def _probe_factory(table: dict, calls: list | None = None):
    """command -> (rc, out) の偽 run_probe。"""
    def run_probe(command: str, timeout: int = 30):
        if calls is not None:
            calls.append(command)
        for key, result in table.items():
            if key in command:
                if isinstance(result, Exception):
                    raise result
                return result
        return (1, "")
    return run_probe


# ── numbered_cat_command（安全検証・出口ゲートと共用） ──

def test_numbered_cat_command_rejects_unsafe_paths():
    assert numbered_cat_command("/ws", "docs/a.md") == "cat -n /ws/docs/a.md"
    assert numbered_cat_command("/ws", "../etc/passwd") is None
    assert numbered_cat_command("/ws", "docs/../../x") is None
    assert numbered_cat_command("/ws", "a;rm -rf /") is None
    assert numbered_cat_command("/ws", "日本語.md") is None   # 安全文字外は採取しない


# ── collect_doc_excerpts（採取・上限・縮退） ──

def test_collects_readable_files_and_skips_failures():
    probe = _probe_factory({
        "docs/a.md": (0, "1\t# 章立て\n2\t- 指摘1"),
        "docs/none.md": (1, ""),                     # 不在（rc 非 0）
        "docs/err.md": RuntimeError("ssh down"),     # probe 例外
    })
    docs = collect_doc_excerpts(probe, "/ws",
                                ["docs/a.md", "docs/none.md", "docs/err.md",
                                 "../bad", None],
                                per_file_cap=1000, total_cap=5000)
    assert "--- docs/a.md ---" in docs and "指摘1" in docs
    assert "none.md" not in docs and "err.md" not in docs


def test_returns_none_when_nothing_collected():
    probe = _probe_factory({})
    assert collect_doc_excerpts(probe, "/ws", ["docs/x.md"], 1000, 5000) is None
    assert collect_doc_excerpts(probe, "/ws", [], 1000, 5000) is None


def test_per_file_and_total_caps_are_enforced():
    big = "1\t" + "あ" * 10000
    probe = _probe_factory({"docs/a.md": (0, big), "docs/b.md": (0, big)})
    docs = collect_doc_excerpts(probe, "/ws", ["docs/a.md", "docs/b.md"],
                                per_file_cap=500, total_cap=700)
    assert len(docs) <= 700 + 20                     # 切り詰め表示の余白のみ許容
    assert "…（以降略）" in docs


# ── PlanService.build の透過と互換 ──

class _FakeDecomposerOld:
    """context_docs 引数を持たない旧型（互換確認用）。"""
    def __init__(self):
        self.calls = []

    def decompose(self, summary, progress=None):
        self.calls.append((summary,))
        return [{"step": 1, "command": summary, "execution": "agent",
                 "depth": None, "confidence": 1.0, "depends_on": []}]


class _FakeDecomposerNew:
    def __init__(self):
        self.calls = []

    def decompose(self, summary, progress=None, context_docs=None):
        self.calls.append((summary, context_docs))
        return [{"step": 1, "command": summary, "execution": "agent",
                 "depth": None, "confidence": 1.0, "depends_on": []}]


def _service(decomposer):
    return PlanService(decomposer, corrector=None, resolve=lambda *a: None,
                       valid_models=[])


def test_build_without_docs_keeps_legacy_call_shape():
    old = _FakeDecomposerOld()
    plan = _service(old).build("要約")
    assert plan and old.calls == [("要約",)]


def test_build_passes_docs_through():
    new = _FakeDecomposerNew()
    _service(new).build("要約", docs="--- docs/a.md ---\n1\t中身")
    assert new.calls[0][1] == "--- docs/a.md ---\n1\t中身"


# ── decomposer のプロンプト組み立て ──

def test_decomposer_appends_docs_block(monkeypatch, tmp_path):
    from ai_gateway import decomposer as dz
    prompts = []

    def fake_run_ollama(model, prompt, **kw):
        prompts.append(prompt)
        return json.dumps([{"step": 1, "command": "x", "execution": "agent",
                            "depth": None, "confidence": 1.0, "depends_on": []}])
    monkeypatch.setattr(dz, "run_ollama", fake_run_ollama)
    d = dz.TaskDecomposer.__new__(dz.TaskDecomposer)
    d.model = "m"
    d.llm_timeout = 1
    d.ollama_host = None
    d.llm_think = None
    d.decisions_dir = str(tmp_path)
    d.decompose("docs/a.md を直す")
    d.decompose("docs/a.md を直す", context_docs="--- docs/a.md ---\n1\t中身")
    # ブロック見出し（コロン付き完全形）で照合する — プロンプト本文の分割規則にも
    # 「対象文書の現状（実測抜粋）」の語が含まれるため、語の部分一致では判定できない
    block_header = "対象文書の現状（実測抜粋・行番号付き）:"
    assert block_header not in prompts[0]              # 無ければ従来と同一
    assert block_header in prompts[1]
    assert "1\t中身" in prompts[1]
    assert prompts[1].index("ユーザー指示:") < prompts[1].index(block_header)
