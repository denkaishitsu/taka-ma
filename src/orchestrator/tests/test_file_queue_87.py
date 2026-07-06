"""task-87（ファイルキュー非原子書込＋クラッシュ回収）の振る舞いテスト。

grep/AST では潰せない振る舞い（予約回収が accepted/in_progress を init へ戻す・原子書込で
本パスに壊れた JSON が残らない）を分離実行で担保する。設計書 §8.3。
"""
import json
import os

import pytest

from orchestrator.file_queue import FileQueue, atomic_write_json


def _put(directory, name, status):
    path = os.path.join(directory, name)
    atomic_write_json(path, {"task_id": name, "status": status})
    return path


# ── H8: reclaim（予約の再起動回収・§8.3） ──

def test_reclaim_resets_reserved_to_init(tmp_path):
    q = FileQueue(str(tmp_path), poll_interval=1)
    _put(str(tmp_path), "a.json", "accepted")
    _put(str(tmp_path), "b.json", "in_progress")
    _put(str(tmp_path), "c.json", "init")        # 未予約はそのまま
    _put(str(tmp_path), "d.json", "completed")   # 対象外

    n = q.reclaim({"accepted", "in_progress"}, "init")

    assert n == 2
    assert json.loads((tmp_path / "a.json").read_text())["status"] == "init"
    assert json.loads((tmp_path / "b.json").read_text())["status"] == "init"
    assert json.loads((tmp_path / "c.json").read_text())["status"] == "init"
    assert json.loads((tmp_path / "d.json").read_text())["status"] == "completed"


def test_reclaimed_task_is_reclaimable_by_claim(tmp_path):
    """回収後、claim('init') が再取得できる（＝再処理される）ことを確認する。"""
    q = FileQueue(str(tmp_path), poll_interval=1)
    _put(str(tmp_path), "stuck.json", "in_progress")
    assert q.claim("init") is None            # 回収前は init が無く拾えない
    q.reclaim({"accepted", "in_progress"}, "init")
    picked = q.claim("init")
    assert picked is not None and picked[1]["status"] == "init"


def test_reclaim_returns_zero_when_nothing_reserved(tmp_path):
    q = FileQueue(str(tmp_path), poll_interval=1)
    _put(str(tmp_path), "a.json", "init")
    assert q.reclaim({"accepted", "in_progress"}, "init") == 0


# ── H6/H7: atomic_write_json（書込の原子性・§8.3） ──

def test_atomic_write_no_partial_file_on_serialize_error(tmp_path):
    """JSON 化不能オブジェクトで書込失敗しても、本パスに壊れたファイルも tmp も残さない。"""
    path = str(tmp_path / "rec.json")

    class NotSerializable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(path, {"x": NotSerializable()})
    # 本ファイルは作られない（os.replace 前に失敗）
    assert not os.path.exists(path)
    # 孤児 .tmp も後始末されている
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_replaces_old_version_wholesale(tmp_path):
    """既存ファイルの書換は旧版全体→新版全体で差し替わる（部分書込が観測されない）。"""
    path = str(tmp_path / "rec.json")
    atomic_write_json(path, {"status": "pending"})
    atomic_write_json(path, {"status": "confirmed"})
    assert json.loads((tmp_path / "rec.json").read_text())["status"] == "confirmed"
    assert list(tmp_path.glob("*.tmp")) == []
