"""タスク作業ディレクトリのローテーション（§8.13 workspace の retention 削除）。

sa-ru が既定 workspace（`{workspace_base}/{task_id}`）で実行したタスクは、終了後も
作業ディレクトリ（clone したリポジトリ・生成物）が MBP に残り続け、1 件で数百 MB に
なり得る唯一の蓄積源となる。task_context レコード（`{ctx_dir}/{task_id}.json`）は
終了 status（completed/failed）を持ったままディスクに残る耐久記録なので、これを
「タスクが終了した」ことの判定根拠（SSOT）として retention 超過分を削除する。

削除しないもの（安全側の設計）:
- 実行中タスク（status が終了系でない、またはメモリ store に残っている task_id）
- `repo:` 明示指定の実開発リポジトリ（workspace が workspace_base 外 → レコードのみ削除）
- レコードを持たない orphan ディレクトリ（根拠なしに消さない。件数をログで可視化のみ）
"""

import datetime
import json
import logging
import os
import shutil

logger = logging.getLogger("sentinel.workspace_rotator")


def _is_under(path: str, base: str) -> bool:
    """path が base 配下の実パスかを判定する（symlink・`..` 混入で外部を指す事故の防止）。"""
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(base)
    return real_path.startswith(real_base.rstrip(os.sep) + os.sep)


def rotate_workspaces(ctx_dir: str, workspace_base: str, retention_days: int,
                      active_task_ids: set[str] | None = None,
                      on_before_delete=None):
    """終了済みタスクの workspace と task_context レコードを retention に従い削除する。

    判定は task_context レコードの status（completed/failed）とレコードファイルの
    mtime（= sa-ru が終了 status を push した時刻）で行う。ディレクトリ側の mtime は
    worker の書き込みで変わり「終了からの経過」を表さないため使わない。

    Args:
        ctx_dir: task_context レコードのディレクトリ（qu-e.yaml task_context.dir）
        workspace_base: 既定 workspace の基底（qu-e.yaml workspace_rotation.workspace_base）
        retention_days: 終了 push からの保持日数（qu-e.yaml workspace_rotation.retention_days）
        active_task_ids: メモリ store 上の実行中 task_id 集合。レコード内容と store が
            食い違った場合に store 側を優先して削除を見送る安全弁
        on_before_delete: workspace の rmtree 直前に呼ぶコールバック（引数は削除パス）。
            file_audit の suppress_subtree を渡し、自己操作の削除イベントが外部改変として
            escalate されるのを防ぐ（§8.12 との干渉回避）
    """
    if not os.path.isdir(ctx_dir):
        return
    active = active_task_ids or set()
    threshold = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    rotated_ids: set[str] = set()

    for name in sorted(os.listdir(ctx_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(ctx_dir, name)
        # 1 レコードの不備（壊れた json・権限）で全体を止めない。残骸は次回周回が拾う
        try:
            with open(path) as f:
                ctx = json.load(f)
            task_id = ctx.get("task_id")
            status = ctx.get("status")
            if not task_id or status not in ("completed", "failed"):
                continue
            if task_id in active:
                # レコード上は終了でも store が実行中と言うなら実行中扱い（安全側）
                continue
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
            if mtime >= threshold:
                continue

            workspace = os.path.expanduser(ctx.get("workspace") or "")
            if workspace and _is_under(workspace, workspace_base):
                # 既定 workspace（{base}/{task_id}）のみ実体を削除する。
                # workspace_base 外（repo: 指定の実開発リポジトリ）はユーザー資産のため触らない
                if os.path.isdir(workspace):
                    if on_before_delete:
                        on_before_delete(workspace)
                    shutil.rmtree(workspace)
                    logger.info("workspace rotation: 削除 task_id=%s workspace=%s", task_id, workspace)
            # レコード自身も蓄積源（1 タスク 1 ファイル）なので workspace と同時に削除する
            os.remove(path)
            rotated_ids.add(task_id)
        except Exception:
            logger.exception("workspace rotation: レコード処理失敗（スキップ）: %s", path)

    # レコードを持たない orphan（rotation 前から残っていた過去分・実行中タスクの実体）は
    # 削除根拠が無いため触らず、蓄積量の把握のため件数だけ可視化する
    try:
        if os.path.isdir(workspace_base):
            record_ids = _known_task_ids(ctx_dir)
            orphans = [n for n in os.listdir(workspace_base)
                       if os.path.isdir(os.path.join(workspace_base, n))
                       and n not in record_ids and n not in active]
            if orphans:
                logger.warning("workspace rotation: レコード無しの orphan %d 件（削除せず）: %s",
                               len(orphans), ", ".join(sorted(orphans)[:10]))
    except OSError:
        logger.exception("workspace rotation: orphan 走査失敗")


def _known_task_ids(ctx_dir: str) -> set[str]:
    """現存する task_context レコードの task_id 集合（orphan 判定用）。"""
    ids: set[str] = set()
    for name in os.listdir(ctx_dir):
        if name.endswith(".json"):
            ids.add(name[:-len(".json")])
    return ids
