"""users.yaml の読み書き — ロール台帳の単一正本（SSOT）。

認可（role_check）と /taka-ma-user コマンドが共通でここを経由する。
書き込みは一時ファイル + os.replace で原子的に行い、途中失敗で台帳を壊さない。

構築手順書: docs/procedures/03-slack-bot.md（アクセス制御）
運用情報:   docs/operations/u-zu/slack-bot.md（ロール定義・ユーザー管理）
"""

import os
import tempfile

import yaml

# 有効なロール（運用書のロール定義に対応）
VALID_ROLES = ("owner", "admin", "user")

_DEFAULT_USERS_PATH = "/opt/taka-ma/config/users.yaml"


def _users_path() -> str:
    """users.yaml のパス。テストは TAKA_MA_USERS_PATH で差し替える。"""
    return os.environ.get("TAKA_MA_USERS_PATH", _DEFAULT_USERS_PATH)


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
    users = load_users()
    if user_id in users:
        raise ValueError(f"既に登録済み: {user_id}（変更は update）")
    users[user_id] = {"name": name, "role": role}
    save_users(users)


def update_user(user_id: str, role: str) -> None:
    """既存ユーザーのロールを変更する。未登録・不正ロールは ValueError。"""
    if role not in VALID_ROLES:
        raise ValueError(f"不正なロール: {role}（{', '.join(VALID_ROLES)} のいずれか）")
    users = load_users()
    if user_id not in users:
        raise ValueError(f"未登録: {user_id}（追加は add）")
    users[user_id]["role"] = role
    save_users(users)


def remove_user(user_id: str) -> None:
    """ユーザーを削除する。未登録は ValueError。"""
    users = load_users()
    if user_id not in users:
        raise ValueError(f"未登録: {user_id}")
    del users[user_id]
    save_users(users)
