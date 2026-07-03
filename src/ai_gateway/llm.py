"""ローカル ollama 呼び出しの共通口。

sa-ru プロセス内の複数箇所（ya-ta の分類・会話フロントエンド等）が `ollama run <model>` を
同じ形で叩いていたため、呼び出しを 1 箇所に集約する（timeout 等の調整漏れ・実装ぶれを防ぐ）。
JSON パースや用途別のフォールバックは呼び出し側の責務に残す（ここでは stdout を返すだけ）。
"""

import subprocess


def run_ollama(model: str, prompt: str, timeout: int) -> str:
    """`ollama run <model>` に prompt を stdin で渡し、stdout（生テキスト）を返す。

    例外（TimeoutExpired 等）は握りつぶさず呼び出し側へ送出する。
    """
    result = subprocess.run(
        ["ollama", "run", model],
        input=prompt,
        capture_output=True, text=True, timeout=timeout,
    )
    return result.stdout
