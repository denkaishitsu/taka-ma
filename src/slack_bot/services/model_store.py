"""ya-ta.yaml の models セクション読み書き — モデル登録の単一正本（SSOT）。

/taka-ma-model コマンド（add / update / remove / list）が共通でここを経由する。
sa-ru は起動時に ya-ta.yaml の models/routing をマージして読むため（orchestrator/__main__.py）、
編集後にサービス再起動すれば反映される（その再起動は handler 側 model_ops が担う）。

なぜ yaml.safe_dump で丸ごと書き戻さないか:
  ya-ta.yaml には OOM 回避の num_ctx 注記・routing 解説・外部サービス追加例（suno）等、
  運用上不可欠な手書きコメントが多数ある。safe_dump はコメントを全消去し並びも崩すため、
  対象モデルエントリの行だけをテキスト上で差し替える「サーキカル編集」を行い、
  他エントリ・コメント・整形を一切壊さない。編集後は必ず yaml.safe_load で全体を再パースし、
  意図どおり（追加/更新/削除）になっていることを確認してから原子的に書き出す（壊れた YAML を残さない）。

構築手順書: docs/procedures/03-slack-bot.md（Slash Commands / モデル管理）
運用情報:   docs/operations/u-zu/slack-bot.md（モデル管理）
"""

import os
import re
import tempfile

import yaml

# 本番のデプロイ先（sa-ru / ya-ta が読む実体）。テストは TAKA_MA_YATA_PATH で差し替える。
_DEFAULT_YATA_PATH = "/opt/taka-ma/ya-ta/config/ya-ta.yaml"

# models 配下のモデルキー行（2 スペースインデント・コメント行ではない）。例: "  opus4.6:"
_ENTRY_RE = re.compile(r"^  ([^\s#:][^:]*):\s*$")
# トップレベルキー（列 0 の非空白・非コメント）。models 領域の終端＝次のトップレベルキー。
_TOPLEVEL_RE = re.compile(r"^[^\s#]")

# add で受け付けるフィールドと、その YAML スカラー整形種別。
#   "str"  → ダブルクォート（full_name / model_flag / model_id / description）
#   "list" → フロー表記 [a, b]（methods / capabilities）
#   "bare" → 素のスカラー（type / vendor / command）
_FIELD_KIND = {
    "full_name": "str", "type": "bare", "vendor": "bare", "methods": "list",
    "command": "bare", "model_flag": "str", "model_id": "str",
    "capabilities": "list", "description": "str",
}
# 出力時のフィールド並び（既存エントリの記述順に合わせる）。
_FIELD_ORDER = ["full_name", "type", "vendor", "methods", "command",
                "model_flag", "model_id", "capabilities", "description"]


def _yata_path() -> str:
    """ya-ta.yaml の実パスを返す。テスト差し替え用に環境変数で上書き可能にしている。"""
    return os.environ.get("TAKA_MA_YATA_PATH", _DEFAULT_YATA_PATH)


def _read_lines() -> list[str]:
    """ya-ta.yaml を行単位で読み込む（行レベルで編集・差し替えするための入力）。"""
    with open(_yata_path()) as f:
        return f.readlines()


def _write_atomic(text: str) -> None:
    """同一ディレクトリの一時ファイルへ書いてから os.replace で原子的に差し替える。"""
    path = _yata_path()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_models() -> dict:
    """models 辞書（key -> conf）を返す。

    ya-ta.yaml がまだ存在しない（ya-ta 未構築）場合・models セクションが
    無い場合はいずれも空 dict を返す。読取系は設定不在をエラーにせず
    「登録モデル無し」として扱う（書込系 add/install は別途ファイルを要求）。
    """
    try:
        with open(_yata_path()) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        # 壊れた ya-ta.yaml は YAMLError（OSError でも ValueError でもない）を投げる。
        # そのまま伝播すると handler の except (ValueError, OSError) を素通りして
        # ack 後にハンドラが落ち、ユーザーへ何も返らない（M9）。ValueError に正規化する。
        raise ValueError(f"ya-ta.yaml の解析に失敗しました: {e}") from e
    return data.get("models") or {}


def get_model(key: str) -> dict | None:
    """登録済みモデル定義を返す。未登録なら None。"""
    return load_models().get(key)


def _models_region(lines: list[str]) -> tuple[int, int]:
    """`models:` 行の直後 index と models 領域の終端 index（次トップレベルキー）を返す。

    models セクションが無ければ ValueError。
    """
    m_idx = next((i for i, ln in enumerate(lines)
                  if re.match(r"^models:\s*(#.*)?$", ln)), None)
    if m_idx is None:
        raise ValueError("ya-ta.yaml に models セクションがありません")
    end = next((i for i in range(m_idx + 1, len(lines))
                if _TOPLEVEL_RE.match(lines[i])), len(lines))
    return m_idx + 1, end


def _entries(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    """models 領域 [start, end) のモデルエントリを (key, 開始 index, 終端 index) で列挙する。

    エントリは「2 スペースのキー行」から始まり、次のモデルキー行（_ENTRY_RE）または
    トップレベルキーが現れる手前までを 1 エントリとする。配下の 4 スペースフィールド行・
    ブロックスカラー継続・フィールド間に挟まれたコメント行・空行を含む。

    ただし末尾の空行と 2 スペースコメント（次エントリ用の区切りやコメント例ブロック）は
    エントリ範囲に含めない。これにより (1) フィールド間のコメントでエントリが途中分割されず、
    (2) remove で末尾のコメント例ブロックや隣の区切り空行を巻き込まない。
    """
    result = []
    i = start
    while i < end:
        m = _ENTRY_RE.match(lines[i])
        if not m:
            i += 1
            continue
        key = m.group(1)
        j = i + 1
        while j < end and not _ENTRY_RE.match(lines[j]) \
                and not _TOPLEVEL_RE.match(lines[j]):
            j += 1
        # 末尾の空行・コメント行を範囲から除外する。models 領域でエントリ末尾に続く
        # 標準パターンは「区切り空行＋コメント例ブロック（suno）＋次セクションを説明する
        # 列 0 コメント」で、これらはどのエントリにも属さない。indent を問わず trim して
        # remove がそれらを巻き込まないようにする（フィールド間の内部コメントは、後ろに
        # フィールドが続くので trim ループの停止条件で保持される）。
        e = j
        while e > i + 1 and (lines[e - 1].strip() == ""
                             or lines[e - 1].lstrip().startswith("#")):
            e -= 1
        result.append((key, i, e))
        i = j
    return result


def _fmt_scalar(name: str, value) -> str:
    """フィールド名に応じて YAML スカラー文字列へ整形する（既存エントリの体裁に合わせる）。"""
    kind = _FIELD_KIND.get(name, "bare")
    if kind == "list":
        items = value if isinstance(value, list) else [value]
        return "[" + ", ".join(str(v) for v in items) + "]"
    if kind == "str":
        # ダブルクォート内の " をエスケープ（YAML 二重引用符仕様）。
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
    return str(value)


def _render_entry(key: str, conf: dict) -> list[str]:
    """1 モデルエントリ分の行（末尾改行付き、末尾に空行 1 行）を生成する。"""
    out = [f"  {key}:\n"]
    for name in _FIELD_ORDER:
        if name in conf and conf[name] not in (None, ""):
            out.append(f"    {name}: {_fmt_scalar(name, conf[name])}\n")
    out.append("\n")
    return out


def _validated_write(lines: list[str], key: str, must_exist: bool) -> None:
    """編集後の行を結合 → yaml 再パースで models[key] の有無を検証 → 原子的に書き出す。"""
    text = "".join(lines)
    try:
        parsed = (yaml.safe_load(text) or {}).get("models") or {}
    except yaml.YAMLError as e:
        # 行編集の結果が壊れた YAML になった場合。壊れたファイルを書き出さず、
        # YAMLError を handler が扱える ValueError に正規化して中断する（M9）。
        raise ValueError(f"編集後の ya-ta.yaml が不正な YAML になりました: {e}") from e
    if must_exist and key not in parsed:
        raise ValueError(f"内部エラー: 編集後に {key} が models に見つかりません")
    if not must_exist and key in parsed:
        raise ValueError(f"内部エラー: 編集後も {key} が models に残っています")
    _write_atomic(text)


def add_model(key: str, conf: dict) -> None:
    """新規モデルを models へ追加する。既存キーは ValueError。

    conf は full_name / type / command / methods を必須とし、vendor / model_flag /
    model_id / capabilities / description を任意で持つ（handler が組み立てる）。
    methods はルーティングの呼び出し方法（pty/subprocess）を決める核情報で、
    欠けるとモデルが登録できても起動経路が不定になるため必須にする（usage 表記と一致）。
    """
    for required in ("full_name", "type", "command", "methods"):
        if not conf.get(required):
            raise ValueError(f"必須項目が不足: {required}")
    lines = _read_lines()
    body_start, body_end = _models_region(lines)
    entries = _entries(lines, body_start, body_end)
    if any(k == key for k, _, _ in entries):
        raise ValueError(f"既に登録済み: {key}（変更は update）")
    # 最後のモデルエントリ直後（コメント例ブロックの手前）へ挿入する。
    insert_at = entries[-1][2] if entries else body_start
    lines[insert_at:insert_at] = _render_entry(key, conf)
    _validated_write(lines, key, must_exist=True)


# エントリ内の管理フィールド行（4 スペース・"name:"）。ブロックスカラー継続（6 スペース）は除外。
_MANAGED_FIELD_RE = re.compile(r"^    ([^\s:]+):")


def _field_insert_index(lines: list[str], e_start: int, e_end: int, name: str) -> int:
    """新フィールド name を _FIELD_ORDER 準拠で挿入する index を返す。

    _FIELD_ORDER 上 name より前に並ぶべき既存フィールドのうち、最後のものの直後。
    該当が無ければキー行直後。add 時の _render_entry と同じ並びを update でも保つため。
    """
    order = _FIELD_ORDER.index(name) if name in _FIELD_ORDER else len(_FIELD_ORDER)
    insert_at = e_start + 1
    for i in range(e_start + 1, e_end):
        m = _MANAGED_FIELD_RE.match(lines[i])
        if not m:
            continue
        existing = m.group(1)
        existing_order = (_FIELD_ORDER.index(existing)
                          if existing in _FIELD_ORDER else len(_FIELD_ORDER))
        if existing_order < order:
            insert_at = i + 1
    return insert_at


def update_model(key: str, fields: dict) -> None:
    """既存モデルの一部フィールドを差し替える。未登録は ValueError。

    対象フィールド行があれば値を置換し、無ければ _FIELD_ORDER 準拠の位置へ挿入する
    （他フィールド・コメントは保持）。
    """
    if not fields:
        raise ValueError("更新するフィールドがありません")
    lines = _read_lines()
    body_start, body_end = _models_region(lines)
    entry = next((e for e in _entries(lines, body_start, body_end) if e[0] == key), None)
    if entry is None:
        raise ValueError(f"未登録: {key}（追加は add）")
    _, e_start, e_end = entry
    for name, value in fields.items():
        new_line = f"    {name}: {_fmt_scalar(name, value)}\n"
        field_re = re.compile(rf"^    {re.escape(name)}:\s")
        idx = next((i for i in range(e_start + 1, e_end) if field_re.match(lines[i])), None)
        if idx is not None:
            lines[idx] = new_line
        else:
            # 未存在フィールドは _FIELD_ORDER 準拠位置へ挿入（以降の境界 e_end もずらす）。
            insert_at = _field_insert_index(lines, e_start, e_end, name)
            lines.insert(insert_at, new_line)
            e_end += 1
    _validated_write(lines, key, must_exist=True)


def remove_model(key: str) -> None:
    """モデルエントリを削除する。未登録は ValueError。"""
    lines = _read_lines()
    body_start, body_end = _models_region(lines)
    entry = next((e for e in _entries(lines, body_start, body_end) if e[0] == key), None)
    if entry is None:
        raise ValueError(f"未登録: {key}")
    _, e_start, e_end = entry
    del lines[e_start:e_end]
    _validated_write(lines, key, must_exist=False)
