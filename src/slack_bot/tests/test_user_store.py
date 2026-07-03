"""user_store 単体テスト — users.yaml の読み書き。

構築手順書: docs/procedures/03-slack-bot.md（アクセス制御）
"""

import pytest

from services import user_store


@pytest.fixture
def users_file(tmp_path, monkeypatch):
    """テスト用の users.yaml を一時ディレクトリに割り当てる。"""
    path = tmp_path / "users.yaml"
    monkeypatch.setenv("TAKA_MA_USERS_PATH", str(path))
    return path


def test_load_missing_returns_empty(users_file):
    # 台帳が無い状態では空 dict（例外を投げない）
    assert user_store.load_users() == {}


def test_add_and_load(users_file):
    user_store.add_user("U1", "alice", "owner")
    users = user_store.load_users()
    assert users["U1"] == {"name": "alice", "role": "owner"}


def test_add_duplicate_raises(users_file):
    user_store.add_user("U1", "alice", "user")
    with pytest.raises(ValueError):
        user_store.add_user("U1", "alice", "admin")


def test_add_invalid_role_raises(users_file):
    with pytest.raises(ValueError):
        user_store.add_user("U1", "alice", "superuser")


def test_update_role(users_file):
    user_store.add_user("U1", "alice", "user")
    user_store.update_user("U1", "admin")
    assert user_store.load_users()["U1"]["role"] == "admin"


def test_update_missing_raises(users_file):
    with pytest.raises(ValueError):
        user_store.update_user("U404", "user")


def test_remove(users_file):
    user_store.add_user("U1", "alice", "user")
    user_store.remove_user("U1")
    assert "U1" not in user_store.load_users()


def test_remove_missing_raises(users_file):
    with pytest.raises(ValueError):
        user_store.remove_user("U404")


def test_save_is_atomic_on_role_failure(users_file):
    # 既存台帳がある状態で不正ロール更新を試みても台帳が壊れない
    user_store.add_user("U1", "alice", "owner")
    with pytest.raises(ValueError):
        user_store.update_user("U1", "bogus")
    assert user_store.load_users()["U1"]["role"] == "owner"
