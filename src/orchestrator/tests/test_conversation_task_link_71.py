"""会話とタスクの継続紐づけ（設計書 §8.3 (C)・配管層）の振る舞いテスト。

検証する振る舞い（存在ではなく挙動）:
- セッション永続化ファイルは丸めず全履歴を保持し、冒頭ターンが時間経過で消えない
  （2026-08-10 インシデント F3 の再発防止の土台）
- 脳 LLM へ渡る履歴は「冒頭 + 直近」の二窓ビューに丸められ、冒頭プロンプトが
  スレッドの長さによらずプロンプトへ残る（中間は「（中略 n ターン）」と明示）
- passive 発話（チャンネルスレッドの非メンション返信）は既存セッションに限り履歴へ
  追記され、脳 LLM 呼び出し・Slack 返信は発生しない。セッションが無ければ捨てる
- 確定タスクに conversation_id / parent_task_id が永続化され、同一会話からの連続
  タスクが親子チェーンになる（sa-ru 再起動をまたいでも継承される）
- 完了結果の還流はタスクの conversation_id を最優先し、キーを持たない旧タスクは
  従来の再導出へフォールバックする
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import orchestrator.conversation as conv_mod
from orchestrator.conversation import ConversationManager


class SlackStub:
    def __init__(self):
        self.sent = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.sent.append(text)

    def send_exec_confirm_request(self, *a, **kw):
        self.sent.append("exec_confirm")


def make_manager(tmp_path, head=2, tail=4):
    config = {
        "sa-ru": {"model": "test-model", "converse_timeout_sec": 5,
                  "ollama_host": "http://localhost:11434"},
        "exec_confirm": {"dir": str(tmp_path / "confirm")},
        "conversation": {
            "session_ttl_sec": 3600,
            "sessions_dir": str(tmp_path / "sessions"),
            "history_head_turns": head,
            "history_tail_turns": tail,
        },
    }
    return ConversationManager(config, SlackStub(), task_dir=str(tmp_path / "tasks"))


CID = "T1:C1:111.222"


def msg(text, **kw):
    base = {"conversation_id": CID, "text": text,
            "channel_id": "C1", "team_id": "T1", "thread_ts": "111.222"}
    base.update(kw)
    return base


def _session_data(tmp_path):
    files = os.listdir(tmp_path / "sessions")
    assert len(files) == 1
    with open(tmp_path / "sessions" / files[0]) as f:
        return json.load(f)


def test_file_keeps_full_history_beyond_view(tmp_path, monkeypatch):
    """永続化ファイルは二窓幅を超えても丸められず、冒頭ターンが残る。"""
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: json.dumps({"reply": "了解", "ready": False}))
    m = make_manager(tmp_path, head=2, tail=4)
    for i in range(10):  # user+assistant で 20 ターン ≫ head+tail=6
        m.handle_message(msg(f"発話{i}"))
    turns = _session_data(tmp_path)["turns"]
    assert len(turns) == 20, "永続化側が丸められている（全履歴保持でない）"
    assert turns[0]["text"] == "発話0", "冒頭ターンが失われている"


def test_llm_prompt_contains_head_and_marker(tmp_path, monkeypatch):
    """脳 LLM のプロンプトに冒頭発話と中略マーカーが含まれ、中間ターンは落ちる。"""
    prompts = []

    def fake_run(model, prompt, **kw):
        prompts.append(prompt)
        return json.dumps({"reply": "了解", "ready": False})

    monkeypatch.setattr(conv_mod, "run_ollama", fake_run)
    m = make_manager(tmp_path, head=2, tail=4)
    for i in range(10):
        m.handle_message(msg(f"発話{i}"))
    # run_ollama には会話プロンプト以外（進行主張の選別・§8.3 安全網）も流れるため、
    # 会話プロンプト（判定指示を含むもの）だけを対象に取る
    last = [p for p in prompts if "毎ターン行う判定" in p][-1]
    assert "発話0" in last, "冒頭プロンプトが LLM 入力に残っていない（F3 再発）"
    assert "中略" in last, "省略が明示されていない"
    assert "発話4" not in last, "中間ターンが丸められていない"
    assert "発話9" in last, "直近ターンが LLM 入力に無い"


def test_view_within_budget_passes_through(tmp_path):
    """二窓幅以内の履歴は丸めずそのまま返す（マーカーも入らない）。"""
    m = make_manager(tmp_path, head=2, tail=4)
    history = [{"role": "user", "text": f"t{i}"} for i in range(6)]
    view = m._history_view(history)
    assert view == history


def test_passive_appends_without_llm_or_reply(tmp_path, monkeypatch):
    """passive 発話は既存セッションへ追記のみ。脳 LLM も Slack 返信も発生しない。"""
    calls = {"n": 0}

    def fake_run(*a, **kw):
        calls["n"] += 1
        return json.dumps({"reply": "了解", "ready": False})

    monkeypatch.setattr(conv_mod, "run_ollama", fake_run)
    m = make_manager(tmp_path)
    m.handle_message(msg("最初の発話"))  # セッションを作る（LLM 1 回・返信 1 件)
    llm_before, sent_before = calls["n"], len(m.slack.sent)

    m.handle_message(msg("人どうしの補足", passive=True))
    assert calls["n"] == llm_before, "passive で脳 LLM が呼ばれている"
    assert len(m.slack.sent) == sent_before, "passive に返信している"
    turns = _session_data(tmp_path)["turns"]
    assert turns[-1] == {"role": "user", "text": "人どうしの補足"}


def test_passive_without_session_is_dropped(tmp_path, monkeypatch):
    """セッションが無い会話への passive 発話は捨てる（新規セッションを作らない）。"""
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM 呼び出し禁止")))
    m = make_manager(tmp_path)
    m.handle_message(msg("無関係スレッドの発話", conversation_id="T1:C1:999.999",
                         passive=True))
    assert not os.listdir(tmp_path / "sessions"), "無関係スレッドのセッションが作られた"


def _record(summary="要約", cid=CID):
    return {"exec_request_id": "r1", "conversation_id": cid, "summary": summary,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1",
            "thread_ts": "111.222"}


def _read_tasks(tmp_path):
    tasks = []
    for name in sorted(os.listdir(tmp_path / "tasks")):
        with open(tmp_path / "tasks" / name) as f:
            tasks.append(json.load(f))
    return tasks


def test_exec_task_carries_conversation_id_and_parent_chain(tmp_path):
    """確定タスクに conversation_id が載り、同一会話の連続タスクが親子チェーンになる。"""
    m = make_manager(tmp_path)
    t1 = m.create_exec_task(_record("依頼 A"))
    t2 = m.create_exec_task(_record("依頼 B"))
    tasks = {t["task_id"]: t for t in _read_tasks(tmp_path)}
    assert tasks[t1]["conversation_id"] == CID
    assert tasks[t1]["parent_task_id"] is None, "初回タスクに親が付いている"
    assert tasks[t2]["parent_task_id"] == t1, "2 件目が直前タスクを親にしていない"


def test_parent_chain_survives_restart(tmp_path):
    """last_task_id は永続化され、再起動相当の新インスタンスでも親が継承される。"""
    m1 = make_manager(tmp_path)
    t1 = m1.create_exec_task(_record("依頼 A"))
    m2 = make_manager(tmp_path)  # 再起動相当（in-memory は空）
    t2 = m2.create_exec_task(_record("依頼 B"))
    tasks = {t["task_id"]: t for t in _read_tasks(tmp_path)}
    assert tasks[t2]["parent_task_id"] == t1, "再起動で親子チェーンが途切れた"


def test_reflow_prefers_task_conversation_id(tmp_path):
    """還流先はタスクの conversation_id を最優先する（thread からの再導出より強い）。"""
    m = make_manager(tmp_path)
    task = {"conversation_id": CID,
            "team_id": "T9", "channel_id": "C9", "thread_ts": "999.999"}
    m.append_task_result(task, "結果", "/p/x.json")
    data = _session_data(tmp_path)
    assert data["conversation_id"] == CID, "conversation_id が還流先に使われていない"


def test_reflow_falls_back_to_derivation_for_old_tasks(tmp_path):
    """conversation_id を持たない旧タスクは従来どおり thread から還流先を再導出する。"""
    m = make_manager(tmp_path)
    m.append_task_result(
        {"team_id": "T1", "channel_id": "C1", "thread_ts": "111.222"}, "結果", "/p/x.json")
    assert _session_data(tmp_path)["conversation_id"] == CID
