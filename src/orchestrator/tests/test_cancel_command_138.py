"""中止・取消命令の即時実行の振る舞いテスト（設計書 §8.10d）。

grep や AST では潰せない振る舞いを分離実行で担保する:

  - 制御判定の 2 段構成: キーワードに当たらない発話は LLM を呼ばずに None（レイテンシ不変）、
    当たった発話のみ LLM 弁別、判定不能は None へ縮退（fail-safe）
  - 停止命令が承認ゲートを迂回すること: 着手確認（send_exec_confirm_request）が呼ばれず、
    提示中プランへの訂正解釈（plan_service.correct）にも入らない
  - /taka-ma-go（force_ready）は制御判定に掛からないこと
  - 停止対象の特定と終端: 同一会話面のみ、提示中計画→cancelled+done/、init/in_progress/
    pending_approval→failed（result に中止明記）、保留承認レコードの退避、他会話面は不干渉
  - 実行台帳の cancel: 連鎖・worker の asyncio.Task が cancel されること
  - 中止済みタスクのキュー滞留分が worker 取得時にスキップされること
  - headless worker が cancel でローカル ssh を kill する（孤児化防止・§8.5 資源回収）こと

Orchestrator の __init__ はモデル/SSH/config 一式を要求するため、__new__ で本体を作らず
対象メソッドと、それが触る属性だけを差し込む（test_approval_hold_132.py と同じ流儀）。
"""
import asyncio
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import ai_gateway.classifier as clf_mod
from ai_gateway.classifier import TaskClassifier
from orchestrator import (
    Orchestrator,
    STATUS_PENDING_APPROVAL,
    _CANCELLED_BY_CONTROL,
)
from orchestrator.conversation import ConversationManager
from orchestrator.file_queue import FileQueue


# ── classify_control: 2 段判定（§8.10d 判定） ──

class _LoggerStub:
    def log_decision(self, **kw):
        pass


def _classifier(monkeypatch, llm_response):
    """LLM 応答を固定した TaskClassifier を作る（__new__ で config 要求を回避）。

    llm_response が Exception なら ollama 障害を模して送出する。呼び出し回数は
    戻り値の calls リストで観測する。
    """
    c = TaskClassifier.__new__(TaskClassifier)
    c.ai_gateway_model = "test-model"
    c.ollama_host = "http://localhost:11434"
    c.llm_timeout = 5
    c.llm_think = None
    c.logger = _LoggerStub()
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        if isinstance(llm_response, Exception):
            raise llm_response
        return llm_response

    monkeypatch.setattr(clf_mod, "run_ollama", fake_run)
    return c, calls


def test_control_no_keyword_skips_llm(monkeypatch):
    """停止語彙を含まない発話は LLM を呼ばず None（通常会話にレイテンシを足さない）。"""
    c, calls = _classifier(monkeypatch, json.dumps({"control": "cancel"}))
    assert c.classify_control("ログイン機能を実装して") is None
    assert calls == [], "キーワード非該当なのに LLM が呼ばれた"


def test_control_keyword_and_llm_cancel(monkeypatch):
    """停止語彙 + LLM が cancel と弁別 → "cancel"。"""
    c, calls = _classifier(
        monkeypatch, json.dumps({"control": "cancel", "reason": "停止命令", "confidence": 0.9}))
    assert c.classify_control("調査を中止します") == "cancel"
    assert calls == [1], "LLM 弁別が呼ばれていない"


def test_control_keyword_but_llm_none(monkeypatch):
    """停止語彙を含むだけの開発依頼は LLM が none と弁別 → None（誤検知対策）。"""
    c, _ = _classifier(
        monkeypatch, json.dumps({"control": "none", "reason": "開発依頼", "confidence": 0.9}))
    assert c.classify_control("キャンセル機能を実装して") is None


def test_control_llm_failure_falls_back_to_none(monkeypatch):
    """LLM 障害・JSON 不正・非 dict の JSON は None へ縮退（fail-safe。会話側へ落とす）。"""
    c1, _ = _classifier(monkeypatch, RuntimeError("ollama down"))
    assert c1.classify_control("中止して") is None
    c2, _ = _classifier(monkeypatch, "not a json at all")
    assert c2.classify_control("中止して") is None
    c3, _ = _classifier(monkeypatch, json.dumps(["cancel"]))  # dict でない JSON（.get が無い）
    assert c3.classify_control("中止して") is None


# ── ConversationManager: 承認ゲートを通さない即時実行（§8.10d 介入点） ──

class SlackStub:
    def __init__(self):
        self.sent = []
        self.exec_confirms = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.sent.append(text)

    def send_exec_confirm_request(self, *a, **kw):
        self.exec_confirms.append(a)


class _ControlClassifierStub:
    """classify_control の判定を固定し、呼ばれたことを記録する。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def classify_control(self, text):
        self.calls.append(text)
        return self.result

    def parse_model(self, command):
        return command, []


class _PlanServiceStub:
    """訂正解釈に入ったら失敗させる見張り（停止命令は訂正より先に捌かれる契約）。"""

    def __init__(self):
        self.correct_calls = []

    def correct(self, plan, text, progress=None):
        self.correct_calls.append(text)
        return plan, [], None


def _manager(tmp_path, *, classifier, canceller, plan_service=None):
    config = {
        "sa-ru": {"model": "test-model", "converse_timeout_sec": 5,
                  "ollama_host": "http://localhost:11434"},
        "exec_confirm": {"dir": str(tmp_path / "confirm")},
        "conversation": {"session_ttl_sec": 3600,
                         "sessions_dir": str(tmp_path / "sessions")},
    }
    return ConversationManager(config, SlackStub(), task_dir=str(tmp_path / "tasks"),
                               classifier=classifier, plan_service=plan_service,
                               canceller=canceller)


def _msg(text, **over):
    base = {"conversation_id": "T1:C1:111.222", "text": text,
            "channel_id": "C1", "team_id": "T1", "user_id": "U1",
            "thread_ts": "111.222"}
    base.update(over)
    return base


def _write_pending_confirm(confirm_dir, **over):
    """提示中（pending）の確認レコードを 1 件置き、そのパスを返す。"""
    os.makedirs(confirm_dir, exist_ok=True)
    record = {"exec_request_id": "req-1", "conversation_id": "T1:C1:111.222",
              "summary": "作業A", "plan": [{"step": 1, "command": "x"}],
              "status": "pending", "user_id": "U1", "team_id": "T1",
              "channel_id": "C1", "thread_ts": "111.222",
              "created_at": "2026-08-14T00:00:00+00:00"}
    record.update(over)
    path = os.path.join(confirm_dir, f"{record['exec_request_id']}.json")
    with open(path, "w") as f:
        json.dump(record, f, ensure_ascii=False)
    return path


def test_cancel_bypasses_confirm_gate_and_correction(tmp_path):
    """停止命令は着手確認もプラン訂正解釈も経由せず、canceller が即時実行される。

    pending の確認レコードを置いた状態（F4 再現: 提示中に「中止します」）で、
    send_exec_confirm_request が呼ばれず、plan_service.correct にも入らないこと。
    """
    plan_service = _PlanServiceStub()
    classifier = _ControlClassifierStub("cancel")
    cancel_calls = []

    def canceller(msg):
        cancel_calls.append(msg)
        return {"confirms": ["作業A"], "running": ["調査タスク"],
                "queued": [], "held": []}

    m = _manager(tmp_path, classifier=classifier, canceller=canceller,
                 plan_service=plan_service)
    _write_pending_confirm(m.confirm_dir)
    m.handle_message(_msg("調査を中止します"))

    assert cancel_calls, "canceller が呼ばれていない"
    assert m.slack.exec_confirms == [], "中止命令に着手確認ボタンが提示された（F4 再発）"
    assert plan_service.correct_calls == [], "停止命令が訂正解釈に回された"
    assert len(m.slack.sent) == 1, "報告が 1 メッセージでない"
    assert "中止しました（2 件）" in m.slack.sent[0]
    assert "作業A" in m.slack.sent[0] and "調査タスク" in m.slack.sent[0]


def test_cancel_report_appended_to_history(tmp_path):
    """発話と停止報告が会話履歴へ追記される（後続会話の文脈維持）。"""
    m = _manager(tmp_path, classifier=_ControlClassifierStub("cancel"),
                 canceller=lambda msg: {"confirms": [], "running": ["t"],
                                        "queued": [], "held": []})
    m.handle_message(_msg("中止して"))
    with m._sessions_lock:
        history = m._load_or_create_session("T1:C1:111.222")
    texts = [t["text"] for t in history]
    assert "中止して" in texts, "発話が履歴に無い"
    assert any("中止しました" in t for t in texts), "停止報告が履歴に無い"


def test_cancel_no_targets_message(tmp_path):
    """停止対象ゼロは「停止対象なし」を返す（承認ゲート形式の確認は出さない）。"""
    m = _manager(tmp_path, classifier=_ControlClassifierStub("cancel"),
                 canceller=lambda msg: {"confirms": [], "running": [],
                                        "queued": [], "held": []})
    m.handle_message(_msg("中止"))
    assert "停止対象の作業はありませんでした" in m.slack.sent[0]
    assert m.slack.exec_confirms == []


def test_cancel_failure_reports_cause(tmp_path):
    """canceller の失敗は原因を明示して返す（無言ドロップ・包括表現の禁止）。"""

    def broken(msg):
        raise RuntimeError("loop down")

    m = _manager(tmp_path, classifier=_ControlClassifierStub("cancel"), canceller=broken)
    m.handle_message(_msg("中止して"))
    assert "RuntimeError" in m.slack.sent[0] and "loop down" in m.slack.sent[0]
    # エラー文言は履歴に残さない（脳がオウム返しする・handle_message エラー経路と同じ規律）
    with m._sessions_lock:
        history = m._load_or_create_session("T1:C1:111.222")
    assert all("中止処理に失敗" not in t["text"] for t in history)


def test_force_ready_skips_control_detection(tmp_path, monkeypatch):
    """/taka-ma-go（force_ready）は制御判定に掛けない（明示の実行エスケープ）。"""
    import orchestrator.conversation as conv_mod
    monkeypatch.setattr(
        conv_mod, "run_ollama",
        lambda *a, **kw: json.dumps({"reply": "", "ready": True, "summary": "やる"}))
    classifier = _ControlClassifierStub("cancel")
    m = _manager(tmp_path, classifier=classifier,
                 canceller=lambda msg: {"confirms": [], "running": [],
                                        "queued": [], "held": []})
    m.handle_message(_msg("止まっていた作業を実行", force_ready=True))
    assert classifier.calls == [], "force_ready なのに制御判定が呼ばれた"
    assert m.slack.exec_confirms, "force_ready の着手確認が提示されていない"


# ── Orchestrator._cancel_conversation_targets: 特定・終端・台帳 cancel（§8.10d） ──

def _orch(tmp_path):
    """cancel 本体が触る属性だけを差し込んだ Orchestrator を作る。"""
    o = Orchestrator.__new__(Orchestrator)
    o.task_dir = str(tmp_path / "tasks")
    o.approval_dir = str(tmp_path / "approvals")
    o.task_q = FileQueue(o.task_dir, poll_interval=0.1)
    o.exec_confirm_q = FileQueue(str(tmp_path / "confirm"), poll_interval=0.1)
    o._running_tasks = {}
    o._cancelled_tasks = set()
    o._push_task_context = lambda task: None  # qu-e への SSH push は対象外（分離実行）
    return o


def _write_task(task_dir, task):
    os.makedirs(task_dir, exist_ok=True)
    path = os.path.join(task_dir, f"{task['task_id']}.json")
    with open(path, "w") as f:
        json.dump(task, f, ensure_ascii=False)
    return path


def _face(task_id, status, *, team="T1", channel="C1", command="やること"):
    return {"task_id": task_id, "status": status, "command": command,
            "team_id": team, "channel_id": channel, "thread_ts": "111.222"}


def _cancel_msg():
    return {"conversation_id": "T1:C1:111.222", "text": "中止します",
            "team_id": "T1", "channel_id": "C1", "user_id": "U1",
            "thread_ts": "111.222"}


def test_cancel_targets_all_states_same_face_only(tmp_path):
    """同一会話面の 4 区分を全て停止し、他会話面には触れない。終端は failed + 中止明記。"""
    o = _orch(tmp_path)
    _write_pending_confirm(str(tmp_path / "confirm"))
    _write_pending_confirm(str(tmp_path / "confirm"),
                           exec_request_id="req-other", channel_id="C9")
    p_init = _write_task(o.task_dir, _face("t-init", "init"))
    p_run = _write_task(o.task_dir, _face("t-run", "in_progress"))
    p_held = _write_task(o.task_dir, {**_face("t-held", STATUS_PENDING_APPROVAL),
                                      "held_approval_id": "req-a"})
    p_other = _write_task(o.task_dir, _face("t-other", "init", channel="C9"))
    os.makedirs(o.approval_dir, exist_ok=True)
    with open(os.path.join(o.approval_dir, "req-a.json"), "w") as f:
        json.dump({"request_id": "req-a", "task_id": "t-held",
                   "status": "pending",
                   "held_at": datetime.datetime.now().isoformat()}, f)

    report = asyncio.run(o._cancel_conversation_targets(_cancel_msg()))

    assert len(report["confirms"]) == 1 and len(report["queued"]) == 1
    assert len(report["running"]) == 1 and len(report["held"]) == 1

    # 同一面の pending 確認レコードは done/ へ、他会話面のものは pending のまま残る
    done = os.listdir(tmp_path / "confirm" / "done")
    assert done == ["req-1.json"]
    assert os.path.exists(tmp_path / "confirm" / "req-other.json")

    # タスクは failed で終端しアーカイブされ、result に中止命令による停止が刻まれる
    for orig in (p_init, p_run, p_held):
        assert not os.path.exists(orig), f"{orig} が終端されていない"
    today = datetime.date.today().isoformat()
    archived = {name: json.load(open(os.path.join(o.task_dir, "done", today, name)))
                for name in os.listdir(os.path.join(o.task_dir, "done", today))}
    assert len(archived) == 3
    for task in archived.values():
        assert task["status"] == "failed"
        assert task["result"] == _CANCELLED_BY_CONTROL
    # 他会話面のタスクは init のまま
    assert json.load(open(p_other))["status"] == "init"

    # 保留承認レコードは done/ へ退避（孤児化防止）
    assert os.path.exists(os.path.join(o.approval_dir, "done", "req-a.json"))
    # 中止済み集合に登録され、キュー滞留分のスキップ根拠になる
    assert {"t-init", "t-run", "t-held"} <= o._cancelled_tasks


def test_cancel_face_match_normalizes_none_and_empty(tmp_path):
    """会話面照合は None と ""（レコード側の既定値）を同一視する（取りこぼし防止）。"""
    o = _orch(tmp_path)
    _write_task(o.task_dir, _face("t-x", "init", team=""))
    msg = {**_cancel_msg(), "team_id": None}
    report = asyncio.run(o._cancel_conversation_targets(msg))
    assert len(report["queued"]) == 1, "None と '' の会話面不一致で停止対象を取りこぼした"


def test_cancel_cancels_running_chain_and_workers(tmp_path):
    """実行台帳に登録された連鎖・worker の asyncio.Task が cancel される。"""
    o = _orch(tmp_path)
    _write_task(o.task_dir, _face("t-run", "in_progress"))

    async def scenario():
        chain = asyncio.create_task(asyncio.sleep(3600))
        worker = asyncio.create_task(asyncio.sleep(3600))
        o._running_tasks["t-run"] = {"chain": chain, "workers": {worker}}
        await o._cancel_conversation_targets(_cancel_msg())
        await asyncio.sleep(0)  # cancel の伝播を 1 周回す
        return chain.cancelled(), worker.cancelled()

    chain_cancelled, worker_cancelled = asyncio.run(scenario())
    assert chain_cancelled, "連鎖タスクが cancel されていない"
    assert worker_cancelled, "worker タスクが cancel されていない"
    assert "t-run" not in o._running_tasks, "実行台帳から削除されていない"


def test_worker_skips_cancelled_task_item(tmp_path):
    """中止済み task_id のキュー滞留サブタスクは worker 取得時に実行されずスキップされる。"""
    o = _orch(tmp_path)
    o._cancelled_tasks.add("t-dead")
    executed = []
    o._execute_cross_review = lambda *a, **kw: executed.append("cross")

    async def scenario():
        fut = asyncio.get_event_loop().create_future()
        item = {"task_id": "t-dead", "_command": "x", "_step": 1,
                "_result_future": fut, "_model": ["a", "b"]}
        await o._execute_worker_task(item)
        return fut

    fut = asyncio.run(scenario())
    assert fut.cancelled(), "future が解決されていない（連鎖側の待ちが残る）"
    assert executed == [], "中止済みタスクのサブタスクが実行された"


# ── headless: cancel でローカル ssh を kill（§8.5 資源回収・孤児化防止） ──

def test_headless_run_kills_proc_on_cancel(monkeypatch):
    from orchestrator.headless_runner import WorkerHeadlessRunner

    class _HangingStdout:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(3600)  # 出力が来ない worker を模す

    class _Proc:
        def __init__(self):
            self.killed = False
            self.stdout = _HangingStdout()

        def kill(self):
            self.killed = True

        async def wait(self):
            return 0

    proc = _Proc()

    async def fake_exec(*a, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    async def scenario():
        runner = WorkerHeadlessRunner("t1-step1-fable")
        task = asyncio.create_task(runner.run("do something", timeout=3600))
        await asyncio.sleep(0.01)  # run が proc 起動・stream 待ちに入るまで進める
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task.cancelled()

    assert asyncio.run(scenario()), "cancel が伝播していない"
    assert proc.killed, "cancel 時にローカル ssh（proc）が kill されていない"
