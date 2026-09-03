"""Task #161 ハーネス硬化の orchestrator 側テスト。

- headless: stream-json 読取上限の配線（limit 引数）・行長超過の StreamReadError 化・
  読取異常時のプロセス回収（設計 §8.5。読取エラーが「難所」と誤解釈され偽昇格した
  2026-08-30 実測の是正）
- 昇格ラダー: StreamReadError は昇格の発動理由にしない（インフラ起因をモデル能力と
  混同しない・§8.5）
- 会話脳: 例文エコー検出（§8.4.1。converse.md の例文が返信に逐語出現したら棄却）
- 完了検査: file_min_bytes（生成依頼の最小達成検査・§8.10f）
"""

import asyncio
import json

import pytest

from orchestrator import Orchestrator, grounding
from orchestrator import contract as contract_rules
from orchestrator.conversation import ConversationManager, _extract_prompt_examples
from orchestrator.headless_runner import StreamReadError, WorkerHeadlessRunner


# ── headless: 読取上限と異常時の後始末（§8.5） ──

class _FakeProc:
    """run() が扱う最小のプロセス偽装。stdout 反復は factory が決める。"""

    def __init__(self, aiter_factory, returncode=0):
        self._factory = aiter_factory
        self.returncode = returncode
        self.killed = False
        self.stdout = self
        self.stderr = self

    def __aiter__(self):
        return self._factory()

    async def read(self):
        return b""

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _proc_with_lines(lines, **kwargs):
    async def _gen():
        for line in lines:
            yield line
    return _FakeProc(_gen, **kwargs)


def _proc_raising(exc):
    async def _gen():
        raise exc
        yield  # pragma: no cover — ジェネレータ化のためのダミー
    return _FakeProc(_gen)


def test_run_passes_stream_limit_to_subprocess(monkeypatch):
    """create_subprocess_exec へ limit=stream_limit_bytes が渡る（既定 64KB のままだと
    大きい tool_result 1 行で行長超過になる・§8.5）。"""
    captured = {}
    result_line = json.dumps({"type": "result", "result": "ok"}).encode() + b"\n"

    async def _fake_exec(*argv, **kwargs):
        captured.update(kwargs)
        return _proc_with_lines([result_line])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = WorkerHeadlessRunner("t1-step1-opus", stream_limit_bytes=10 * 1024 * 1024)
    result = asyncio.run(runner.run("task", timeout=5))
    assert captured["limit"] == 10 * 1024 * 1024
    assert result.text == "ok"


def test_limit_overrun_becomes_stream_read_error_and_kills_proc(monkeypatch):
    """行長超過（readline の ValueError）は StreamReadError に変換し、ssh プロセスを
    確実に畳む（読取死亡後の ssh -tt リーク・2026-08-30 実測の是正）。"""
    proc = _proc_raising(ValueError("Separator is not found, and chunk exceed the limit"))

    async def _fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = WorkerHeadlessRunner("t1-step1-opus")
    with pytest.raises(StreamReadError):
        asyncio.run(runner.run("task", timeout=5))
    assert proc.killed


def test_reader_failure_always_kills_proc(monkeypatch):
    """行長超過以外の読取異常でも worker プロセスを残さない（§8.5 資源回収）。"""
    proc = _proc_raising(OSError("read failed"))

    async def _fake_exec(*argv, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    runner = WorkerHeadlessRunner("t1-step1-opus")
    with pytest.raises(OSError):
        asyncio.run(runner.run("task", timeout=5))
    assert proc.killed


# ── 昇格ラダー: StreamReadError は昇格させない（§8.5） ──

async def _noop_notify(*a, **k):
    return None


def _orch():
    o = Orchestrator.__new__(Orchestrator)
    o._notify = _noop_notify
    o._cancelled_tasks = set()
    return o


def _item(candidates):
    loop = asyncio.get_event_loop()
    return {
        "task_id": "t1", "channel_id": None, "team_id": None, "thread_ts": None,
        "_command": "cmd", "_execution": "agent", "_depth": None, "_confidence": 0.9,
        "_model": None, "_lane": "agent", "_candidates": candidates,
        "_user_specified": False, "_step": 1,
        "_result_future": loop.create_future(),
    }


def test_agent_lane_stream_read_error_does_not_escalate():
    """StreamReadError で上位段へ昇格しない（偽昇格 haiku→sonnet→opus の是正・§8.5）。"""
    async def _run():
        o = _orch()
        ran = []

        async def _run_candidate(item, model_name, command, step, channel, team_id, thread_ts):
            ran.append(model_name)
            raise StreamReadError("stream-json 読取失敗")

        o._run_candidate = _run_candidate
        item = _item(["haiku", "sonnet", "opus"])
        await o._execute_worker_task(item)
        assert ran == ["haiku"]  # 昇格せず最初の段で打ち切り
        with pytest.raises(StreamReadError):
            item["_result_future"].result()
    asyncio.run(_run())


def test_agent_lane_plain_failure_still_escalates():
    """通常の障害は従来どおり昇格する（StreamReadError の特別扱いが縮めていないこと）。"""
    async def _run():
        o = _orch()
        ran = []

        async def _run_candidate(item, model_name, command, step, channel, team_id, thread_ts):
            ran.append(model_name)
            if model_name == "haiku":
                raise RuntimeError("難所")
            return ("ok", "done")

        o._run_candidate = _run_candidate
        item = _item(["haiku", "sonnet"])
        await o._execute_worker_task(item)
        assert ran == ["haiku", "sonnet"]
        assert item["_result_future"].result() == "done"
    asyncio.run(_run())


# ── 会話脳: 例文エコー検出（§8.4.1） ──

_TEMPLATE = """本文の「この内容で着手します」は指示された返信文。

| 発話 | 判定 | 理由 |
|------|------|------|
| 「README の冒頭にインストール手順の節を追加して」 | ready=true | 細部 |
| 「短い」 | ready=false | min_len 未満は集めない |
| 「ログイン周りをなんとかしたい」 | ready=false | 相談 |

{history}|{message}"""


def test_extract_prompt_examples_table_only():
    """例文は「判定の例」表の第 1 列だけから集める（本文の指示された返信文は含めない）。"""
    examples = _extract_prompt_examples(_TEMPLATE)
    assert "README の冒頭にインストール手順の節を追加して" in examples
    assert "ログイン周りをなんとかしたい" in examples
    assert "短い" not in examples                      # min_len 未満は除外
    assert "この内容で着手します" not in examples       # 表外（指示された返信文）は集めない


def _echo_cm(tmp_path):
    cm = ConversationManager.__new__(ConversationManager)
    cm._prompt_template = _TEMPLATE
    cm._prompt_examples = _extract_prompt_examples(_TEMPLATE)
    cm.model = "m"
    cm.ollama_host = "http://x"
    cm.timeout = 5
    cm.think = None
    return cm


def test_invoke_llm_rejects_example_echo(tmp_path, monkeypatch):
    """返信にプロンプト例文が逐語出現したら棄却する（2026-08-30 23:50 実測の是正）。"""
    import orchestrator.conversation as conv_mod
    cm = _echo_cm(tmp_path)
    monkeypatch.setattr(conv_mod, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "README の冒頭にインストール手順の節を追加して",
         "ready": False, "summary": None, "probe": None}))
    out = cm._invoke_llm([{"role": "user", "text": "残りの mermaid を直してコミットしろ"}],
                         force=False)
    assert out["error"] is True
    assert out["ready"] is False
    assert "README" not in out["reply"]  # エコーを人へ見せない


def test_invoke_llm_allows_echo_when_user_typed_it(tmp_path, monkeypatch):
    """ユーザー自身が例文と同じ文を打った場合の復唱は正当（誤棄却しない）。"""
    import orchestrator.conversation as conv_mod
    cm = _echo_cm(tmp_path)
    monkeypatch.setattr(conv_mod, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "「README の冒頭にインストール手順の節を追加して」ですね、着手します",
         "ready": True, "summary": "README 追記", "probe": None}))
    out = cm._invoke_llm([{"role": "user",
                           "text": "README の冒頭にインストール手順の節を追加して"}],
                         force=False)
    assert "error" not in out
    assert out["ready"] is True


# ── file_min_bytes（§8.10f 生成依頼の最小達成検査） ──

class _Probe:
    def __init__(self, responses):
        self.responses = responses
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        for prefix, result in self.responses:
            if prefix in command:
                return result
        return 0, ""


def test_validate_contract_accepts_file_min_bytes():
    raw = {"directive": None, "constraints": [], "rest_summary": None,
           "acceptance": [{"kind": "file_min_bytes",
                           "params": {"path": "out/video.mp4", "min_bytes": "100"}}]}
    validated, problems = contract_rules.validate_contract(raw, "動画を作って")
    assert problems == []
    # 数字文字列は int へ正規化される（diff_limit の max_lines と同じ規律）
    assert validated["acceptance"][0]["params"]["min_bytes"] == 100


def test_file_min_bytes_passes_at_or_above_threshold():
    probe = _Probe([("rev-parse --is-inside-work-tree", (1, "")),
                    ("wc -c", (0, "150"))])
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/ws", [{"kind": "file_min_bytes",
                 "params": {"path": "out.md", "min_bytes": 100}}])
    assert report.ok


def test_file_min_bytes_fails_below_threshold_and_when_missing():
    probe = _Probe([("rev-parse --is-inside-work-tree", (1, "")),
                    ("wc -c", (0, "10"))])
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/ws", [{"kind": "file_min_bytes",
                 "params": {"path": "out.md", "min_bytes": 100}}])
    assert not report.ok
    assert "acceptance_failed:file_min_bytes" in report.cause

    probe = _Probe([("rev-parse --is-inside-work-tree", (1, "")),
                    ("wc -c", (1, ""))])  # 不在（wc 失敗）
    verifier = grounding.GroundingVerifier(probe)
    report = verifier.verify_acceptance(
        "/ws", [{"kind": "file_min_bytes",
                 "params": {"path": "out.md", "min_bytes": 100}}])
    assert not report.ok
