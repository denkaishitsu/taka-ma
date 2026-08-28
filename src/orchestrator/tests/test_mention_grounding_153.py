"""#153 basic-design スレッド障害の是正（sa-ru 側）の検証。

検証する振る舞い（2026-08-25 実障害の構造的再発防止）:
- awaiting_reply（§8.3 (C) 能動昇格）: 質問・確認の送信で true、着手・完了還流で false に
  遷移し、セッション永続化ファイルへ書かれる（u-zu が読んで非メンション返信を能動投入する）
- 進行状況発言のグラウンディング（§8.3）: 「作業中」等の返信は実行系タスクの実在を機械確認
  してから返す。無ければ脳の返信は使わず「タスクは走っていません」を実測で返す
- 既定完了検査の自動付与（§8.10f）: 編集系の依頼の成果物パスへ file 検査を必ず載せる
  （#149 の push 限定を成果物編集全般へ拡張）

conversation.py は test_exec_contract_149.py と同方式でファイル直ロードする。
"""

import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import contract as contract_rules  # noqa: E402

_CONV_PATH = os.path.join(_HERE, "..", "conversation.py")


def _load_conversation_module():
    spec = importlib.util.spec_from_file_location("conversation_153", _CONV_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


conversation = _load_conversation_module()
ConversationManager = conversation.ConversationManager


class _FakeNotifier:
    def __init__(self):
        self.notes = []
        self.confirms = []
        self.plan_updates = []

    def notify(self, text, channel=None, team_id=None, thread_ts=None):
        self.notes.append(text)

    def send_exec_confirm_request(self, exec_request_id, summary, channel=None,
                                  team_id=None, thread_ts=None, plan_text=None,
                                  workspace_text=None, contract_text=None):
        self.confirms.append({"summary": summary})

    def send_plan_update(self, exec_request_id, body, channel=None,
                         team_id=None, thread_ts=None):
        self.plan_updates.append({"exec_request_id": exec_request_id, "body": body})


def _manager(tmp_dir):
    sessions_dir = tempfile.mkdtemp(prefix="sessions-")
    config = {
        "sa-ru": {"model": "dummy", "ollama_host": "http://localhost:11434",
                  "converse_timeout_sec": 120},
        "exec_confirm": {"dir": tmp_dir},
        "conversation": {"sessions_dir": sessions_dir, "session_ttl_sec": 3600,
                         "history_head_turns": 4, "history_tail_turns": 16},
        "task_context": {"workspace_base": "/opt/taka-ma/work",
                         "worker_home": "/Users/dev"},
        "ya-ta": {"model": "dummy", "llm_timeout_sec": 60},
        "contract": {"intents_dir": tempfile.mkdtemp(prefix="intents-")},
    }
    return ConversationManager(config, _FakeNotifier(), task_dir=tmp_dir)


def _msg(text, cid="c1", force=False):
    return {"conversation_id": cid, "text": text, "force_ready": force,
            "user_id": "U1", "team_id": "T1", "channel_id": "C1", "thread_ts": "1.2"}


def _session_data(mgr, cid="c1"):
    with open(mgr._session_path(cid)) as f:
        return json.load(f)


def _reply(mgr, monkeypatch, reply):
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": False, "summary": None, "reply": reply})


# ── awaiting_reply の遷移（§8.3 (C) 能動昇格） ──

def test_awaiting_true_on_conversation_reply(monkeypatch):
    """会話継続の返信（ready=false）で awaiting_reply=true が永続化される。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _reply(mgr, monkeypatch, "どのファイルを直しますか？")
    mgr.handle_message(_msg("直してほしい"))
    assert _session_data(mgr)["awaiting_reply"] is True


def test_awaiting_true_on_plan_presentation_false_on_start(monkeypatch):
    """計画提示（着手/訂正待ち）で true、着手（確定タスク生成）で false になる。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": True, "summary": "要約", "reply": ""})
    monkeypatch.setattr(mgr, "_build_contract", lambda cid, summary, progress=None: {
        "directive": None, "constraints": [], "acceptance": [],
        "workspace": None, "needs_repo": False})
    mgr.handle_message(_msg("やって"))
    assert _session_data(mgr)["awaiting_reply"] is True

    record = None
    for name in os.listdir(tmp):
        if name.endswith(".json"):
            with open(os.path.join(tmp, name)) as f:
                data = json.load(f)
            if data.get("exec_request_id"):
                record = data
    mgr.create_exec_task(record)
    assert _session_data(mgr)["awaiting_reply"] is False


def test_awaiting_true_on_reject_false_on_result_reflow():
    """「やり直す」で true（続きの指示待ち）、完了還流で false になる。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    record = {"conversation_id": "c1", "channel_id": "C1", "team_id": "T1",
              "thread_ts": "1.2"}
    mgr.notify_rejected(record)
    assert _session_data(mgr)["awaiting_reply"] is True

    task = {"conversation_id": "c1"}
    mgr.append_task_result(task, "結果", "/tmp/result.json")
    assert _session_data(mgr)["awaiting_reply"] is False


def test_awaiting_restored_from_session_file(monkeypatch):
    """awaiting_reply は永続化ファイルから回復される（再起動で巻き戻らない）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _reply(mgr, monkeypatch, "続きをどうしますか？")
    mgr.handle_message(_msg("相談"))
    assert _session_data(mgr)["awaiting_reply"] is True

    # 再起動相当: 同じ sessions_dir を見る新しいマネージャで別キーの更新を行っても
    # awaiting は回復値のまま保持される
    mgr2 = ConversationManager(mgr.config, _FakeNotifier(), task_dir=mgr.task_dir)
    mgr2._append_turn("c1", "user", "追記")
    assert _session_data(mgr2)["awaiting_reply"] is True


# ── 進行状況のグラウンディング（§8.3。probe 一次経路 + LLM 選別の安全網） ──
#
# 言語理解（進行状況の質問か・進行主張か）は LLM の仕事のため、単体テストでは選別側を
# モックし「配線と機械判定（実レコード照合・差し替え/併記）」を固定する。言語カバレッジ
# 自体は実機 E2E とプロンプト（converse.md / progress_claim.md）の責務。

def _claims(mgr, monkeypatch, value):
    # handle_message の選別入口は #158 で _claims_check（progress/state の 2 判定）へ
    # 拡張された。本ファイルの検証対象は進行主張の配線のため state は常に False で固定する
    monkeypatch.setattr(mgr, "_claims_check",
                        lambda reply, progress=None: {"progress": value, "state": False})


def test_probe_task_status_answers_with_measured(monkeypatch):
    """probe="task_status" は脳の reply を使わず実測（実レコード）で答える（一次経路）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": False, "summary": None, "reply": "作業中です", "probe": "task_status"})
    mgr.handle_message(_msg("進捗どう？"))
    note = mgr.slack.notes[-1]
    assert "タスクは走っていません" in note
    assert "作業中です" not in note        # 宣言は届かない
    assert _session_data(mgr)["awaiting_reply"] is True


def test_invoke_llm_probe_whitelist(monkeypatch):
    """probe は許可値（repo_status / task_status）のみ通す。未知の値は None に落とす。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(conversation, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "", "ready": False, "summary": None, "probe": "task_status"}))
    assert mgr._invoke_llm([{"role": "user", "text": "x"}], force=False)["probe"] == "task_status"
    monkeypatch.setattr(conversation, "run_ollama", lambda *a, **k: json.dumps(
        {"reply": "", "ready": False, "summary": None, "probe": "run_command"}))
    assert mgr._invoke_llm([{"role": "user", "text": "x"}], force=False)["probe"] is None


def test_progress_claim_without_tasks_replaced_by_measured(monkeypatch):
    """安全網: 主張あり（選別=true）で実行系タスクが無い → 脳の返信を使わず実測で返す。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _reply(mgr, monkeypatch, "現在も作業中です。もう少しお待ちください。")
    _claims(mgr, monkeypatch, True)
    mgr.handle_message(_msg("進捗どう？"))
    note = mgr.slack.notes[-1]
    assert "タスクは走っていません" in note
    assert "0 件" in note
    assert "お待ちください" not in note  # 宣言は届かない
    # 履歴にも実測文言が残る（脳の虚偽主張を文脈にしない）
    turns = _session_data(mgr)["turns"]
    assert "タスクは走っていません" in turns[-1]["text"]


def test_progress_claim_with_running_task_gets_measured_footer(monkeypatch):
    """安全網: 主張ありで実行系タスクがあれば返信に実測（task_id・status）を併記する。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    with open(os.path.join(tmp, "20260825_t1.json"), "w") as f:
        json.dump({"task_id": "abcdef12-3456", "status": "in_progress",
                   "conversation_id": "c1"}, f)
    _reply(mgr, monkeypatch, "実行中です。")
    _claims(mgr, monkeypatch, True)
    mgr.handle_message(_msg("進捗どう？"))
    note = mgr.slack.notes[-1]
    assert "実行中です。" in note
    assert "実行系タスク 1 件" in note
    assert "abcdef12: in_progress" in note


def test_progress_claim_ignores_other_conversation_tasks(monkeypatch):
    """他会話のタスク・終端 status のタスクは実行系に数えない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    with open(os.path.join(tmp, "t_other.json"), "w") as f:
        json.dump({"task_id": "x", "status": "in_progress",
                   "conversation_id": "c9"}, f)
    with open(os.path.join(tmp, "t_done.json"), "w") as f:
        json.dump({"task_id": "y", "status": "completed",
                   "conversation_id": "c1"}, f)
    _reply(mgr, monkeypatch, "作業中です。")
    _claims(mgr, monkeypatch, True)
    mgr.handle_message(_msg("進捗どう？"))
    assert "タスクは走っていません" in mgr.slack.notes[-1]


def test_progress_claim_mentions_pending_plans_with_real_count(monkeypatch):
    """着手待ちの計画は実数の件数で併記し、最新の計画をボタン付きで再提示する。

    固定文言「1 件」が実態 2 件と食い違い、「着手ボタンで開始できます」と言いながら
    ボタンを出さなかった 2026-08-26 実機 E2E 検出の是正を固定する。
    """
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    for i, ts in ((1, "2026-08-25T00:00:00"), (2, "2026-08-25T12:00:00")):
        with open(os.path.join(tmp, f"confirm{i}.json"), "w") as f:
            json.dump({"exec_request_id": f"e{i}", "status": "pending",
                       "conversation_id": "c1", "created_at": ts,
                       "summary": f"S{i}", "plan": None,
                       "channel_id": "C1", "team_id": "T1"}, f)
    _reply(mgr, monkeypatch, "対応中です。")
    _claims(mgr, monkeypatch, True)
    mgr.handle_message(_msg("どうなってる？"))
    note = mgr.slack.notes[-1]
    assert "タスクは走っていません" in note
    assert "着手待ちの計画が 2 件" in note
    # 最新（created_at が新しい方）の計画がボタン付きで再提示される
    assert mgr.slack.plan_updates == [{"exec_request_id": "e2", "body": "S2"}]


def test_task_status_probe_represents_pending_plan(monkeypatch):
    """probe 経路でも、着手待ちの計画があればボタン付き再提示が付く。無ければ出ない。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(mgr, "_invoke_llm", lambda history, force, progress=None: {
        "ready": False, "summary": None, "reply": "", "probe": "task_status"})
    mgr.handle_message(_msg("進捗どう？"))
    assert mgr.slack.plan_updates == []  # pending なし → 再提示なし
    with open(os.path.join(tmp, "confirm1.json"), "w") as f:
        json.dump({"exec_request_id": "e1", "status": "pending",
                   "conversation_id": "c1", "created_at": "2026-08-25T00:00:00",
                   "summary": "S1", "plan": None}, f)
    mgr.handle_message(_msg("進捗どう？"))
    assert "着手待ちの計画が 1 件" in mgr.slack.notes[-1]
    assert mgr.slack.plan_updates == [{"exec_request_id": "e1", "body": "S1"}]


def test_progress_claim_grounded_via_llm_selector(monkeypatch):
    """実機 E2E FAIL（2026-08-25 V3）の実文言が LLM 選別経由で差し替わる配線を固定。

    1 回目の run_ollama = 会話（虚偽進行報告）、2 回目 = ready 再検査の選別
    （§8.3 細部質問の検品・質問でないため other で素通し）、3 回目 = 進行主張の選別（true）。
    語列挙の正規表現は使わない（選別は progress_claim.md への構造化 1 問）。
    """
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    outs = [json.dumps({"reply": "現在、基本設計書 `docs/02-basic-design.md` の更新"
                                 "（B1: D4とB2: D6の反映）を実施しています。"
                                 "完了次第、結果をお伝えします。",
                        "ready": False, "summary": None, "probe": None}),
            '{"verdict": "other"}',
            '{"claims_progress": true}']
    monkeypatch.setattr(conversation, "run_ollama", lambda *a, **k: outs.pop(0))
    mgr.handle_message(_msg("今の作業状況は？"))
    note = mgr.slack.notes[-1]
    assert "タスクは走っていません" in note
    assert "実施しています" not in note


def test_claims_progress_selector_parses_and_degrades(monkeypatch):
    """選別の構造化出力のパースと縮退: true/false を判定し、壊れ出力・LLM 不達は
    素通し（False）へ縮退する（選別失敗で返信を全置換する誤爆より安全側）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    monkeypatch.setattr(conversation, "run_ollama",
                        lambda *a, **k: '{"claims_progress": true}')
    assert mgr._claims_progress("The update is in progress.") is True
    monkeypatch.setattr(conversation, "run_ollama",
                        lambda *a, **k: '{"claims_progress": false}')
    assert mgr._claims_progress("どのファイルですか？") is False
    monkeypatch.setattr(conversation, "run_ollama", lambda *a, **k: "{broken")
    assert mgr._claims_progress("x") is False

    def boom(*a, **k):
        raise conversation.OllamaTimeoutError("timeout")
    monkeypatch.setattr(conversation, "run_ollama", boom)
    assert mgr._claims_progress("x") is False


def test_non_progress_reply_passes_through(monkeypatch):
    """進行主張なし（選別=false）の返信はそのまま返す（グラウンディング非介入）。"""
    tmp = tempfile.mkdtemp()
    mgr = _manager(tmp)
    _reply(mgr, monkeypatch, "対象のブランチ名を教えてください。")
    _claims(mgr, monkeypatch, False)
    mgr.handle_message(_msg("直して"))
    assert mgr.slack.notes[-1] == "対象のブランチ名を教えてください。"


# ── 既定完了検査の自動付与（§8.10f 成果物 file 既定） ──

def _contract(acceptance=None, directive=None):
    return {"directive": directive, "constraints": [],
            "acceptance": acceptance or [], "workspace": None, "needs_repo": False}


def test_default_file_check_for_deliverable_path():
    out = contract_rules.apply_default_acceptance(
        _contract(), "docs/design/basic-design.md を追記して")
    assert out["acceptance"] == [
        {"kind": "file", "params": {"path": "docs/design/basic-design.md"}}]


def test_default_file_check_composes_with_pushed():
    out = contract_rules.apply_default_acceptance(
        _contract(), "docs/a.md を更新して push もして")
    assert {"kind": "pushed", "params": {}} in out["acceptance"]
    assert {"kind": "file", "params": {"path": "docs/a.md"}} in out["acceptance"]


def test_default_file_check_not_duplicated_when_brain_covered_path():
    existing = [{"kind": "remote_file",
                 "params": {"branch": "main", "path": "docs/a.md"}}]
    out = contract_rules.apply_default_acceptance(
        _contract(acceptance=list(existing)), "docs/a.md を更新して")
    assert out["acceptance"] == existing


def test_default_file_check_skips_deletion_requests():
    """削除系の依頼には file 検査を課さない（成功したほど誤未達になる）。"""
    out = contract_rules.apply_default_acceptance(
        _contract(), "docs/a.md を削除して")
    assert out["acceptance"] == []


def test_default_file_check_ignores_versions_absolute_paths_and_urls():
    out = contract_rules.apply_default_acceptance(
        _contract(), "バージョンを 0.6.0 に更新して")
    assert out["acceptance"] == []
    out = contract_rules.apply_default_acceptance(
        _contract(), "/opt/taka-ma/docs/a.md を更新して")
    assert out["acceptance"] == []
    out = contract_rules.apply_default_acceptance(
        _contract(), "https://example.com/docs/a.md を参照して README を更新して")
    assert out["acceptance"] == []


def test_default_file_check_capped():
    """付与は上限 5 件（列挙の暴走で契約を肥大させない）。"""
    summary = "次を更新して: " + " ".join(f"docs/f{i}.md" for i in range(8))
    out = contract_rules.apply_default_acceptance(_contract(), summary)
    assert len(out["acceptance"]) == 5
    assert all(a["kind"] == "file" for a in out["acceptance"])


def test_default_file_check_reads_directive_too():
    out = contract_rules.apply_default_acceptance(
        _contract(directive="edit docs/b.md"), "任せる")
    assert out["acceptance"] == [{"kind": "file", "params": {"path": "docs/b.md"}}]
