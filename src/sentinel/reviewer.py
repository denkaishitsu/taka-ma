"""qu-e Reviewer — Tier 2 コードレビューおよび file_audit 判定。

構築手順書: docs/procedures/07-sentinel.md Step 3 (Tier 2) / Step 5 (file_audit)
関連: 設計書 §8.12 / A1 §1〜§2
"""

import json
import os
import re

import httpx

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


class QueReviewer:
    """qu-e のローカル LLM（ollama）に判定を委ねるレビューア。

    コマンド審査（Tier 2）・コードレビュー・file_audit 判定の 3 用途を持つ。いずれも
    プロンプトを組み立てて LLM に問い、JSON の判定を返す。LLM 接続不能・パース失敗は
    握り潰さず escalate（人間判断へ）に倒すことで、判定不能を「安全側」へ寄せる。
    """

    def __init__(self, model: str, ollama_host: str, prompts_dir: str):
        """レビューアを構築する。

        Args:
            model: 判定に使う ollama モデル名。
            ollama_host: ollama サーバの URL（/api/generate を叩く）。
            prompts_dir: プロンプト雛形の置き場。file_audit はテンプレ差し替え方式のため
                起動時に一度だけ読み込んでキャッシュする（判定のたびの I/O を避ける）。
        """
        self.model = model
        self.ollama_url = ollama_host
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
            return json.loads(_extract_json(response))
        except (json.JSONDecodeError, httpx.HTTPError) as e:
            # 判定不能（LLM 不達・壊れた JSON）は approve せず escalate に倒す
            return {
                "decision": "escalate",
                "reason": f"qu-e 判定不能（{type(e).__name__}: {e}）",
                "confidence": 0.0,
                "diff_summary": "",
            }

    async def _generate(self, prompt: str) -> str:
        """ollama にプロンプトを投げ、生成テキスト（応答本文）を返す。

        ストリーミングは使わず一括取得。呼び出し側が JSON としてパースする前提のため、
        ここでは応答の生文字列だけを返す。接続・タイムアウト系の失敗は httpx.HTTPError
        として送出され、各 review_* 側の escalate フォールバックで受ける。
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.ollama_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=60,
            )
            return resp.json()["response"]
