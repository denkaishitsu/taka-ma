"""approval_store 単体テスト — Tier3 承認ファイルの status 更新（§8.10）。

Task #89 の堅牢化を検証する:
- H10: pending のときだけ一度だけ terminal に遷移し、既決は False（多重押下で双方成功しない）。
      排他ロック(flock)は同一プロセス内の並行呼び出しも直列化する。
- H12: request_id を uuid 相当の形式に限定し、パス区切り・.. を含む入力を拒否する
      （{request_id}.json 経由の承認ディレクトリ外書き込みを防ぐ）。

構築手順書: docs/procedures/03-slack-bot.md
"""

import json
import os

import pytest

from services import approval_store


@pytest.fixture
def approval_dir(tmp_path, monkeypatch):
    """承認ファイルを置く一時ディレクトリを割り当てる。

    APPROVAL_DIR は import 時に環境変数から束縛されるため（呼び出し毎に読み直す
    user_store 等とは異なる）、テストではモジュール属性を直接差し替える。
    """
    d = tmp_path / "approvals"
    d.mkdir()
    monkeypatch.setattr(approval_store, "APPROVAL_DIR", str(d))
    return d


def _write_pending(d, request_id):
    (d / f"{request_id}.json").write_text(json.dumps({"status": "pending"}))


def test_resolve_pending_succeeds_and_records(approval_dir):
    _write_pending(approval_dir, "abc-123")
    assert approval_store.resolve_approval("abc-123", "approved", user_id="U1") is True
    rec = json.loads((approval_dir / "abc-123.json").read_text())
    assert rec["status"] == "approved"
    assert rec["decided_by"] == "U1"
    assert rec["decided_at"]


def test_second_decision_on_decided_returns_false(approval_dir):
    # H10: 一度 terminal になった後の再決定は False（多重押下で双方成功報告しない）
    _write_pending(approval_dir, "r1")
    assert approval_store.resolve_approval("r1", "approved", user_id="U1") is True
    assert approval_store.resolve_approval("r1", "rejected", user_id="U2") is False
    # 先着の決定が保持される（後着は上書きしない）
    assert json.loads((approval_dir / "r1.json").read_text())["status"] == "approved"


def test_invalid_decision_rejected(approval_dir):
    _write_pending(approval_dir, "r2")
    assert approval_store.resolve_approval("r2", "maybe", user_id="U1") is False
    assert json.loads((approval_dir / "r2.json").read_text())["status"] == "pending"


def test_missing_file_returns_false(approval_dir):
    assert approval_store.resolve_approval("nope", "approved", user_id="U1") is False


@pytest.mark.parametrize("bad_id", [
    "../../etc/passwd",     # 親ディレクトリ脱出
    "a/b",                  # パス区切り
    "..",                   # 親参照
    "r.1",                  # ドット（拡張子偽装）
    "",                     # 空
])
def test_path_traversal_and_malformed_request_id_rejected(approval_dir, bad_id):
    # H12: 不正 request_id は False で弾き、ファイル生成・外部書き込みをしない
    assert approval_store.resolve_approval(bad_id, "approved", user_id="U1") is False


def test_traversal_does_not_touch_outside_file(approval_dir, tmp_path):
    # 脱出先に pending を置いても、検証で弾かれるので更新されない
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"status": "pending"}))
    approval_store.resolve_approval("../secret", "approved", user_id="U1")
    assert json.loads(outside.read_text())["status"] == "pending"


def test_concurrent_double_press_only_one_succeeds(approval_dir):
    # H10: 同一プロセス内の並行押下を flock が直列化し、成功は 1 回だけ
    import threading
    _write_pending(approval_dir, "race")
    results = []
    lock = threading.Lock()

    def press(decision, uid):
        r = approval_store.resolve_approval("race", decision, user_id=uid)
        with lock:
            results.append(r)

    ts = [threading.Thread(target=press, args=("approved", f"U{i}")) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert results.count(True) == 1
    assert results.count(False) == 7
