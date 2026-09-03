"""Task #161 ハーネス硬化の sentinel（qu-e）側テスト。

- 一時成果物の既定除外（§8.12。`.tmp-*/` 配下・`*.tmp` — mermaid 検証の一時ファイル
  14 件が 1 件ずつ通知され承認面が溺れた 2026-08-30 実測の是正。コード側定数）
- 同一タスクの連続アラートのダイジェスト集約（§8.12。60 秒窓・1 通に件数＋パス列挙＋
  代表理由）
- 審査モデルの keep_alive 常駐（§8.8。cold ロード → 審査タイムアウト → Tier 3 濫発の
  経路除去）
"""

import asyncio
import json
import os

from file_auditor import FileAuditHandler
from reviewer import QueReviewer


def _handler(tmp, loop=None):
    config = {
        "file_audit": {
            "ignore_patterns": [],
            "log_dir": os.path.join(tmp, "logs"),
            "o_moi_alert_dir": os.path.join(tmp, "alerts"),
            "mac_mini_host": "mac-mini",
            "debounce_sec": 1,
            "alert_digest_window_sec": 60,
            "control_plane_files": [".taka-hook-settings.json"],
        },
        "task_context": {"dir": os.path.join(tmp, "task-context")},
    }
    own_loop = loop is None
    if own_loop:
        loop = asyncio.new_event_loop()
    try:
        return FileAuditHandler(config, reviewer=None, task_context_store={}, loop=loop)
    finally:
        if own_loop:
            loop.close()


# ── 一時成果物の既定除外（コード側定数・§8.12） ──

def test_tmp_artifacts_ignored_by_default(tmp_path):
    h = _handler(str(tmp_path))
    assert h._should_ignore("/repo/.tmp-mmd-check/diagram-1.png")   # `.tmp-*` dir 配下
    assert h._should_ignore("/repo/build/output.tmp")               # `*.tmp` ファイル
    assert not h._should_ignore("/repo/docs/design.md")             # 通常成果物は監査
    # 名前の一部に tmp を含むだけの成果物は巻き込まない（パターンは前方 `.tmp-` / 拡張子）
    assert not h._should_ignore("/repo/tmpl/readme.md")


# ── 連続アラートのダイジェスト集約（§8.12） ──

def _record(path, decision="escalate", reason="理由"):
    return {"path": path, "decision": decision, "reason": reason,
            "task_id": "t1", "workspace": "/repo", "command": "", "status": "in_progress",
            "confidence": 0.5, "diff_summary": ""}


def _capture_push(h):
    pushed = []

    def _push(audit_id, record, channel_id, thread_ts, team_id=""):
        pushed.append((audit_id, record))

    h._push_alert_to_o_moi = _push
    return pushed


def test_digest_flush_single_alert_passes_through(tmp_path):
    loop = asyncio.new_event_loop()
    try:
        h = _handler(str(tmp_path), loop=loop)
        pushed = _capture_push(h)

        async def _run():
            h._enqueue_digest("t1", "id-1", _record("/repo/a.md"), "C1", None, "T1")
            h._flush_digest("t1")

        loop.run_until_complete(_run())
        assert len(pushed) == 1
        assert pushed[0][0] == "id-1"
        assert pushed[0][1]["path"] == "/repo/a.md"   # 1 件は個別アラートのまま
    finally:
        loop.close()


def test_digest_flush_aggregates_multiple_alerts(tmp_path):
    """複数件は 1 通に畳む: 件数＋パス列挙＋代表理由、deny があれば decision=deny。"""
    loop = asyncio.new_event_loop()
    try:
        h = _handler(str(tmp_path), loop=loop)
        pushed = _capture_push(h)

        async def _run():
            h._enqueue_digest("t1", "id-1", _record("/repo/a.tmp2", reason="代表"),
                              "C1", None, "T1")
            h._enqueue_digest("t1", "id-2", _record("/repo/b.md", decision="deny"),
                              "C1", None, "T1")
            h._enqueue_digest("t1", "id-3", _record("/repo/c.md"), "C1", None, "T1")
            h._flush_digest("t1")

        loop.run_until_complete(_run())
        assert len(pushed) == 1                          # 3 件 → 1 通
        audit_id, digest = pushed[0]
        assert audit_id == "id-1"                        # 代表 id は先頭件（jsonl と突合可能）
        assert "3 件" in digest["reason"]
        assert "代表" in digest["reason"]
        assert digest["decision"] == "deny"              # 1 件でも deny があれば deny
        for p in ("/repo/a.tmp2", "/repo/b.md", "/repo/c.md"):
            assert p in digest["diff_summary"]           # パス列挙
        assert digest["member_audit_ids"] == ["id-1", "id-2", "id-3"]
        assert h._digest == {}                           # 台帳は掃除される
    finally:
        loop.close()


def test_digest_window_timer_set_on_first_alert(tmp_path):
    """最初のアラートで窓タイマーが張られ、同一 task_id の後続は同じ窓に積まれる。"""
    loop = asyncio.new_event_loop()
    try:
        h = _handler(str(tmp_path), loop=loop)
        _capture_push(h)

        async def _run():
            h._enqueue_digest("t1", "id-1", _record("/repo/a.md"), "C1", None, "T1")
            h._enqueue_digest("t1", "id-2", _record("/repo/b.md"), "C1", None, "T1")

        loop.run_until_complete(_run())
        assert len(h._digest["t1"]["alerts"]) == 2
        assert h._digest["t1"]["timer"] is not None
        h._digest["t1"]["timer"].cancel()
    finally:
        loop.close()


# ── 審査モデルの keep_alive 常駐（§8.8） ──

def test_generate_sends_keep_alive(tmp_path, monkeypatch):
    """/api/generate の payload に keep_alive（qu-e.yaml 由来の数値）が載る。"""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "file_audit.md").write_text("{path}")
    reviewer = QueReviewer(model="m", ollama_host="http://x", prompts_dir=str(prompts),
                           inference_lock=str(tmp_path / "lock"),
                           review_timeout_sec=5, keep_alive_sec=-1)
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"decision": "approve", "reason": "",
                                            "risk_score": 0.1})}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, timeout=None):
            captured["payload"] = json
            return _Resp()

    import reviewer as reviewer_mod
    monkeypatch.setattr(reviewer_mod.httpx, "AsyncClient", _Client)
    asyncio.run(reviewer._generate("prompt"))
    assert captured["payload"]["keep_alive"] == -1
    assert captured["payload"]["model"] == "m"
