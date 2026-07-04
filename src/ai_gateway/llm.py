"""ローカル ollama 呼び出しの共通口。

sa-ru プロセス内の複数箇所（ya-ta の分解・分類・リスク判定・会話フロントエンド等）が
`ollama run <model>` を同じ形で叩くため、呼び出しを 1 箇所に集約する
（timeout・フラグの調整漏れ・実装ぶれを防ぐ）。

DeepSeek-R1 等の推論モデルは既定で思考過程（Thinking...）と端末制御コード（word-wrap
の再描画 ANSI）を stdout に混ぜるため、--hidethinking（思考は内部で行い出力には出さない＝
判定品質は維持）と --nowordwrap（ANSI 混入の抑止）を常時付与する。これで残るのは
本文（多くは json フェンス付き）だけになる。JSON 抽出は extract_json を使う。
"""

import re
import subprocess


def run_ollama(model: str, prompt: str, timeout: int) -> str:
    """`ollama run <model>` に prompt を stdin で渡し、stdout（生テキスト）を返す。

    思考表示・word-wrap ANSI を抑止する。例外（TimeoutExpired 等）は握りつぶさず送出する。
    """
    result = subprocess.run(
        ["ollama", "run", "--hidethinking", "--nowordwrap", model],
        input=prompt,
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> str:
    """LLM 出力から JSON 本体（オブジェクト or 配列）を取り出して返す。

    モデルは ```json フェンスや前後の散文を付けることがあるため、
    (1) フェンス内 → (2) 最初の `{`/`[` から最後の `}`/`]` までの順に取り出す。
    どちらも取れなければ元テキストを返す（呼び出し側で json.loads が失敗し
    用途別フォールバックに落ちる）。
    """
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    ends = [i for i in (text.rfind("}"), text.rfind("]")) if i != -1]
    if starts and ends:
        s, e = min(starts), max(ends)
        if e > s:
            return text[s:e + 1]
    return text.strip()
