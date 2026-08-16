"""§8.3 上り発話の正規化 — 搬送路（Slack）が本文へ混ぜ込む情報を落とす。

sa-ru から先は「人間が言ったことだけ」が流れる状態を不変条件とし、搬送路の都合を
中核へ持ち込まない。現に落とす必要があるのは、ユーザー本人の認可でアプリが投稿した
発話の末尾に Slack が追記するアプリ帰属表記（`*<文言>* <アプリ名>`）。

これが残ると sa-ru の意図解釈に常時ノイズが乗るだけでなく、訂正の簡易記法
（§10.2.1、orchestrator/plan.py の `_SIMPLE_RE`）は行全体をアンカーする決定的パースの
ため必ず不一致になり、決定的経路が失われる。
"""

import logging
import os
import re

import yaml

logger = logging.getLogger("u-zu.sanitize")

# 帰属表記の「形」。Slack は本文末尾へ `*<文言>* <アプリ名>` を足す。文言は変わり得るので、
# 既知の suffix に一致しなくてもこの形をしていれば「表記が変わった」と判る。
# 末尾のアプリ名を ASCII 語に限るのは、日本語本文と区別するため。`*重要* な件を進めてくれ。`
# のように強調で始まる普通の発話は、日本語には語間の空白が無いため末尾 1 語として丸ごと拾われ、
# 制限が無いと帰属表記と見分けが付かない（実際に誤検知した）。アプリ名を日本語にすると検知
# できなくなるが、監視したいのは文言の変更であってアプリ名は `taka-ma-mcp` で固定である。
_ATTRIBUTION_SHAPE = re.compile(r"\*[^*\n]+\*[^\S\n]+[A-Za-z0-9][A-Za-z0-9._-]*\s*$")

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "u-zu.yaml")


def load_sanitize_apps(path: str = CONFIG_PATH) -> list[dict]:
    """u-zu.yaml（SSOT）から正規化対象アプリの定義を読む。

    キー欠落・ファイル不在は起動時に例外で落とす（正規化なしで常駐する偽正常を許さない。
    コード側に既定値を置かない #103 の方針）。無効化したい場合は `apps: []` と明示する。
    """
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {})["inbound_sanitize"]["apps"]


def strip_app_attribution(text: str, app_id, apps: list[dict]) -> str:
    """アプリ経由投稿の本文末尾から、Slack が付けたアプリ帰属表記を落とす。

    app_id: Slack イベントの `app_id`（人手で打った発話には付かないので None になる）。
    apps:   u-zu.yaml の `inbound_sanitize.apps`。app_id と suffix 群を 1 ブロックで持つ。

    判定を本文ではなく app_id で行うのが要点。ユーザーが偶然同じ文字列で終わる発話を
    打っても、app_id が無いため対象にならない（誤除去の構造的な排除）。
    """
    if not app_id:
        return text
    for entry in apps:
        if entry["app_id"] != app_id:
            continue
        for suffix in entry["attribution_suffixes"]:
            if text.endswith(suffix):
                return text[: -len(suffix)].rstrip()
        # 既知の表記に一致しなかった。ここで一律に警告してはならない。同じ app_id でも、
        # ユーザー本人の OAuth トークンで投稿した発話（G2 リレーの `relay.sh`）には帰属表記が
        # 付かないため、毎発話が警告になる（実機で全発話に出た）。
        # 鳴らすべきなのは「帰属表記の形はあるのに既知の文言に一致しない」＝ Slack の文言変更・
        # アプリ改名の疑いがある場合だけ。放置すると原文汚染が静かに復活する。
        if _ATTRIBUTION_SHAPE.search(text):
            logger.warning(
                "アプリ帰属表記を除去できません（app_id=%s 末尾30字=%r）。"
                "u-zu.yaml の inbound_sanitize.apps[].attribution_suffixes を更新してください",
                app_id, text[-30:])
        return text
    # 未登録アプリ。他社アプリの投稿には干渉しない。
    return text
