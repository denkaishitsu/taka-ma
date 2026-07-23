"""Task #126（execution × depth 直交2軸ルーティング）の振る舞いテスト。

写像テーブル（_resolve_model）・昇格ラダー（_escalation_chain）・実行計画（_plan_execution）・
ESCALATE 自己申告検出（_escalate_reason）・レーン割当を、実物 ya-ta.yaml の routing 定義で
担保する。grep/AST では潰せない「どの軸の組がどのモデルへ落ちるか」「昇格の順序」「レーンが
解決モデルの method で決まるか」を分離実行で確認する（設計書 §2.2）。
"""
import types
from pathlib import Path

import pytest
import yaml

from orchestrator import (
    Orchestrator,
    _axis_label,
    _escalate_reason,
    _escalation_chain,
    _resolve_model,
)

# 実物 ya-ta.yaml（tests/ から見て ../../ai_gateway/config）を写像の権威として読む
_YATA = Path(__file__).resolve().parents[2] / "ai_gateway/config/ya-ta.yaml"


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(_YATA.read_text())


@pytest.fixture(scope="module")
def routing(config):
    return config["routing"]


# ── 写像テーブル（設計書 §2.2 の表）──

@pytest.mark.parametrize("execution,depth,conf,expected", [
    ("inline", None, 0.9, "gemma"),      # inline & 高 conf → gemma
    ("inline", None, 0.5, "haiku"),      # inline & 低 conf → haiku
    ("inline", "deep", 0.9, "gemma"),    # inline は depth 不問（deep でも gemma）
    ("agent", "shallow", 0.9, "haiku"),  # agent/shallow & 高 → haiku
    ("agent", "deep", 0.9, "opus"),      # agent/deep & 高 → opus
    ("agent", "shallow", 0.5, "sonnet"), # agent & 低 conf → sonnet（迷いの落下先）
    ("agent", "deep", 0.5, "sonnet"),    # agent/deep でも低 conf → sonnet
    ("agent", None, 0.9, "sonnet"),      # depth 省略 → sonnet（unspecified）
    ("agent", None, 0.5, "sonnet"),      # depth 省略 & 低 conf → sonnet
])
def test_resolve_model_matrix(routing, execution, depth, conf, expected):
    assert _resolve_model(routing, execution, depth, conf) == expected


def test_resolve_model_threshold_boundary(routing):
    """confidence == threshold（0.8）は high 側（境界含む）。"""
    assert _resolve_model(routing, "inline", None, 0.8) == "gemma"
    # ちょうど下回ると low 側
    assert _resolve_model(routing, "inline", None, 0.79) == "haiku"


def test_resolve_model_confidence_none_is_high(routing):
    """confidence 欠損（None）は high 扱い（多重防御）。"""
    assert _resolve_model(routing, "agent", "deep", None) == "opus"


# ── 昇格ラダー（設計書 §2.2「昇格ラダー」）──

def test_escalation_chain_from_ladder_member(routing):
    """primary がラダー上（haiku）→ 以降の段だけを返す。"""
    assert _escalation_chain(routing, "haiku") == ["haiku", "sonnet", "opus"]
    assert _escalation_chain(routing, "sonnet") == ["sonnet", "opus"]
    assert _escalation_chain(routing, "opus") == ["opus"]


def test_escalation_chain_from_off_ladder(routing):
    """primary がラダー外（gemma）→ gemma を先頭にラダー全体を続ける。"""
    assert _escalation_chain(routing, "gemma") == ["gemma", "haiku", "sonnet", "opus"]


def test_escalation_chain_none_primary(routing):
    """primary が None（写像未解決）→ ラダー全体。"""
    assert _escalation_chain(routing, None) == ["haiku", "sonnet", "opus"]


def test_escalation_chain_max_steps(routing):
    """max_steps=1 は primary を含めて 2 件に制限。"""
    assert _escalation_chain(routing, "haiku", max_steps=1) == ["haiku", "sonnet"]
    assert _escalation_chain(routing, "gemma", max_steps=0) == ["gemma"]


# ── ESCALATE 自己申告検出（設計書 §2.2「昇格の引き金 (a)」）──

def test_escalate_reason_detected():
    assert _escalate_reason("作業途中\nESCALATE: 設計判断が必要") == "設計判断が必要"


def test_escalate_reason_absent():
    assert _escalate_reason("通常の出力\n結果です") is None


def test_escalate_reason_non_string():
    assert _escalate_reason(None) is None


# ── _axis_label（通知・ドライラン表示）──

def test_axis_label_with_depth():
    assert _axis_label({"execution": "agent", "depth": "deep"}) == "agent/deep"


def test_axis_label_depth_omitted():
    assert _axis_label({"execution": "inline", "depth": None}) == "inline"


# ── _plan_execution（レーン・候補列・明示指定。self の重い依存を使わない）──

def _orch(config):
    o = Orchestrator.__new__(Orchestrator)
    o.config = config
    return o


def test_plan_inline_lane_is_inline(config):
    """inline & 高 conf → gemma（subprocess）→ inline レーン。"""
    o = _orch(config)
    lane, candidates, user_specified = o._plan_execution("inline", None, 0.9, None)
    assert lane == "inline"
    assert candidates[0] == "gemma"
    assert user_specified is False
    # gemma 失敗時の昇格先（headless 群）が続く
    assert candidates[1:] == ["haiku", "sonnet", "opus"]


def test_plan_agent_lane_is_agent(config):
    """agent/deep & 高 conf → opus（headless）→ agent レーン。"""
    o = _orch(config)
    lane, candidates, user_specified = o._plan_execution("agent", "deep", 0.9, None)
    assert lane == "agent"
    assert candidates == ["opus"]  # opus はラダー最終段 → 昇格先なし


def test_plan_low_conf_falls_to_sonnet(config):
    """迷い（低 conf）→ sonnet 起点、opus まで昇格。"""
    o = _orch(config)
    lane, candidates, _ = o._plan_execution("agent", "shallow", 0.5, None)
    assert lane == "agent"
    assert candidates == ["sonnet", "opus"]


def test_plan_user_specified_no_escalation(config):
    """:モデル名 明示指定 → 候補 1 件・昇格しない。"""
    o = _orch(config)
    lane, candidates, user_specified = o._plan_execution("inline", None, 0.9, "opus")
    assert candidates == ["opus"]
    assert user_specified is True
    assert lane == "agent"  # opus は headless → agent レーン


def test_plan_cross_review(config):
    """2 モデル以上の明示指定 → cross-review（agent レーン・候補列そのまま）。"""
    o = _orch(config)
    lane, candidates, user_specified = o._plan_execution(
        "agent", "deep", 0.9, ["opus", "gemini"])
    assert lane == "agent"
    assert candidates == ["opus", "gemini"]
    assert user_specified is True
