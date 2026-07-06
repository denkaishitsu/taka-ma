"""qu-e Reviewer — Tier 2 コードレビューおよび file_audit 判定。

構築手順書: docs/procedures/07-sentinel.md Step 3 (Tier 2) / Step 5 (file_audit)
関連: 設計書 §8.12 / A1 §1〜§2
"""

import asyncio
import fcntl
import json
import os
import re

import httpx

# qu-e 単一モデルへの推論を直列化するためのプロセス間ロック（§4.2 推論の直列化）。
# file_audit（常駐プロセス）と Tier2 審査（review_cli の 1 ショット・別プロセス）が同一
# ollama モデルを同時に叩くと相互に遅延し双方が timeout→escalate に倒れるため、両プロセスが
# 同一パスを flock して 1 件ずつに直列化する。config 未指定時の既定。
DEFAULT_INFERENCE_LOCK = "/opt/taka-ma/data/qu-e-inference.lock"

# qu-e のローカル LLM（Qwen3.6-35B-A3B、thinking モデル）は応答本文を ```json フェンスで
# 包むことがある（実機検証で "rm -rf /" 審査時に再現・是正、非決定的＝プロンプトにより
# フェンス無しで返る場合もある）。ya-ta 側 ai_gateway.llm.extract_json と同一パターンだが、
# qu-e は MBP 単体で常駐し ya-ta（Mac mini 専用コンポーネント）を import できないため
# 同ロジックをここに複製する。
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """LLM 応答から JSON 本体を取り出す（ai_gateway.llm.extract_json と同じ2段構え）。

    (1) フェンス内 → (2) 最初の `{`/`[` から最後の `}`/`]` までの順に取り出す。
    開始フェンスのみで閉じフェンスを欠く等の不完全な出力でもフェンス正規表現だけでは
    救出できないため、括弧スキャンへのフォールバックを持たせる。どちらも取れなければ
    元テキストを返す（呼び出し側で json.loads が失敗し escalate フォールバックに落ちる）。
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


# file_audit 判定の有効値。ここに正規化できないものはすべて人間へ escalate（fail-closed）。
_VALID_AUDIT_DECISIONS = ("approve", "deny", "escalate")


def _escalate_audit_result(reason: str) -> dict:
    """判定不能・異常出力時の file_audit フォールバック（人間確認へ倒す fail-closed）。"""
    return {"decision": "escalate", "reason": reason, "confidence": 0.0, "diff_summary": ""}


def _normalize_audit_result(result) -> dict:
    """qu-e LLM の file_audit 判定を安全に正規化する（設計書 §8.12「fail-closed 原則」）。

    危険な変更を無音で承認扱いにしないため、少しでも不確かなら `escalate` に倒す:
    - dict でない（配列・文字列・スカラ）→ escalate
    - `decision` を trim + lower し、`approve` / `deny` / `escalate` 以外（大文字 `DENY`・
      `block`・キー欠落・非文字列）→ escalate
    - `reason` / `confidence` / `diff_summary` は安全な既定で補完
    さらに、返す dict は既知の 4 キーだけに限定し、`id` / `path` 等の監査固定キーを
    LLM 応答が持ち込んで後段で上書きする経路を断つ。
    """
    if not isinstance(result, dict):
        return _escalate_audit_result(
            f"qu-e 応答が JSON オブジェクトでない（{type(result).__name__}）")
    decision = result.get("decision")
    norm = decision.strip().lower() if isinstance(decision, str) else None
    if norm not in _VALID_AUDIT_DECISIONS:
        return _escalate_audit_result(f"qu-e 判定が不正（decision={decision!r}）")
    confidence = result.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        confidence = 0.0
    return {
        "decision": norm,
        "reason": result.get("reason") or "",
        "confidence": float(confidence),
        "diff_summary": result.get("diff_summary", ""),
    }


class QueReviewer:
    """qu-e のローカル LLM（ollama）に判定を委ねるレビューア。

    コマンド審査（Tier 2）・コードレビュー・file_audit 判定の 3 用途を持つ。いずれも
    プロンプトを組み立てて LLM に問い、JSON の判定を返す。LLM 接続不能・パース失敗は
    握り潰さず escalate（人間判断へ）に倒すことで、判定不能を「安全側」へ寄せる。
    """

    def __init__(self, model: str, ollama_host: str, prompts_dir: str,
                 inference_lock: str = DEFAULT_INFERENCE_LOCK):
        """レビューアを構築する。

        Args:
            model: 判定に使う ollama モデル名。
            ollama_host: ollama サーバの URL（/api/generate を叩く）。
            prompts_dir: プロンプト雛形の置き場。file_audit はテンプレ差し替え方式のため
                起動時に一度だけ読み込んでキャッシュする（判定のたびの I/O を避ける）。
            inference_lock: 推論直列化のプロセス間ロックファイルパス（§4.2）。file_audit と
                Tier2 の両プロセスで同一パスを指すことで、同一 ollama モデルへの同時推論を
                1 件ずつに直列化する。
        """
        self.model = model
        self.ollama_url = ollama_host
        self._inference_lock = inference_lock
        # ロックファイルの親ディレクトリを用意（初回 open で失敗しないように）。
        # ベア名（ディレクトリ成分なし）だと dirname="" で makedirs が FileNotFoundError に
        # なるため、その場合はカレント（"."）に倒す。
        os.makedirs(os.path.dirname(inference_lock) or ".", exist_ok=True)
        # file_audit のプロンプト雛形は毎回同じなので起動時に読み込んで保持する
        with open(os.path.join(prompts_dir, "file_audit.md")) as f:
            self._file_audit_template = f.read()

    async def review_command(self, command: str, context: dict) -> dict:
        """Tier 2 審査: コマンドの安全性を判定（設計書 §8.8）。

        Returns: {"decision": "approve"|"deny"|"escalate", "reason": "...", "risk_score": float}
        ollama 接続失敗 / タイムアウト / JSON パースエラー時は escalate（Tier 3 へフォールバック、安全側）。
        """
        prompt = f"""
Review the following command for security risks:

Command: {command}
Instance: {context.get('instance_id')}
Working Directory: {context.get('working_dir')}
Recent Activity: {context.get('recent_activity', 'N/A')}

Respond in JSON:
{{"decision": "approve|deny|escalate", "reason": "...", "risk_score": 0.0-1.0}}
"""
        try:
            response = await self._generate(prompt)
            return json.loads(_extract_json(response))
        except (json.JSONDecodeError, httpx.HTTPError) as e:
            return {"decision": "escalate",
                    "reason": f"qu-e 判定不能（{type(e).__name__}: {e}）",
                    "risk_score": 0.0}

    async def review_diff(self, diff: str, file_path: str) -> dict:
        """ファイル変更のコードレビュー。ollama 接続失敗 / JSON パースエラー時は escalate。"""
        prompt = f"""
Review this code change for:
1. Security vulnerabilities (injection, XSS, etc.)
2. Malicious code patterns
3. Destructive operations
4. Sensitive data exposure

File: {file_path}
Diff:
```
{diff}
```

Respond in JSON:
{{"decision": "approve|deny|escalate", "issues": [...], "severity": "low|medium|high|critical"}}
"""
        try:
            response = await self._generate(prompt)
            return json.loads(_extract_json(response))
        except (json.JSONDecodeError, httpx.HTTPError) as e:
            return {"decision": "escalate",
                    "reason": f"qu-e 判定不能（{type(e).__name__}: {e}）",
                    "issues": [],
                    "severity": "high"}

    async def review_file_audit(self, path: str, diff: str, command: str, status: str) -> dict:
        """file_audit 判定: ファイル変更の安全性を approve/deny/escalate で返す（A1 §1〜§2）。

        Returns:
            dict: {decision, reason, confidence, diff_summary}
            JSON パースエラー時は escalate fallback。
        """
        # 雛形のプレースホルダを実値で差し替える。欠損は安全な既定（diff 無し / 状態 none）に倒す
        prompt = (self._file_audit_template
                  .replace("{path}", path)
                  .replace("{diff}", diff or "(no diff)")
                  .replace("{command}", command or "")
                  .replace("{status}", status or "none"))
        try:
            response = await self._generate(prompt)
            result = json.loads(_extract_json(response))
        except Exception as e:
            # 判定不能は握り潰さず escalate に倒す（fail-closed）。JSONDecodeError・
            # httpx.HTTPError に加え、ollama 応答が "response" キーを欠く等の想定外例外
            # （KeyError など）も含めて捕捉し、監査を無音で素通りさせない。
            return _escalate_audit_result(f"qu-e 判定不能（{type(e).__name__}: {e}）")
        # 応答形状の正規化（非 dict・未知 decision・キー欠落は escalate に倒す）
        return _normalize_audit_result(result)

    async def _generate(self, prompt: str) -> str:
        """ollama にプロンプトを投げ、生成テキスト（応答本文）を返す。

        ストリーミングは使わず一括取得。呼び出し側が JSON としてパースする前提のため、
        ここでは応答の生文字列だけを返す。接続・タイムアウト系の失敗は httpx.HTTPError
        として送出され、各 review_* 側の escalate フォールバックで受ける。

        §4.2 推論の直列化: 同一 ollama モデルへの推論を、file_audit（常駐）と Tier2
        （review_cli の別プロセス）の間で 1 件ずつに直列化する。プロセス間 flock を
        取得してから HTTP を投げる。flock 取得（＝キュー待ち）はブロッキングのため
        `to_thread` で別スレッドに逃がし、イベントループを止めない。タイムアウト（60s）は
        ロック取得後に投げる HTTP に付くため、待ち時間を含まない実行開始起点で計測される。
        """
        lock_fd = await asyncio.to_thread(self._acquire_inference_lock)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False},
                    timeout=60,
                )
                # HTTP エラー応答（5xx 等）は "response" キーを欠くことがあり、そのまま
                # ["response"] すると KeyError で判定不能を握り潰す経路に落ちる。ここで
                # HTTPStatusError（httpx.HTTPError の一種）へ変換し、各 review_* の
                # escalate フォールバックで確実に受ける（fail-closed）。
                resp.raise_for_status()
                return resp.json()["response"]
        finally:
            await asyncio.to_thread(self._release_inference_lock, lock_fd)

    def _acquire_inference_lock(self) -> int:
        """推論直列化ロックを排他取得する（ブロッキング。to_thread から呼ぶ）。"""
        fd = os.open(self._inference_lock, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    @staticmethod
    def _release_inference_lock(fd: int) -> None:
        """推論直列化ロックを解放して fd を閉じる（to_thread から呼ぶ）。"""
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
