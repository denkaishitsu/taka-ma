"""ローカル ollama 呼び出しの共通口。

sa-ru プロセス内の複数箇所（ya-ta の分解・分類・リスク判定・会話フロントエンド等）が
同じ形でローカル LLM を叩くため、呼び出しを 1 箇所に集約する
（timeout・パラメータの調整漏れ・実装ぶれを防ぐ）。

呼び出しは `ollama run` の subprocess 起動ではなく **HTTP API（/api/generate）** を使う
（設計書 §8.4「ollama 呼び出し方式」）。subprocess 方式は呼び出しごとに CLI が起動し、
keep_alive を制御できずモデルのロード・プロンプト全量評価を毎回払うため、会話履歴が
伸びるほど毎ターン遅くなる欠陥があった（実運用実測: 会話 1 ターン中央値 44 秒）。
HTTP API では keep_alive でモデルを常駐させ、同一プレフィックスの KV キャッシュ再利用が
効き、接続失敗と生成タイムアウトを例外として区別できる。

応答は stream=true（NDJSON 逐次受信）で受ける（設計書 §8.4「ollama 実行失敗の検知」）。
逐次受信するのは、生成中のトークン数を GenerationProgress へ記録し、ハートビート進捗通知
（§10.8）から読めるようにするため。timeout は deadline 方式で接続〜生成完了の全体に適用する
（チャンクが届き続けていても全体の壁時計で打ち切る）。

思考系モデルの思考過程は API では `response`（本文）と分離して返るため、CLI 時代の
--hidethinking / --nowordwrap 相当の抑止は不要（ANSI 混入も TTY 非経由のため起きない）。
JSON 抽出は従来どおり extract_json を使う。
"""

import http.client
import json
import logging
import re
import time
import urllib.parse

logger = logging.getLogger("sa-ru.llm")

# モデルを常駐させ続ける（Mac mini は sa-ru/ya-ta 専用機であり、アンロードして
# 空けたメモリの使い道がない。ロード往復の排除を優先する。設計書 §8.4）。
# 数値で渡すこと: ollama API の keep_alive は数値（秒・負=無期限）か単位付き文字列
# （"-1m" 等）のみ受理し、単位なし文字列 "-1" はパース不能でリクエストごと拒否される
KEEP_ALIVE = -1


class OllamaError(RuntimeError):
    """ollama 呼び出しの失敗（基底）。従来 RuntimeError を捕捉していた呼び出し側の
    安全側フォールバック（設計書 §8.4）がそのまま機能するよう RuntimeError を継承する。"""


class OllamaTimeoutError(OllamaError):
    """生成がタイムアウトした（モデルは応答中だったが制限秒数を超えた）。"""


class OllamaConnectionError(OllamaError):
    """ollama へ接続できない・応答が異常（未起動・モデル未 pull・HTTP エラー・ストリーム不正）。"""


class GenerationProgress:
    """生成進捗の共有ホルダー（設計書 §10.8）。

    生成スレッド（run_ollama）が受信チャンクごとに tokens を進め、ハートビート側の
    別スレッドが読む。int 属性の更新・参照のみ（CPython の GIL 下で不可分）のためロックは
    持たない。1 回の生成につき 1 個を作り使い捨てる。
    """

    def __init__(self):
        self.tokens = 0  # 生成済みトークン数（受信チャンク数≒トークン数。完了時は eval_count で確定）


def run_ollama(model: str, prompt: str, timeout: int, host: str,
               think: bool | None = None,
               progress: GenerationProgress | None = None) -> str:
    """ollama HTTP API `/api/generate`（stream=true）で prompt を生成し、本文テキストを返す。

    host は `sa-ru.yaml` の `sa-ru.ollama_host`（例 "http://localhost:11434"）を呼び出し元
    から渡す（供給元を 1 つに保つ）。

    think: 思考型モデルの思考の有効/無効。None は payload に含めない（think 非対応
    モデルとの互換・モデル既定に従う）。False は思考自体をスキップする——会話脳の
    実測で思考が 1 ターン約 1400 トークン・所要 30 秒の支配項だったため、判定品質より
    応答速度を優先する用途（会話）で False を渡す（設計書 §8.4。分解・分類・リスク判定は
    ya-ta.yaml の llm_think に従う）。

    timeout は deadline 方式で接続〜生成完了の全体に適用し、超過時は OllamaTimeoutError を
    送出する（逐次受信が続いていても打ち切る。設計書 §8.4）。接続失敗・HTTP エラー応答・
    ストリーム中のエラーチャンク／不正行は、空・部分的な出力を正常な生成結果として返さない
    よう OllamaConnectionError を送出する。いずれも RuntimeError 派生のため、呼び出し側は
    区別してリトライ・通知文言に反映するか、従来どおり RuntimeError として一括で安全側
    フォールバックに落とすかを選べる。

    progress を渡すと受信チャンクごとに生成トークン数を記録する（§10.8 ハートビートが読む）。
    """
    deadline = time.monotonic() + timeout
    url = urllib.parse.urlsplit(host)
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "keep_alive": KEEP_ALIVE,
        }
        if think is not None:
            payload["think"] = think
        try:
            conn.request(
                "POST", "/api/generate",
                body=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            # 接続クローズ型応答では getresponse() 後に conn.sock が None になるため、
            # deadline 適用（settimeout）用のソケット参照をここで確保する
            sock = conn.sock
            resp = conn.getresponse()
        except TimeoutError as e:
            # 接続・応答ヘッダ待ちの超過。deadline 超過と同じ契約（timeout）として送出する
            raise OllamaTimeoutError(
                f"ollama '{model}' への接続/応答が {timeout} 秒を超えました ({host})") from e
        except OSError as e:
            raise OllamaConnectionError(
                f"ollama へ接続できません（未起動の可能性・{host}）: {e}") from e
        if resp.status != 200:
            detail = resp.read(2048).decode("utf-8", errors="replace")[:300]
            raise OllamaConnectionError(
                f"ollama '{model}' が HTTP {resp.status} を返しました: {detail.strip()}")

        parts: list[str] = []
        done_chunk: dict = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OllamaTimeoutError(
                    f"ollama '{model}' の生成が {timeout} 秒を超えました")
            # 各読み取りの待ちを残り時間で頭打ちにする＝生成全体への deadline 適用。
            # 読み取り超過は socket.timeout（= TimeoutError）として送出される。
            sock.settimeout(remaining)
            try:
                line = resp.readline()
            except TimeoutError as e:
                raise OllamaTimeoutError(
                    f"ollama '{model}' の生成が {timeout} 秒を超えました") from e
            if not line:
                break  # done チャンク無しの切断。取得済み本文を返し、下流のパースに委ねる
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError as e:
                # NDJSON 契約違反（プロキシ・サーバ異常等）。部分出力を正常結果として返さない
                raise OllamaConnectionError(
                    f"ollama ストリーム応答を解釈できません: {line[:200]!r}") from e
            if chunk.get("error"):
                raise OllamaConnectionError(f"ollama がエラーを返しました: {chunk['error']}")
            piece = chunk.get("response", "")
            parts.append(piece)
            # 1 チャンク≒1 トークン。思考（thinking）チャンクも生成の進捗として数える
            if progress is not None and (piece or chunk.get("thinking")):
                progress.tokens += 1
            if chunk.get("done"):
                done_chunk = chunk
                if progress is not None and chunk.get("eval_count"):
                    progress.tokens = chunk["eval_count"]  # 実測トークン数で確定
                break

        # 所要時間の実測をログに残す（タイムアウト値・モデル入替の判断材料。設計書 §8.4
        # 「タイムアウト値は実測の p95 に余裕を載せて config で管理する」の入力データ）
        total_sec = done_chunk.get("total_duration", 0) / 1e9
        logger.info(
            "ollama 生成完了: model=%s total=%.1fs prompt_tokens=%s eval_tokens=%s",
            model, total_sec, done_chunk.get("prompt_eval_count"), done_chunk.get("eval_count"))
        return "".join(parts)
    finally:
        conn.close()


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
