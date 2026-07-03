"""/taka-ma-model の引数パース — Slack コマンド本文からモデル定義を組み立てる純粋ロジック。

slack_bolt 等に依存しないため単体テスト可能。commands.py のハンドラから利用する。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
運用情報:   docs/operations/u-zu/slack-bot.md（add/update の引数例）
"""

# --flag を ya-ta.yaml の field 名へ写像する（--model-flag → model_flag 等）。
FLAG_TO_FIELD = {
    "--full-name": "full_name", "--vendor": "vendor", "--methods": "methods",
    "--command": "command", "--model-flag": "model_flag", "--model-id": "model_id",
    "--capabilities": "capabilities", "--description": "description", "--type": "type",
}
# カンマ区切りで配列にするフィールド。
LIST_FIELDS = {"methods", "capabilities"}

# --vendor から worker CLI 名（ya-ta.yaml の command）を推測する対応表。
# CLI 名が固定のベンダーのみ列挙。未知ベンダーは vendor 名をそのまま CLI 名に使う。
VENDOR_COMMAND = {"anthropic": "claude", "google": "agy"}


def parse_model_opts(tokens: list[str]) -> dict:
    """`--flag value` の並びを field 辞書へ変換する。未知フラグ・値欠落は ValueError。"""
    fields = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok not in FLAG_TO_FIELD:
            raise ValueError(f"不明なオプション: {tok}")
        if i + 1 >= len(tokens):
            raise ValueError(f"{tok} の値がありません")
        name = FLAG_TO_FIELD[tok]
        value = tokens[i + 1]
        fields[name] = [v.strip() for v in value.split(",") if v.strip()] \
            if name in LIST_FIELDS else value
        i += 2
    return fields


def build_model_conf(fields: dict) -> dict:
    """add 用に必須項目を補完したモデル定義を組み立てる。

    type は --type 明示が最優先。無ければ --vendor があれば api、--model-id があれば local。
    command は --command 明示が最優先。無ければ api は vendor から推測、local は ollama。
    """
    conf = dict(fields)
    if not conf.get("type"):
        if conf.get("vendor"):
            conf["type"] = "api"
        elif conf.get("model_id"):
            conf["type"] = "local"
        else:
            raise ValueError("type を判定できません（--vendor か --model-id か --type を指定）")
    if not conf.get("command"):
        if conf["type"] == "local":
            conf["command"] = "ollama"
        elif conf.get("vendor"):
            conf["command"] = VENDOR_COMMAND.get(conf["vendor"], conf["vendor"])
        else:
            raise ValueError("command を判定できません（--command か --vendor を指定）")
    return conf
