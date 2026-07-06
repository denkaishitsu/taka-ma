"""ロールベース認可 — Slack user ID と users.yaml で照合。

台帳の読み込みは user_store に一本化する（書き込みは /taka-ma-user 経由）。
未登録の Slack user ID は全ハンドラで拒否される（設計書 §1.2・運用書「アクセス制御」）。

構築手順書: docs/procedures/03-slack-bot.md（ロールチェックの実装）
"""

from services.user_store import load_users

# ロールの階層（数値が大きいほど強い権限）。owner ⊃ admin ⊃ user。
ROLE_ORDER = {"owner": 3, "admin": 2, "user": 1}


def get_role(user_id: str) -> str | None:
    """登録ロールを返す。未登録・role 欠落レコードなら None。"""
    user = load_users().get(user_id)
    return user.get("role") if user else None


def check_role(user_id: str, required_role: str) -> bool:
    """ロールチェック。required_role 以上の権限があれば True。
    未登録の Slack user ID は全て拒否（False）。role 欠落の壊れたレコードも
    未認可（レベル 0）として拒否する（user["role"] の KeyError で無応答にしない、M11）。
    """
    user = load_users().get(user_id)
    if not user:
        return False
    return ROLE_ORDER.get(user.get("role"), 0) >= ROLE_ORDER.get(required_role, 0)


def can_manage_user(actor_role: str | None, target_current_role: str | None,
                    new_role: str | None) -> bool:
    """/taka-ma-user の実行可否（運用書「コマンドごとのロール要件」※注）。

    - owner は何でも可。
    - admin は user 級のみ管理可。owner/admin を対象にする操作（既存が owner/admin、
      または付与しようとするロールが owner/admin）は owner 限定。
    - それ以外（未登録 actor 等）は不可。
    """
    if actor_role == "owner":
        return True
    if actor_role != "admin":
        return False
    if new_role in ("owner", "admin"):
        return False
    if target_current_role in ("owner", "admin"):
        return False
    return True


def role_denied_message(required_role: str) -> str:
    """認可拒否時に Slack へ返す統一メッセージ。"""
    return (
        f":no_entry: 権限がありません（要 {required_role} 以上）。"
        "管理者に `/taka-ma-user` での登録・昇格を依頼してください。"
    )


def authorize(user_id: str, required_role: str, say) -> bool:
    """認可ゲート。許可なら True。拒否なら拒否メッセージを say して False を返す。

    各ハンドラは ack 後に `if not authorize(...): return` で先頭ゲートする。
    """
    if check_role(user_id, required_role):
        return True
    say(role_denied_message(required_role))
    return False
