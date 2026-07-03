"""role_check 単体テスト — 認可の階層判定と未登録拒否、authorize ゲート。

構築手順書: docs/procedures/03-slack-bot.md（ロールチェックの実装）
"""

import pytest

from services import user_store, role_check


@pytest.fixture
def populated(tmp_path, monkeypatch):
    """owner / admin / user を 1 名ずつ登録した台帳を用意する。"""
    path = tmp_path / "users.yaml"
    monkeypatch.setenv("TAKA_MA_USERS_PATH", str(path))
    user_store.add_user("U_OWNER", "owner1", "owner")
    user_store.add_user("U_ADMIN", "admin1", "admin")
    user_store.add_user("U_USER", "user1", "user")
    return path


def test_unregistered_denied_everywhere(populated):
    # 未登録 user_id は最弱の user 要件すら満たさない
    assert role_check.check_role("U_UNKNOWN", "user") is False
    assert role_check.check_role("U_UNKNOWN", "admin") is False
    assert role_check.check_role("U_UNKNOWN", "owner") is False


def test_owner_passes_all(populated):
    assert role_check.check_role("U_OWNER", "owner")
    assert role_check.check_role("U_OWNER", "admin")
    assert role_check.check_role("U_OWNER", "user")


def test_admin_hierarchy(populated):
    assert role_check.check_role("U_ADMIN", "admin")
    assert role_check.check_role("U_ADMIN", "user")
    assert role_check.check_role("U_ADMIN", "owner") is False


def test_user_hierarchy(populated):
    assert role_check.check_role("U_USER", "user")
    assert role_check.check_role("U_USER", "admin") is False
    assert role_check.check_role("U_USER", "owner") is False


def test_get_role(populated):
    assert role_check.get_role("U_ADMIN") == "admin"
    assert role_check.get_role("U_UNKNOWN") is None


def test_authorize_allows_and_is_silent(populated):
    said = []
    ok = role_check.authorize("U_OWNER", "owner", lambda msg: said.append(msg))
    assert ok is True
    assert said == []  # 許可時は何も say しない


def test_authorize_denies_and_says(populated):
    said = []
    ok = role_check.authorize("U_USER", "admin", lambda msg: said.append(msg))
    assert ok is False
    assert len(said) == 1
    assert "権限がありません" in said[0]


# --- /taka-ma-user の管理可否（can_manage_user）---

def test_owner_can_manage_anyone():
    assert role_check.can_manage_user("owner", None, "owner")
    assert role_check.can_manage_user("owner", "admin", "user")
    assert role_check.can_manage_user("owner", "owner", "user")


def test_admin_can_manage_user_level_only():
    # 新規 user の追加・既存 user の変更/削除（new_role=None は remove 相当）は可
    assert role_check.can_manage_user("admin", None, "user")
    assert role_check.can_manage_user("admin", "user", "user")
    assert role_check.can_manage_user("admin", "user", None)


def test_admin_cannot_touch_privileged():
    # admin が owner/admin を付与・既存 owner/admin を変更/削除するのは不可
    assert role_check.can_manage_user("admin", None, "admin") is False
    assert role_check.can_manage_user("admin", None, "owner") is False
    assert role_check.can_manage_user("admin", "owner", "user") is False
    assert role_check.can_manage_user("admin", "admin", None) is False


def test_non_admin_cannot_manage():
    assert role_check.can_manage_user("user", None, "user") is False
    assert role_check.can_manage_user(None, None, "user") is False
