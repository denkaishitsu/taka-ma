"""Task #127（計画プレビュー + 訂正ゲート）の振る舞いテスト。

grep では潰せない振る舞い（wave 分割が実行の依存解釈と一致するか、weight が execution×depth
から導かれるか、訂正の簡易記法・自然言語が同一パッチ出口へ集約されるか、上書きが昇格ラダーを
止めないか、承認済みプランが再分解されないか）を分離実行で担保する（設計書 §8.10b / §10.2.1）。
"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest
import yaml

from orchestrator import Orchestrator
from orchestrator.plan import (
    PlanService,
    apply_patches,
    compute_waves,
    derive_weight,
    diff_view,
    effective_deps,
    format_plan,
    parse_simple_correction,
)

_YATA = Path(__file__).resolve().parents[2] / "ai_gateway/config/ya-ta.yaml"


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(_YATA.read_text())


def _orch(config):
    """写像解決だけを使う軽量 Orchestrator（重い依存は生成しない）。"""
    o = Orchestrator.__new__(Orchestrator)
    o.config = config
    return o


def _service(config, corrector=None):
    o = _orch(config)
    return PlanService(decomposer=None, corrector=corrector,
                       resolve=o._plan_execution, valid_models=config["models"].keys())


PLAN = [
    {"step": 1, "command": "プロジェクトを解析", "execution": "agent", "depth": "deep",
     "confidence": 0.9, "depends_on": []},
    {"step": 2, "command": "変更をコミット", "execution": "agent", "depth": "shallow",
     "confidence": 0.9, "depends_on": []},
    {"step": 3, "command": "結果をまとめる", "execution": "inline", "depth": None,
     "confidence": 0.9, "depends_on": [1, 2]},
]


# ── wave 分割（実行の依存解釈と同一であること・§10.2.1 不変条件）──

def test_compute_waves_groups_independent_steps():
    """依存の無い 1・2 は同一段、両者に依存する 3 は次段。"""
    assert compute_waves(PLAN) == [[1, 2], [3]]


def test_compute_waves_ignores_dangling_dependency():
    """存在しない step への依存は実行時に無視される（_execute_subtask_in_chain）ため段も上げない。"""
    plan = [{"step": 1, "command": "a", "execution": "agent", "depends_on": [99]}]
    assert compute_waves(plan) == [[1]]


def test_effective_deps_shared_with_graph_validation():
    """dangling 除外の解釈が検証・wave・実行で 1 か所（effective_deps）に集約されている。"""
    assert effective_deps({"depends_on": [1, 99]}, {1, 2}) == [1]


def test_compute_waves_does_not_hang_on_cycle():
    """循環（実行前検証が failed に倒す不正グラフ）でもプレビューは無限ループしない。"""
    plan = [{"step": 1, "command": "a", "execution": "agent", "depends_on": [2]},
            {"step": 2, "command": "b", "execution": "agent", "depends_on": [1]}]
    assert compute_waves(plan) == [[1, 2]]


# ── weight 導出（表示専用・model から逆算しない・§10.2.1）──

@pytest.mark.parametrize("execution,depth,expected", [
    ("inline", None, "機械的"),
    ("inline", "deep", "機械的"),   # inline は depth 不問
    ("agent", "shallow", "軽"),
    ("agent", None, "中"),
    ("agent", "deep", "重"),
])
def test_derive_weight(execution, depth, expected):
    assert derive_weight(execution, depth) == expected


def test_weight_unchanged_by_model_override(config):
    """model を上書きしても weight は変わらない（weight は execution×depth からのみ導出）。"""
    svc = _service(config)
    before = {i["step"]: i for i in svc.view(PLAN)}
    updated, errors = apply_patches(PLAN, [{"steps": [2], "model": "opus"}],
                                    set(config["models"]))
    after = {i["step"]: i for i in svc.view(updated)}
    assert not errors
    assert after[2]["model"] == "opus"          # model は変わる
    assert after[2]["weight"] == before[2]["weight"] == "軽"  # weight は据え置き


# ── プレビュー本文 ──

def test_format_plan_shows_waves_weight_and_model(config):
    text = format_plan(_service(config).view(PLAN))
    assert "第1段（並行 2 件）" in text
    assert "第2段（直列）" in text
    assert "重さ: 重" in text and "opus" in text     # step1: agent/deep → 重 / opus
    assert "機械的" in text and "gemma" in text      # step3: inline → 機械的 / gemma


# ── 訂正の簡易記法（決定的パース・LLM 不要）──

def test_simple_single_step_model(config):
    models = set(config["models"])
    assert parse_simple_correction("2 opus", models) == [{"steps": [2], "model": "opus"}]


def test_simple_multi_step_model(config):
    models = set(config["models"])
    assert parse_simple_correction("2,4 sonnet", models) == [{"steps": [2, 4], "model": "sonnet"}]


def test_simple_depth_word(config):
    models = set(config["models"])
    assert parse_simple_correction("3 重い", models) == [{"steps": [3], "depth": "deep"}]


def test_simple_all_target(config):
    models = set(config["models"])
    assert parse_simple_correction("all haiku", models) == [{"steps": "all", "model": "haiku"}]


def test_simple_rejects_unknown_value(config):
    """未登録の語（モデルでも深さでもない）は簡易記法として扱わない → 自然言語経路へ。"""
    assert parse_simple_correction("2 なんとか", set(config["models"])) is None


def test_simple_all_or_nothing(config):
    """複数行のうち 1 行でも解釈できなければ全体を自然言語経路へ回す（部分適用しない）。"""
    assert parse_simple_correction("2 opus\nよろしく", set(config["models"])) is None


# ── パッチ適用（上書きは model / depth のみ）──

def test_depth_override_reresolves_model(config):
    """`3 重い` は depth=deep へ上書きし、model は写像を引き直して opus になる。"""
    plan = [{"step": 3, "command": "x", "execution": "agent", "depth": "shallow",
             "confidence": 0.9, "depends_on": []}]
    svc = _service(config)
    updated, errors = apply_patches(plan, [{"steps": [3], "depth": "deep"}], set(config["models"]))
    assert not errors
    assert svc.view(updated)[0]["model"] == "opus"


def test_unknown_step_is_reported_not_silently_dropped(config):
    _, errors = apply_patches(PLAN, [{"steps": [9], "model": "opus"}], set(config["models"]))
    assert errors and "Step 9" in errors[0]


def test_unregistered_model_is_reported(config):
    _, errors = apply_patches(PLAN, [{"steps": [1], "model": "gpt"}], set(config["models"]))
    assert errors and "gpt" in errors[0]


def test_apply_patches_does_not_mutate_input(config):
    apply_patches(PLAN, [{"steps": "all", "model": "opus"}], set(config["models"]))
    assert all("model_override" not in s for s in PLAN)


def test_diff_view_reports_only_changes(config):
    svc = _service(config)
    updated, _ = apply_patches(PLAN, [{"steps": [2], "model": "opus"}], set(config["models"]))
    lines = diff_view(svc.view(PLAN), svc.view(updated))
    assert len(lines) == 1
    assert lines[0].startswith("Step 2:") and "haiku → opus" in lines[0]


# ── 上書きは昇格ラダーを止めない（人間確認は上流フィルタ・§10.2.1）──

def test_override_keeps_escalation_ladder(config):
    """計画確認での上書き（haiku）は昇格ラダーを保つ（明示指定 `:haiku` とは扱いが違う）。"""
    o = _orch(config)
    lane, candidates, user_specified = o._plan_execution(
        "agent", "deep", 0.9, None, "haiku")
    assert candidates == ["haiku", "sonnet", "opus"]
    assert user_specified is False
    assert lane == "agent"


def test_explicit_model_still_stops_escalation(config):
    """`:モデル名` の明示指定は従来どおり昇格しない（#126 の挙動を壊さない）。"""
    o = _orch(config)
    _, candidates, user_specified = o._plan_execution("agent", "deep", 0.9, "haiku")
    assert candidates == ["haiku"]
    assert user_specified is True


def test_override_wins_over_explicit_model(config):
    """計画確認の上書きは、より古い `:モデル名` 指定より優先する（最新の意思表示）。"""
    o = _orch(config)
    _, candidates, user_specified = o._plan_execution(
        "agent", "deep", 0.9, ["gemini", "opus"], "sonnet")
    assert candidates[0] == "sonnet"
    assert user_specified is False


def test_override_disables_cross_review_at_execution(config):
    """上書きは実行時の cross-review 分岐も抑える（_model を単一へ倒す）。

    _execute_worker_task は _model が 2 モデル以上のリストなら cross-review へ分岐するため、
    上書き時に _model を残すと計画確認の上書きが実行時に無視される（提示と実行の食い違い）。
    """
    o = Orchestrator.__new__(Orchestrator)
    o.config = config
    enqueued = {}

    async def _notify(*a, **kw):
        return None

    async def _enqueue(item):
        enqueued.update(item)
        item["_result_future"].set_result("done")

    o._notify = _notify
    o._enqueue = _enqueue
    task = {"task_id": "t1", "_model": ["gemini", "opus"], "channel_id": "C1"}
    subtask = {"step": 1, "command": "x", "execution": "agent", "depth": "deep",
               "confidence": 0.9, "depends_on": [], "model_override": "sonnet"}

    async def _run():
        futures = {1: asyncio.get_event_loop().create_future()}
        await o._execute_subtask_in_chain(task, subtask, {}, futures, "C1")

    asyncio.run(_run())
    assert enqueued["_model"] == "sonnet"          # cross-review 分岐に入らない単一モデル
    assert enqueued["_candidates"] == ["sonnet", "opus"]  # 昇格ラダーは維持


# ── PlanService.correct の経路分岐 ──

class _FakeCorrector:
    """ya-ta の自然言語訂正のスタブ（返すパッチを固定する）。"""

    def __init__(self, patches):
        self.patches = patches
        self.calls = 0

    def correct(self, subtasks, text, progress=None):
        self.calls += 1
        return self.patches


def test_correct_simple_route_does_not_call_llm(config):
    corrector = _FakeCorrector([])
    svc = _service(config, corrector)
    updated, echo, route = svc.correct(PLAN, "2 opus")
    assert route == "simple"
    assert corrector.calls == 0          # 決定的パースで済む発話は LLM を呼ばない
    assert updated[1]["model_override"] == "opus"
    assert echo and "Step 2" in echo[0]


def test_correct_llm_route_returns_diff(config):
    corrector = _FakeCorrector([{"steps": [2], "model": "opus"}])
    svc = _service(config, corrector)
    updated, echo, route = svc.correct(PLAN, "コミットのやつオーパスで")
    assert route == "llm"
    assert corrector.calls == 1
    assert updated[1]["model_override"] == "opus"
    assert echo == ["Step 2: model haiku → opus"]


def test_correct_returns_none_route_for_non_correction(config):
    """訂正でない発話（空パッチ）はプランを触らず、呼び出し側で通常会話へ落とせる。"""
    svc = _service(config, _FakeCorrector([]))
    updated, echo, route = svc.correct(PLAN, "ところで昨日の件どうなった？")
    assert route is None and echo == [] and updated is PLAN


def test_correct_llm_route_with_no_effective_change_falls_through(config):
    """自然言語経路で実質変化が無ければ訂正扱いにしない（誤検知でプランを触らない）。"""
    svc = _service(config, _FakeCorrector([{"steps": [2], "model": "haiku"}]))
    _, _, route = svc.correct(PLAN, "2 はそのままで")
    assert route is None


# ── 自然言語訂正（ya-ta）の出力サニタイズ ──

def _corrector(config, monkeypatch, stdout):
    import ai_gateway.plan_corrector as pc
    cfg = dict(config)
    cfg["sa-ru"] = {"ollama_host": "http://localhost:11434"}
    monkeypatch.setattr(pc, "run_ollama", lambda *a, **kw: stdout)
    return pc.PlanCorrector(cfg)


def test_plan_corrector_parses_patches(config, monkeypatch):
    c = _corrector(config, monkeypatch, '{"patches": [{"steps": [2], "model": "opus"}]}')
    assert c.correct(PLAN, "2番はオーパスで") == [{"steps": [2], "model": "opus"}]


def test_plan_corrector_drops_malformed_patches(config, monkeypatch):
    """steps 欠落・値なしの要素は捨てる（適用側へ形の壊れたパッチを渡さない）。"""
    c = _corrector(config, monkeypatch,
                   '{"patches": [{"model": "opus"}, {"steps": [1]}, {"steps": "all", "depth": "deep"}]}')
    assert c.correct(PLAN, "x") == [{"steps": "all", "depth": "deep"}]


def test_plan_corrector_returns_empty_on_llm_failure(config, monkeypatch):
    """LLM 失敗（タイムアウト等）は空パッチに倒す＝プランを勝手に書き換えない。"""
    import ai_gateway.plan_corrector as pc
    cfg = dict(config)
    cfg["sa-ru"] = {"ollama_host": "http://localhost:11434"}

    def _boom(*a, **kw):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(pc, "run_ollama", _boom)
    assert pc.PlanCorrector(cfg).correct(PLAN, "x") == []


# ── プレビュー提示（長い計画を切り詰めず全量見せる・§10.2.1 表示形式）──

class _FakeClient:
    def __init__(self):
        self.sent = None

    def chat_postMessage(self, **kw):
        self.sent = kw


def _notifier():
    from orchestrator.slack_notifier import SlackNotifier
    n = SlackNotifier.__new__(SlackNotifier)
    client = _FakeClient()
    n._client_for = lambda team_id: client
    n._channel_for = lambda team_id, channel: "C1"
    return n, client


def test_long_plan_is_split_not_truncated():
    """1 ブロック上限を超える計画も分割して全量提示する（見えない計画を承認させない）。"""
    n, client = _notifier()
    plan_text = "x" * 6000
    n.send_exec_confirm_request("id1", "要約", plan_text=plan_text)
    sections = [b for b in client.sent["blocks"] if b["type"] == "section"]
    body = "".join(b["text"]["text"] for b in sections[1:])  # 先頭は要約
    assert body.count("x") == 6000


def test_over_limit_plan_states_omission():
    """ブロック上限すら超える極端な計画は、省略を明示して全文の在り処を示す。"""
    from orchestrator.slack_notifier import SlackNotifier
    n, client = _notifier()
    over = "y" * (SlackNotifier.PLAN_CHUNK_CHARS * (SlackNotifier.PLAN_MAX_BLOCKS + 1))
    n.send_exec_confirm_request("id2", "要約", plan_text=over)
    texts = [b["text"]["text"] for b in client.sent["blocks"] if b["type"] == "section"]
    assert any("省略しました" in t and "id2.json" in t for t in texts)


# ── 凍結プラン（dispatcher は再分解しない・§10.2）──

def test_dispatcher_uses_frozen_plan_without_redecompose(config, tmp_path):
    """_plan を持つタスクは ya-ta 分解を呼ばず、承認済みプランをそのまま実行へ渡す。"""
    o = Orchestrator.__new__(Orchestrator)
    o.config = config
    o.task_poll = 0
    frozen = [{"step": 1, "command": "承認済みの作業", "execution": "agent",
               "depth": "deep", "confidence": 0.9, "depends_on": [],
               "model_override": "sonnet"}]
    task = {"task_id": "t1", "command": "元の要約", "_plan": frozen,
            "channel_id": "C1", "team_id": "T1", "thread_ts": "1.2"}
    notices, executed = [], []

    class _Q:
        def __init__(self):
            self.served = False

        def claim(self, status, reserve_status=None):
            if self.served:
                raise asyncio.CancelledError  # 2 周目でループを終わらせる
            self.served = True
            return "/tmp/t1.json", task

    class _Decomposer:
        def decompose(self, *a, **kw):
            raise AssertionError("凍結プランがあるのに再分解された")

    async def _update_status(*a, **kw):
        return "/tmp/result.json"

    async def _notify(text, *a, **kw):
        notices.append(text)

    async def _daily_cleanup():
        return None

    async def _execute_chain(task_file, t, subtasks):
        executed.append(subtasks)

    o.task_q = _Q()
    o.decomposer = _Decomposer()
    o._update_status = _update_status
    o._notify = _notify
    o._daily_cleanup = _daily_cleanup
    o._execute_chain = _execute_chain

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await o._dispatcher()
        await asyncio.sleep(0)  # create_task した _execute_chain を回す

    asyncio.run(_run())
    assert executed == [frozen]
    assert any("承認済みの計画 1 件" in n for n in notices)


# ── 会話ゲート側の配線（凍結プランの受け渡しと訂正の適用）──

class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.updates = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None):
        self.notes.append(plan_text or "")

    def send_plan_update(self, exec_request_id, body, channel=None,
                         team_id=None, thread_ts=None):
        self.updates.append((exec_request_id, body))


def _manager(config, tmp, plan_service):
    from orchestrator.conversation import ConversationManager
    cfg = dict(config)
    cfg["sa-ru"] = {"model": "m", "ollama_host": "http://localhost:11434",
                    "converse_timeout_sec": 10}
    cfg["exec_confirm"] = {"dir": f"{tmp}/confirm", "poll_interval_sec": 1}
    cfg["conversation"] = {"dir": f"{tmp}/conv", "sessions_dir": f"{tmp}/sessions",
                           "session_ttl_sec": 60, "poll_interval_sec": 1}
    return ConversationManager(cfg, _FakeNotifier(), task_dir=f"{tmp}/tasks",
                               plan_service=plan_service)


def test_create_exec_task_freezes_plan(config):
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config))
    record = {"summary": "要約", "plan": PLAN, "user_id": "U1", "team_id": "T1",
              "channel_id": "C1", "thread_ts": "1.2"}
    mgr.create_exec_task(record)
    written = json.loads(next(Path(f"{tmp}/tasks").glob("*.json")).read_text())
    assert written["_plan"] == PLAN


def test_correction_updates_pending_record(config):
    """pending の確認レコードがある会話の発話は訂正として適用され、レコードが更新される。"""
    tmp = tempfile.mkdtemp()
    svc = _service(config, _FakeCorrector([]))
    mgr = _manager(config, tmp, svc)
    mgr._present_summary({"conversation_id": "c1", "text": "t", "user_id": "U1",
                          "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"},
                         "要約")
    # _present_summary は plan_service.build を呼ぶが decomposer=None のため plan は None。
    # 訂正対象を作るためレコードへプランを直接載せる（分解自体は別テストで担保）
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    handled = mgr._handle_correction({"conversation_id": "c1", "text": "2 opus",
                                      "channel_id": "C1", "team_id": "T1", "thread_ts": "1.2"})
    assert handled is True
    assert json.loads(path.read_text())["plan"][1]["model_override"] == "opus"


def test_correction_repost_carries_buttons(config):
    """訂正の再提示は着手ボタン付き（同一 exec_request_id）で送る（§8.10b）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config, _FakeCorrector([])))
    base = {"user_id": "U1", "team_id": "T1", "channel_id": "D1", "thread_ts": "100.1"}
    mgr._present_summary({**base, "conversation_id": "T1:D1:100.1", "text": "t"}, "要約")
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    mgr._handle_correction({**base, "conversation_id": "T1:D1:100.1", "text": "2 opus"})
    assert len(mgr.slack.updates) == 1
    rid, body = mgr.slack.updates[0]
    assert rid == record["exec_request_id"]   # ボタンは同じ確認レコードを指す
    assert "Step 2" in body


def test_plan_update_message_has_confirm_buttons():
    """send_plan_update の Block に着手/やり直すボタンが含まれる。"""
    n, client = _notifier()
    n.send_plan_update("id9", "【変更】\nStep 1: model haiku → opus")
    actions = [b for b in client.sent["blocks"] if b["type"] == "actions"]
    ids = [e["action_id"] for b in actions for e in b["elements"]]
    assert ids == ["exec_confirm", "exec_reject"]
    assert all(e["value"] == "id9" for b in actions for e in b["elements"])


def test_correction_reaches_plan_from_new_thread(config):
    """スレッド外の新規投稿（別 conversation_id）でも同じ会話面の pending 計画へ届く。

    u-zu は DM/メンションの thread_ts に「起点、無ければ自身の ts」を入れるため、新規投稿は
    毎回別 conversation_id になる（実機で確認）。conversation_id 一致だけを見ると、
    プレビューへの訂正が無言で新しい会話に化ける。
    """
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config, _FakeCorrector([])))
    base = {"user_id": "U1", "team_id": "T1", "channel_id": "D1", "thread_ts": "100.1"}
    mgr._present_summary({**base, "conversation_id": "T1:D1:100.1", "text": "t"}, "要約")
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    # 別スレッド（新規投稿）＝別 conversation_id からの訂正
    handled = mgr._handle_correction({**base, "conversation_id": "T1:D1:200.2",
                                      "thread_ts": "200.2", "text": "2 opus"})
    assert handled is True
    assert json.loads(path.read_text())["plan"][1]["model_override"] == "opus"


def test_correction_updates_reply_target(config):
    """訂正が届いた場所を確認レコードの送信先に反映し、確定タスクへ引き継ぐ。

    そうしないと、スレッド外から訂正した場合に着手後の実行通知だけが元スレッド
    （人の居ない場所）へ流れる。
    """
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config, _FakeCorrector([])))
    mgr._present_summary({"conversation_id": "T1:D1:100.1", "text": "t", "user_id": "U1",
                          "team_id": "T1", "channel_id": "D1", "thread_ts": "100.1"}, "要約")
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    mgr._handle_correction({"conversation_id": "T1:D1:200.2", "text": "2 opus",
                            "user_id": "U1", "team_id": "T1", "channel_id": "D1",
                            "thread_ts": "200.2"})
    updated = json.loads(path.read_text())
    assert updated["thread_ts"] == "200.2"

    mgr.create_exec_task(updated)
    task = json.loads(next(Path(f"{tmp}/tasks").glob("*.json")).read_text())
    assert task["thread_ts"] == "200.2"


def test_correction_does_not_cross_users_or_channels(config):
    """別ユーザー・別チャンネルの発話は他人の pending 計画へ届かない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config, _FakeCorrector([])))
    mgr._present_summary({"conversation_id": "T1:D1:100.1", "text": "t", "user_id": "U1",
                          "team_id": "T1", "channel_id": "D1", "thread_ts": "100.1"}, "要約")
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    other = {"conversation_id": "T1:D9:300.3", "text": "2 opus", "user_id": "U9",
             "team_id": "T1", "channel_id": "D9", "thread_ts": "300.3"}
    assert mgr._handle_correction(other) is False
    assert "model_override" not in json.loads(path.read_text())["plan"][1]


def test_correction_not_applied_after_decision(config):
    """既に着手（confirmed）済みなら訂正を適用しない（承認と実行の食い違いを作らない）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(config, tmp, _service(config, _FakeCorrector([])))
    mgr._present_summary({"conversation_id": "c1", "text": "t", "user_id": "U1",
                          "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"},
                         "要約")
    path = next(Path(f"{tmp}/confirm").glob("*.json"))
    record = json.loads(path.read_text())
    record["plan"] = PLAN
    path.write_text(json.dumps(record))

    # 訂正の解釈中に「着手」が押された状況を、解釈後・書込前の status 変更で再現する
    original_correct = mgr.plan_service.correct

    def _correct_then_confirm(subtasks, text, progress=None):
        result = original_correct(subtasks, text, progress=progress)
        latest = json.loads(path.read_text())
        latest["status"] = "confirmed"
        path.write_text(json.dumps(latest))
        return result

    mgr.plan_service.correct = _correct_then_confirm
    handled = mgr._handle_correction({"conversation_id": "c1", "text": "2 opus",
                                      "channel_id": "C1", "team_id": "T1", "thread_ts": "1.2"})
    assert handled is True
    assert "model_override" not in json.loads(path.read_text())["plan"][1]
    assert any("既に決着済み" in n for n in mgr.slack.notes)
