"""control_store（§8.10c 制御コマンド投入）の単体テスト。

検証対象:
- enqueue_control が status=pending の制御ファイルを controls/ に原子的に書く。
- sa-ru が読むキー（control_id/command/status/宛先）を欠かさない（契約整合）。
- 未知コマンドは握り潰さず ValueError（sa-ru で黙ってスキップされ無応答になるのを防ぐ）。
"""

import json

import pytest

from services import control_store


def test_enqueue_stop_ollama_writes_pending_record(tmp_path, monkeypatch):
    monkeypatch.setattr(control_store, "CONTROL_DIR", str(tmp_path))

    cid = control_store.enqueue_control(
        control_store.COMMAND_STOP_OLLAMA,
        user_id="U1", team_id="T1", channel_id="C1",
    )

    path = tmp_path / f"{cid}.json"
    assert path.exists()
    rec = json.loads(path.read_text())
    # sa-ru(control_q.claim / _handle_control) が参照するキーを網羅して確認（契約整合）
    assert rec["control_id"] == cid
    assert rec["command"] == "stop_ollama"
    assert rec["status"] == "pending"
    assert rec["user_id"] == "U1"
    assert rec["team_id"] == "T1"
    assert rec["channel_id"] == "C1"
    # tmp ファイルが残らない（os.replace で原子的に確定している）
    assert list(tmp_path.glob("*.tmp")) == []


def test_enqueue_cleans_tmp_on_write_failure(tmp_path, monkeypatch):
    """書込中に例外が起きても孤児 .tmp を残さない（リーク対策）。"""
    monkeypatch.setattr(control_store, "CONTROL_DIR", str(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(control_store.json, "dump", boom)

    with pytest.raises(RuntimeError):
        control_store.enqueue_control(
            control_store.COMMAND_STOP_OLLAMA,
            user_id="U1", team_id="T1", channel_id="C1",
        )
    # 確定ファイルも tmp も残っていない
    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_enqueue_unknown_command_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(control_store, "CONTROL_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        control_store.enqueue_control(
            "rm_rf_root", user_id="U1", team_id="T1", channel_id="C1",
        )
    # 不正命令ではファイルを作らない
    assert list(tmp_path.glob("*.json")) == []
