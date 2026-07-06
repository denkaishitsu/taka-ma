"""§8.10 承認結果通知 — u-zu が Tier 3 承認ファイルの status を更新する。

sa-ru（orchestrator/tier3_handler.py）が `/opt/taka-ma/data/approvals/{request_id}.json` を
status=pending で作成し 1 秒ポーリングで待つ。u-zu はボタン押下／`/taka-ma-approve` を受けて
当該ファイルの status を approved / rejected に更新する（共有 FS、§8.3 と同経路）。
sa-ru がポーリングで検知し worker の y/n プロンプトに応答する。

旧実装は `# TODO: sa-ru に承認結果を通知` のまま承認ファイルを書かず、sa-ru の Future が
永久に未解決 → 5 分後に毎回 auto-deny だった。本サービスで cross-process を完成させる。
"""

import datetime
import fcntl
import json
import os
import re
import uuid

# sa-ru（tier3_handler.py）が作成・ポーリングするディレクトリと一致させる（§8.10）。
# 両プロセスに同じ環境変数 `TAKA_MA_APPROVAL_DIR` を与えれば供給元を 1 つにできる（パス直書きの SSOT 化）。
APPROVAL_DIR = os.environ.get("TAKA_MA_APPROVAL_DIR", "/opt/taka-ma/data/approvals")

# 受理する request_id の形式（uuid 相当：英数とハイフンのみ）。
# `/taka-ma-approve <request_id>` の入力をそのままファイル名に用いるため、
# パス区切り `/`・親参照 `..`・ドットを含むものを弾き、承認ディレクトリ外への
# パストラバーサル（例: `../../etc/x`）を防ぐ（§8.10 request_id の検証）。
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9-]+")

# status 契約値（§8.10）。sa-ru(tier3_handler.py) と**同じ文字列**を使う規約。
# 別ツリー配備で import 共有できないため定数化でタイプミスを防ぎ、grep で両側一致を確認する。
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
VALID_DECISIONS = (STATUS_APPROVED, STATUS_REJECTED)


def resolve_approval(request_id: str, decision: str, *, user_id: str) -> bool:
    """承認ファイル `{request_id}.json` の status を更新する（§8.10 フロー 4）。

    decision: "approved" または "rejected"。
    pending のときのみ更新し、更新できたら True を返す。
    ファイル不在・既決（approved/rejected/timeout 済）・不正 decision・不正 request_id
    のときは False（多重押下・期限切れ・呼び出し側の誤り・パストラバーサル入力）。

    read-check-write は承認ファイル単位のロック（`{path}.lock` の flock 排他）下で行う。
    多重押下・Approve/Reject の同時押下・sa-ru の timeout 確定が競合しても、pending を
    終端へ移せるのは 1 回だけで後続は False を返す（双方が成功報告する TOCTOU を防ぐ、§8.10）。
    sa-ru のポーラが中途半端な JSON を読まないよう、書き手ごとに一意な tmp へ書いて
    os.replace で原子的に差し替える（固定 tmp 名は別書き手と衝突しうるため避ける）。
    """
    if decision not in VALID_DECISIONS:
        return False
    # request_id をファイル名に用いる前に形式検証（パストラバーサル防止、§8.10）。
    if not _REQUEST_ID_RE.fullmatch(request_id or ""):
        return False

    path = os.path.join(APPROVAL_DIR, f"{request_id}.json")
    # ロックファイルを開く（承認ディレクトリ自体が無ければ承認要求も無い＝False）。
    try:
        lock_f = open(f"{path}.lock", "w")
    except (FileNotFoundError, NotADirectoryError):
        return False

    try:
        # 排他ロック取得。競合する別の決定・timeout 確定はここで直列化される。
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            with open(path) as f:
                record = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return False

        if record.get("status") != STATUS_PENDING:
            return False

        record["status"] = decision
        record["decided_at"] = datetime.datetime.now().astimezone().isoformat()
        record["decided_by"] = user_id

        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return True
    finally:
        fcntl.flock(lock_f, fcntl.LOCK_UN)
        lock_f.close()
