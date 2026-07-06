"""ローカル ollama 呼び出しの共通口。

sa-ru プロセス内の複数箇所（ya-ta の分解・分類・リスク判定・会話フロントエンド等）が
`ollama run <model>` を同じ形で叩くため、呼び出しを 1 箇所に集約する
（timeout・フラグの調整漏れ・実装ぶれを防ぐ）。

DeepSeek-R1 等の推論モデルは既定で思考過程（Thinking...）と端末制御コード（word-wrap
の再描画 ANSI）を stdout に混ぜるため、--hidethinking（思考は内部で行い出力には出さない＝
判定品質は維持）と --nowordwrap（ANSI 混入の抑止）を常時付与する。これで残るのは
本文（多くは json フェンス付き）だけになる。JSON 抽出は extract_json を使う。
"""

import json
import re
import subprocess


def run_ollama(model: str, prompt: str, timeout: int) -> str:
    """`ollama run <model>` に prompt を stdin で渡し、stdout（生テキスト）を返す。

    思考表示・word-wrap ANSI を抑止する。例外（TimeoutExpired 等）は握りつぶさず送出する。
    ollama が非ゼロ終了した場合（未起動・モデル未 pull 等）は、空・部分的な stdout を
    正常な生成結果として返さず、stderr を添えて RuntimeError を送出する。呼び出し側は
    これをパースエラーと同列に扱い、用途別の安全側フォールバックへ落とす（設計書 §8.4）。
    """
    result = subprocess.run(
        ["ollama", "run", "--hidethinking", "--nowordwrap", model],
        input=prompt,
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ollama run '{model}' が失敗しました (exit {result.returncode}): "
            f"{result.stderr.strip() or '(stderr 無し)'}"
        )
    return result.stdout


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> str:
    """LLM 出力から JSON 本体（オブジェクト or 配列）を取り出して返す。

    モデルは ```json フェンスや前後の散文を付けることがあるため、
    (1) フェンス内 → (2) 散文中の各開き括弧を起点に、対応する同種の閉じ括弧まで
    型対応で切り出し、json.loads が通る最初の範囲、の順に取り出す。
    どちらも取れなければ元テキストを返す（呼び出し側で json.loads が失敗し
    用途別フォールバックに落ちる）。

    (2) では開き括弧の種別（`{` or `[`）を固定し、文字列リテラル内の括弧を無視しつつ
    深さを数えて対応する閉じ括弧で切り出す。`{`/`[` を独立に探して `min`/`max` で範囲を
    採る旧実装は、開き `{` と別種の閉じ `]` を跨ぐ等、対応の取れない範囲を返し得たため
    廃止した。加えて散文中の無関係な括弧（例: 文中の "[A]"）を誤採取しないよう、
    候補範囲は json.loads で妥当性を確認したものだけを採用する（設計書 §8.4「JSON 抽出の対応括弧」）。
    """
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    for start, opener in enumerate(text):
        if opener not in _OPEN_TO_CLOSE:
            continue
        candidate = _balanced_slice(text, start)
        if candidate is None:
            continue
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    return text.strip()


_OPEN_TO_CLOSE = {"{": "}", "[": "]"}


def _balanced_slice(text: str, start: int) -> str | None:
    """text[start]（開き括弧）から、対応する同種の閉じ括弧までの部分文字列を返す。

    開き括弧の種別を固定し、文字列リテラル内の括弧・引用符は構造にカウントせずに
    深さを数える。対応が閉じなければ（出力が途中で切れた等）None を返す。
    """
    opener = text[start]
    closer = _OPEN_TO_CLOSE[opener]
    depth = 0
    in_str = False
    escaped = False
    for j in range(start, len(text)):
        c = text[j]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return None
