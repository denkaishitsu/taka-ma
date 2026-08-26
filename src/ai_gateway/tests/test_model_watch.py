"""model_watch / model_monitor の分離実行テスト（設計書 §7.4 の無人パイプライン）。

検証する振る舞い（grep では担保できない分岐・保護動作）:
  - 容量適合の合格条件が「予算に収まる」ではなく「最小余裕 min_headroom_gb を残して収まる」
    であること（余裕不足は fits=True でも不合格）
  - `ollama ps` 出力の解析（SIZE/CONTEXT の抽出・ヘッダ行の無視・MB 単位）
  - 実測プロトコルの本番メモリ保護:
      現行モデルを解放してから候補をロードする（同時常駐させない）／
      実測ガード（予測常駐が収まらない見込みならロードしない）／
      異常時も候補の解放と現行モデルの復帰を必ず行う（try/finally）
  - 提案の出力契約: 承認ファイル（status=pending・§8.10 形式）と Block Kit の
    action_id（approve_action / reject_action = u-zu handlers/actions.py との契約）
"""

import json

import pytest

from ai_gateway import model_watch
from ai_gateway.model_monitor import evaluate_swap, format_proposal
from ai_gateway.model_watch import (
    Measurement,
    VerifiedSpec,
    build_slack_blocks,
    measure_candidate,
    parse_ollama_ps,
    propose,
)


def _capacity(min_headroom=8):
    return {
        "hosts": {"mac-mini": {"ram_gb": 64, "reserve_gb": 8, "min_headroom_gb": min_headroom}},
        "roles": {
            "ya-ta": {"host": "mac-mini", "model": "cur:27b", "context": 32768, "size_gb": 17},
            "sa-ru": {"host": "mac-mini", "model": "coexist:35b", "context": 32768, "size_gb": 23},
        },
    }


# ---- 容量適合（最小余裕が合格条件） ----

def test_evaluate_swap_acceptable_with_headroom():
    # 予算 56、同居 23、候補 20 → 残 13 ≥ 最小余裕 8 → 合格
    p = evaluate_swap(_capacity(), "ya-ta", "cand:27b", 20)
    assert p.fits and p.acceptable
    assert p.headroom_gb == pytest.approx(13)


def test_evaluate_swap_fits_but_insufficient_headroom_is_rejected():
    # 予算 56、同居 23、候補 30 → 残 3 < 最小余裕 8 → 物理的には収まるが不合格
    p = evaluate_swap(_capacity(), "ya-ta", "cand:big", 30)
    assert p.fits and not p.acceptable
    assert "余裕" in format_proposal(p)


def test_evaluate_swap_over_budget():
    p = evaluate_swap(_capacity(), "ya-ta", "cand:huge", 40)
    assert not p.fits and not p.acceptable


def test_evaluate_swap_min_headroom_defaults_to_zero():
    # min_headroom_gb 未設定の旧データでは従来の「収まれば可」と等価
    cap = _capacity()
    del cap["hosts"]["mac-mini"]["min_headroom_gb"]
    p = evaluate_swap(cap, "ya-ta", "cand:big", 30)
    assert p.fits and p.acceptable


# ---- ollama ps 解析 ----

def test_parse_ollama_ps():
    out = (
        "NAME                    ID            SIZE      PROCESSOR    CONTEXT    UNTIL\n"
        "qwen3.6:27b-q4_K_M      abcdef123456  17 GB     100% GPU     32768      4 minutes from now\n"
        "tiny:1b                 fedcba654321  800 MB    100% GPU     4096       forever\n"
    )
    ps = parse_ollama_ps(out)
    assert ps["qwen3.6:27b-q4_K_M"] == {"size_gb": 17.0, "context": 32768}
    assert ps["tiny:1b"]["size_gb"] == pytest.approx(0.8)
    assert ps["tiny:1b"]["context"] == 4096


def test_parse_ollama_ps_empty():
    assert parse_ollama_ps("NAME  ID  SIZE\n") == {}


# ---- 実測プロトコル（FakeRunner で分離実行） ----

class FakeRunner:
    """HostRunner の代替。実行されたコマンド列を記録し、台本どおりの出力を返す。

    ps 台本が尽きたら最後の出力を返し続ける（解放待ち・復帰確認など ps 回数が増えても
    「最終状態が安定している」実機と同じ振る舞いになる）。
    """

    def __init__(self, ps_script):
        self.ollama = "ollama"
        self.cmds = []
        self._ps = list(ps_script)
        self._i = 0

    def run(self, cmd, timeout):
        self.cmds.append(cmd)
        if cmd.endswith("ollama ps"):
            out = self._ps[min(self._i, len(self._ps) - 1)]
            self._i += 1
            return 0, out
        return 0, "{}"


_PS_CUR = "NAME ID SIZE PROCESSOR CONTEXT UNTIL\ncur:27b x 17 GB 100%GPU 32768 soon\ncoexist:35b y 23 GB 100%GPU 32768 soon\n"
_PS_FREED = "NAME ID SIZE PROCESSOR CONTEXT UNTIL\ncoexist:35b y 23 GB 100%GPU 32768 soon\n"
_PS_LOADED = "NAME ID SIZE PROCESSOR CONTEXT UNTIL\ncoexist:35b y 23 GB 100%GPU 32768 soon\ncand:27b z 19 GB 100%GPU 32768 soon\n"
_PS_CUR_ONLY = "NAME ID SIZE PROCESSOR CONTEXT UNTIL\ncur:27b x 17 GB 100%GPU 32768 soon\n"
_PS_RESTORED = _PS_CUR


def _measure(monkeypatch, runner, weights_gb=18, on_host="mac-mini"):
    monkeypatch.setattr(model_watch, "HostRunner", lambda ssh, ollama_bin: runner)
    cand = {"model": "cand:27b", "role": "ya-ta", "_verified_weights_gb": weights_gb}
    watch = {"hosts": {"mac-mini": {"ssh": None, "ollama_bin": "ollama"}}, "kv_margin": 1.3}
    return measure_candidate(cand, _capacity(), watch, on_host=on_host)


def test_measure_happy_path_stops_current_before_load_and_restores(monkeypatch):
    # ps 台本: before → guard → loaded → 解放待ち → 復帰確認（以降は最終状態で安定）
    runner = FakeRunner([_PS_CUR, _PS_FREED, _PS_LOADED, _PS_FREED, _PS_RESTORED])
    meas = _measure(monkeypatch, runner)
    assert meas.size_gb == 19.0 and meas.context == 32768 and meas.restored
    joined = "\n".join(runner.cmds)
    # 現行の解放が候補ロードより先（同時常駐させない）
    assert joined.index("stop cur:27b") < joined.index('"model": "cand:27b"')
    # 候補の解放と現行の復帰が行われる
    assert "stop cand:27b" in joined
    assert '"keep_alive": -1' in joined  # 現行の復帰は本番同等の常駐指定


def test_measure_reloads_evicted_coresident(monkeypatch):
    # 復帰確認 ps で同居モデル（coexist:35b）が脱落 → 元の context 32768 で再ロードし復帰確認
    # （ollama は新モデルロード時に keep_alive=-1 常駐も追い出すことがある — 2026-08-26 実機観測）
    runner = FakeRunner([_PS_CUR, _PS_FREED, _PS_LOADED, _PS_FREED,
                         _PS_CUR_ONLY, _PS_CUR_ONLY, _PS_RESTORED])
    meas = _measure(monkeypatch, runner)
    assert meas.restored
    reload_cmd = next(c for c in runner.cmds if '"model": "coexist:35b"' in c)
    assert '"num_ctx": 32768' in reload_cmd and '"keep_alive": -1' in reload_cmd


def test_measure_guard_aborts_before_load(monkeypatch):
    # 予測常駐 40×1.3=52 + 常駐 23 > 予算 56 → pull/load せず中止、現行は復帰される
    runner = FakeRunner([_PS_CUR, _PS_FREED, _PS_RESTORED])
    with pytest.raises(RuntimeError, match="実測ガード"):
        _measure(monkeypatch, runner, weights_gb=40)
    joined = "\n".join(runner.cmds)
    assert "pull" not in joined and '"model": "cand:27b"' not in joined
    assert '"model": "cur:27b"' in joined  # 復帰は実施


def test_measure_without_verified_weights_aborts(monkeypatch):
    runner = FakeRunner([])
    with pytest.raises(RuntimeError, match="実測ガード"):
        _measure(monkeypatch, runner, weights_gb=None)
    assert runner.cmds == []  # 実機に一切触れない


def test_measure_refuses_local_without_host_declaration(monkeypatch):
    # ssh 未設定の host は --on-host（実行機の明示宣言）一致が無い限りローカル実行を拒否
    # （mac-mini 上の定期実行が MBP 役割の候補を mac-mini 上で誤実測・誤 pull する事故の防止）
    runner = FakeRunner([])
    with pytest.raises(RuntimeError, match="ホスト同一性ガード"):
        _measure(monkeypatch, runner, on_host=None)
    with pytest.raises(RuntimeError, match="ホスト同一性ガード"):
        _measure(monkeypatch, runner, on_host="mbp")
    assert runner.cmds == []  # 実機に一切触れない


def test_cleanup_guard_refuses_production_model():
    # cleanup（ollama rm）は候補専用: 現行の本番モデル名は拒否（誤登録経由の削除防止）
    assert model_watch.is_production_model("cur:27b", _capacity())
    assert model_watch.is_production_model("coexist:35b", _capacity())
    assert not model_watch.is_production_model("cand:27b", _capacity())


def test_measure_load_failure_still_releases_and_restores(monkeypatch):
    # ロード後の ps に候補が現れない → 例外。それでも stop cand / 現行復帰は走る（finally）
    runner = FakeRunner([_PS_CUR, _PS_FREED, _PS_FREED, _PS_RESTORED])
    with pytest.raises(RuntimeError, match="見えない"):
        _measure(monkeypatch, runner)
    joined = "\n".join(runner.cmds)
    assert "stop cand:27b" in joined and '"model": "cur:27b"' in joined


# ---- 提案の出力契約 ----

def _sample_proposal_args():
    p = evaluate_swap(_capacity(), "ya-ta", "cand:27b", 19, rationale="test")
    spec = VerifiedSpec(model="cand:27b", hf_repo="X/Y", weights_gb=18.0,
                        hf_context=262144, license="apache-2.0", vision=True)
    meas = Measurement(size_gb=19.0, context=32768, host="mac-mini", restored=True)
    return p, spec, meas


def test_blocks_carry_button_contract():
    p, spec, meas = _sample_proposal_args()
    blocks = build_slack_blocks("model-swap-abc", p, spec, meas)
    actions = [b for b in blocks if b["type"] == "actions"][0]["elements"]
    # u-zu handlers/actions.py の @app.action と一致（request_id は value 経由）
    assert {(a["action_id"], a["value"]) for a in actions} == {
        ("approve_action", "model-swap-abc"), ("reject_action", "model-swap-abc")}


def test_propose_writes_pending_approval_and_record(tmp_path, monkeypatch):
    monkeypatch.setattr(model_watch, "APPROVAL_DIR", str(tmp_path / "approvals"))
    p, spec, meas = _sample_proposal_args()
    watch = {"record_dir": str(tmp_path / "records")}
    request_id = propose(p, spec, meas, watch, send_slack=False)

    approval = json.loads((tmp_path / "approvals" / f"{request_id}.json").read_text())
    # §8.10 形式: pending で作成され、u-zu resolve_approval が status を決着させられる
    assert approval["status"] == "pending"
    assert approval["type"] == "model_swap"
    assert approval["task_id"] == ""  # sa-ru の保留走査（task_id 一致）に拾われない
    record = json.loads((tmp_path / "records" / f"{request_id}.json").read_text())
    assert record["proposal"]["candidate_model"] == "cand:27b"
    assert record["measurement"]["size_gb"] == 19.0
