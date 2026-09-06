"""sa-ru の死活監視（設計書 §8.16.1・ADR 0002・#taka-ma/163.3）の振る舞いテスト。

検証する振る舞い:
- 心拍: 探針が応答すれば健全な心拍（last_ok）が前進し、心拍ファイルが最新 1 行で上書きされる。
  スレッドプール枯渇（探針が probe_timeout_sec で戻らない）では last_ok が前進せず、
  ログは遷移時（検知・回復）の各 1 回だけ。
- 判定: 起動直後は閾値分の猶予。途絶が閾値を超えたら is_stale。
- 自己終了と抑制: 途絶で exit(1)（通知は直近 1 時間の 1 回目のみ）。上限到達で終了せず
  打ち切りを通知。カウンタは 1 時間より古い記録を捨てる。
- ログ規律: ResourceMonitor の検知失敗は開始と回復の各 1 行。qu-e ヘルスチェックは
  状態遷移時のみ。
"""

import asyncio
import json
import logging
import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import liveness  # noqa: E402
from orchestrator.liveness import Heartbeat, RestartLimiter, run_watchdog  # noqa: E402
from orchestrator.resource_monitor import ResourceMonitor  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _conf(tmp_path, **over):
    conf = {"probe_interval_sec": 30, "probe_timeout_sec": 1, "stale_threshold_sec": 300,
            "check_interval_sec": 60,
            "heartbeat_path": str(tmp_path / "sa-ru.heartbeat"),
            "restart_count_path": str(tmp_path / "sa-ru.restart-count"),
            "restart_limit_per_hour": 3}
    conf.update(over)
    return conf


# ── 心拍 ──

def test_missing_yaml_key_fails_at_construction(tmp_path):
    conf = _conf(tmp_path)
    del conf["stale_threshold_sec"]
    with pytest.raises(KeyError):
        Heartbeat(conf)


def test_beat_advances_last_ok_and_overwrites_file(tmp_path):
    clock = _Clock()
    hb = Heartbeat(_conf(tmp_path), clock=clock, wall_clock=lambda: 1_700_000_000.0)
    clock.t += 100
    asyncio.run(hb.beat_once())
    assert hb.pool_ok is True
    assert hb.last_ok == 1100.0
    clock.t += 30
    asyncio.run(hb.beat_once())
    text = open(_conf(tmp_path)["heartbeat_path"]).read()
    assert text.count("\n") == 1                       # 常に最新 1 行（増えない）
    assert json.loads(text) == {"ts": 1_700_000_000.0, "pool_ok": True,
                                "pid": os.getpid(), "beats": 2}


def test_thread_pool_exhaustion_stops_healthy_beat_and_logs_once(tmp_path, monkeypatch, caplog):
    clock = _Clock()
    hb = Heartbeat(_conf(tmp_path, probe_timeout_sec=0.05), clock=clock)

    orig_to_thread = asyncio.to_thread   # liveness.asyncio は同一モジュールのため先に退避

    async def _never(fn):                # 探針が戻らない = プール枯渇の模擬
        await asyncio.sleep(10)
    monkeypatch.setattr(liveness.asyncio, "to_thread", _never)
    caplog.set_level(logging.INFO, logger="sa-ru.liveness")
    start_ok = hb.last_ok
    clock.t += 100
    asyncio.run(hb.beat_once())
    asyncio.run(hb.beat_once())
    assert hb.pool_ok is False
    assert hb.last_ok == start_ok                      # 健全な心拍は前進しない
    assert sum("枯渇" in r.getMessage() for r in caplog.records) == 1   # 遷移時のみ
    # 回復（探針が応答）→ 回復ログ 1 回・last_ok 前進
    monkeypatch.setattr(liveness.asyncio, "to_thread", orig_to_thread)
    clock.t += 30
    asyncio.run(hb.beat_once())
    assert hb.pool_ok is True and hb.last_ok == clock.t
    assert sum("回復" in r.getMessage() for r in caplog.records) == 1


def test_stale_after_threshold_with_startup_grace(tmp_path):
    clock = _Clock()
    hb = Heartbeat(_conf(tmp_path, stale_threshold_sec=300), clock=clock)
    clock.t += 299
    assert not hb.is_stale()                           # 起動直後の猶予
    clock.t += 2
    assert hb.is_stale()


# ── 自己終了と抑制 ──

def _watch(tmp_path, clock, wall, **over):
    hb = Heartbeat(_conf(tmp_path, **over), clock=clock, wall_clock=wall)
    limiter = RestartLimiter(hb.restart_count_path, hb.restart_limit, wall_clock=wall)
    return hb, limiter


def test_stale_triggers_exit_and_notifies_only_first_time(tmp_path):
    clock, wall = _Clock(), _Clock(1_700_000_000.0)
    hb, limiter = _watch(tmp_path, clock, wall)
    exits, notes = [], []
    kw = dict(notify=notes.append, reachability=lambda: "到達不能（最終疎通 09/04 11:20）",
              exit_fn=exits.append, once=True)
    assert run_watchdog(hb, limiter, **kw) == "ok"
    clock.t += 301
    assert run_watchdog(hb, limiter, **kw) == "exit"
    assert exits == [1]
    assert len(notes) == 1 and "自己終了" in notes[0] and "到達不能" in notes[0]
    # 2 回目（同一時間内・上限未満）は終了するが通知しない
    wall.t += 60
    assert run_watchdog(hb, limiter, **kw) == "exit"
    assert exits == [1, 1] and len(notes) == 1


def test_restart_limit_cuts_off_exit_and_notifies_once(tmp_path):
    clock, wall = _Clock(), _Clock(1_700_000_000.0)
    hb, limiter = _watch(tmp_path, clock, wall, restart_limit_per_hour=2)
    exits, notes = [], []
    kw = dict(notify=notes.append, reachability=None, exit_fn=exits.append, once=True)
    clock.t += 301
    assert run_watchdog(hb, limiter, **kw) == "exit"
    assert run_watchdog(hb, limiter, **kw) == "exit"
    assert run_watchdog(hb, limiter, **kw) == "cutoff"   # 直近 1 時間 2 回 = 上限
    assert exits == [1, 1]
    assert len(notes) == 2 and "打ち切り" in notes[1]
    # 1 時間経過で古い記録が捨てられ、再び自己終了できる
    wall.t += 3601
    assert run_watchdog(hb, limiter, **kw) == "exit"
    assert len(limiter.recent()) == 1


def test_restart_counter_survives_corrupt_file(tmp_path):
    wall = _Clock(1_700_000_000.0)
    path = tmp_path / "count"
    path.write_text("{broken")
    limiter = RestartLimiter(str(path), 3, wall_clock=wall)
    assert limiter.recent() == []
    assert limiter.record() == 1


def test_watchdog_thread_exception_is_fail_closed(tmp_path):
    clock, wall = _Clock(), _Clock(1_700_000_000.0)
    hb, limiter = _watch(tmp_path, clock, wall)
    exits = []

    class _Broken(Heartbeat):
        def is_stale(self, now=None):
            raise RuntimeError("bug")
    hb.__class__ = _Broken
    assert run_watchdog(hb, limiter, exit_fn=exits.append, once=True) == "exit"
    assert exits == [1]


# ── ログ規律 ──

class _PM:
    ssh_host, ssh_timeout = "mbp", 30

    def stop_ollama(self):
        return {}


def test_resource_monitor_logs_failure_start_and_recovery_once(monkeypatch, caplog):
    rm = ResourceMonitor(check_interval=0, process_mgr=_PM())
    calls = iter([OSError("no route"), OSError("no route"), OSError("no route"), False, False])

    def _detect():
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(rm, "detect_blender", _detect)
    caplog.set_level(logging.INFO, logger="sa-ru.resource_monitor")
    for _ in range(5):
        asyncio.run(rm._tick())
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("検知が失敗" in m for m in msgs) == 1
    assert sum("検知が回復" in m for m in msgs) == 1
    assert not any(r.exc_info for r in caplog.records)   # traceback の連投なし


def test_sentinel_health_logs_only_on_transition(caplog):
    from sentinel.main import log_health_transition
    caplog.set_level(logging.INFO, logger="qu-e")
    prev = None
    seq = ["healthy", "healthy", "critical", "critical", "healthy", "healthy"]
    for overall in seq:
        prev = log_health_transition(prev, {"overall": overall, "network": {}})
    msgs = [r.getMessage() for r in caplog.records if "ヘルスチェック" in r.getMessage()]
    assert len(msgs) == 3                                # 初回・critical へ・healthy へ
    assert msgs[1].startswith("ヘルスチェック: critical")
