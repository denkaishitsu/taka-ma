"""Task #161: 判定ログ集計（ローカル脳の換装判断基準・§8.4.1）のテスト。

- YaTaLogger.log_decompose_call: 1 呼び出し = 1 行の成否記録（フォールバック率の分母）
- decision_stats.collect_stats: 分解フォールバック率・契約化ローカル縮退率の算出
  （LLM 不使用・JSONL パースのみ）と発動基準の exit code
"""

import datetime
import json
import os

from ai_gateway import decision_stats
from ai_gateway.logger import YaTaLogger


def _write_log(log_dir, entries, days_ago=0):
    os.makedirs(log_dir, exist_ok=True)
    date = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    with open(os.path.join(log_dir, f"ya-ta-decisions-{date}.jsonl"), "a") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_log_decompose_call_writes_one_line(tmp_path):
    logger = YaTaLogger(log_dir=str(tmp_path))
    logger.log_decompose_call(task="X" * 300, fallback=True,
                              reason="分解フォールバック発動: ValueError", subtasks=1)
    files = os.listdir(tmp_path)
    assert len(files) == 1
    entry = json.loads(open(tmp_path / files[0]).read())
    assert entry["kind"] == "decompose_call"
    assert entry["fallback"] is True
    assert len(entry["task"]) == 200        # 200 字に切り詰め


def test_collect_stats_computes_both_rates(tmp_path):
    log_dir = str(tmp_path)
    _write_log(log_dir, [
        {"kind": "decompose_call", "fallback": False},
        {"kind": "decompose_call", "fallback": False},
        {"kind": "decompose_call", "fallback": True},
        {"kind": "decompose_call", "fallback": True},
        {"kind": "contract", "degraded": False},
        {"kind": "contract", "degraded": True},
        # kind 無し（サブタスク単位の log_decision 行）は率の分母に入らない
        {"task": "x", "execution": "agent"},
    ])
    # 期間外（8 日前）は集計対象外
    _write_log(log_dir, [{"kind": "decompose_call", "fallback": True}], days_ago=8)
    stats = decision_stats.collect_stats(log_dir, days=7)
    assert stats["decompose_total"] == 4
    assert stats["decompose_fallback"] == 2
    assert stats["decompose_fallback_rate"] == 0.5
    assert stats["contract_total"] == 2
    assert stats["contract_degraded_rate"] == 0.5


def test_collect_stats_empty_is_none_not_zero(tmp_path):
    """記録なしは N/A（None）— 0% と混同して「健全」と誤読させない。"""
    stats = decision_stats.collect_stats(str(tmp_path), days=7)
    assert stats["decompose_fallback_rate"] is None
    assert stats["contract_degraded_rate"] is None


def test_collect_stats_skips_broken_lines(tmp_path):
    log_dir = str(tmp_path)
    date = datetime.date.today().isoformat()
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, f"ya-ta-decisions-{date}.jsonl"), "w") as f:
        f.write("{broken json\n")
        f.write(json.dumps({"kind": "decompose_call", "fallback": False}) + "\n")
    stats = decision_stats.collect_stats(log_dir, days=1)
    assert stats["decompose_total"] == 1


def test_main_exit_code_signals_threshold_trigger(tmp_path, capsys):
    """発動基準超過は exit 1（cron・手動どちらでも「提案が必要」を機械判定できる）。"""
    log_dir = str(tmp_path)
    _write_log(log_dir, [{"kind": "decompose_call", "fallback": True},
                         {"kind": "decompose_call", "fallback": False}])  # 50% > 5%
    rc = decision_stats.main(["--log-dir", log_dir, "--days", "7"])
    assert rc == 1
    assert "発動" in capsys.readouterr().out


def test_main_exit_zero_when_healthy(tmp_path, capsys):
    log_dir = str(tmp_path)
    _write_log(log_dir, [{"kind": "decompose_call", "fallback": False}] * 100)
    rc = decision_stats.main(["--log-dir", log_dir, "--days", "7"])
    assert rc == 0
