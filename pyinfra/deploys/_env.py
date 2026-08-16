"""共有ヘルパー: 非対話シェルから起動されても Homebrew 配下のコマンドを解決させる。

WHY: deploy は `ollama` / `brew` / `uv` / `npm` を名前だけで呼ぶ。ログインシェル
（Mac mini のターミナル）には /opt/homebrew/bin が PATH に入っているため通るが、
`ssh mac-mini '... pyinfra ...'` のような非対話シェルには入らず
`sh: ollama: command not found` → `No hosts remaining!` で deploy 全体が停止する
（起動元によって成否が変わる環境依存になる）。

@local コネクタは pyinfra プロセスの環境変数を継承するため、deploy 読み込み時に
プロセスの PATH を正規化しておけば、対話・非対話のどちらから起動しても同じ結果になる。
既に PATH にある要素は足さないので、ターミナルからの従来の実行は挙動が変わらない。
"""
import os

# Homebrew の標準 prefix（Apple Silicon / Intel）。「brew 自体が PATH に無い」状況を
# 解決するのが目的のため `brew --prefix` では引けず、既知の 2 か所を直接指定する。
_BREW_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def ensure_brew_path() -> None:
    """pyinfra プロセスの PATH に Homebrew の bin を（不足していれば）前置する。"""
    # 空要素は捨てる。PATH の空要素はカレントディレクトリ扱いになり、PATH 未設定の
    # 環境（例: env -i 経由）で組み立て直すと "…:/usr/local/bin:" のように末尾へ
    # 空要素が残り、リポジトリ直下の同名ファイルを拾いうる。
    current = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    missing = [d for d in _BREW_BIN_DIRS if d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join(missing + current)
