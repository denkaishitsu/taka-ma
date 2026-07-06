"""ファイルキューの原子書込ヘルパー — u-zu の各 writer で共有する（設計書 §8.3「書込の原子性」）。

キューファイル（task / conversation / exec-confirm）は sa-ru が別プロセスでポーリング読取する。
書込先を直接 truncate して書く（open(path, "w")）と、書込の途中で sa-ru が中途半端な JSON を
読む torn-read や、書込中クラッシュで壊れたファイルが本パスに残る事故が起きる。一時ファイルへ
全量書込してから os.replace で差し替えれば、リーダーは常に旧版全体か新版全体のいずれかだけを見る。

従来は approval_store / control_store が同じ tmp→os.replace をそれぞれ inline で持っていた。
規律を 1 箇所に集約し、writer 追加ごとにコピーがドリフトするのを防ぐ。
"""

import json
import os
import uuid


def atomic_write_json(path: str, obj) -> None:
    """obj を JSON として path へ原子的に書き込む（tmp へ全量書込 → os.replace で差し替え）。

    失敗時は孤児 .tmp を後始末してから例外を伝播する（os.replace 前に落ちた .tmp を残さない）。
    """
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
