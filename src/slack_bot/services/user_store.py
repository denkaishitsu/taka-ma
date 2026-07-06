"""users.yaml の読み書き — ロール台帳の単一正本（SSOT）。

認可（role_check）と /taka-ma-user コマンドが共通でここを経由する。
書き込みは一時ファイル + os.replace で原子的に行い、途中失敗で台帳を壊さない。

構築手順書: docs/procedures/03-slack-bot.md（アクセス制御）
運用情報:   docs/operations/u-zu/slack-bot.md（ロール定義・ユーザー管理）
"""

import contextlib
import fcntl
import os
import tempfile

import yaml

# 有効なロール（運用書のロール定義に対応）
VALID_ROLES = ("owner", "admin", "user")

_DEFAULT_USERS_PATH = "/opt/taka-ma/config/users.yaml"


def _users_path() -> str:
    """users.yaml のパス。テストは TAKA_MA_USERS_PATH で差し替える。"""
    return os.environ.get("TAKA_MA_USERS_PATH", _DEFAULT_USERS_PATH)


@contextlib.contextmanager
def _users_lock():
    """users.yaml の read-modify-write を直列化する排他ロック（`{path}.lock` の flock）。

    add/update/remove は「load → 変更 → save」の非原子な read-modify-write で、
    ロック無しでは並行実行が lost-update（片方の変更を取りこぼす）を起こす。
    書込系全経路をこのロック下に置いて直列化する（コマンド・ボタン双方から呼ばれる）。
    """
    path = _users_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_f = open(f"{path}.lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()


def _owner_count(users: dict) -> int:
    """owner ロールのユーザー数。role 欠落レコードは owner に数えない（.get で KeyError 回避）。"""
    return sum(1 for u in users.values() if u.get("role") == "owner")


def load_users() -> dict:
    """user_id -> {name, role} の辞書を返す。台帳が無ければ空 dict。"""
    path = _users_path()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return data.get("users", {}) or {}


def save_users(users: dict) -> None:
    """users 辞書を {"users": ...} 形式で原子的に書き出す。"""
    path = _users_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 同一ディレクトリに一時ファイルを作り、書き込み完了後に置換（部分書き込みを残さない）
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump({"users": users}, f, allow_unicode=True, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        # 置換前に失敗したら一時ファイルを掃除して既存台帳を温存する
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def add_user(user_id: str, name: str, role: str) -> None:
    """ユーザーを新規追加する。既存 user_id・不正ロールは ValueError。"""
    if role not in VALID_ROLES:
        raise ValueError(f"不正なロール: {role}（{', '.join(VALID_ROLES)} のいずれか）")
    with _users_lock():
        users = load_users()
        if user_id in users:
            raise ValueError(f"既に登録済み: {user_id}（変更は update）")
        users[user_id] = {"name": name, "role": role}
        save_users(users)


def update_user(user_id: str, role: str) -> None:
    """既存ユーザーのロールを変更する。未登録・不正ロールは ValueError。

    最後の 1 人となった owner を owner 以外へ降格しようとした場合は拒否する
    （Owner 不変条件、設計書 §1.2：システムを owner からロックアウトさせない）。
    """
    if role not in VALID_ROLES:
        raise ValueError(f"不正なロール: {role}（{', '.join(VALID_ROLES)} のいずれか）")
    with _users_lock():
        users = load_users()
        if user_id not in users:
            raise ValueError(f"未登録: {user_id}（追加は add）")
        if (role != "owner" and users[user_id].get("role") == "owner"
                and _owner_count(users) <= 1):
            raise ValueError(
                "最後の owner は降格できません（先に別のユーザーを owner にしてください）")
        users[user_id]["role"] = role
        save_users(users)


def remove_user(user_id: str) -> None:
    """ユーザーを削除する。未登録は ValueError。

    最後の 1 人となった owner の削除は拒否する（Owner 不変条件、設計書 §1.2）。
    """
    with _users_lock():
        users = load_users()
        if user_id not in users:
            raise ValueError(f"未登録: {user_id}")
        if users[user_id].get("role") == "owner" and _owner_count(users) <= 1:
            raise ValueError(
                "最後の owner は削除できません（先に別のユーザーを owner にしてください）")
        del users[user_id]
        save_users(users)
