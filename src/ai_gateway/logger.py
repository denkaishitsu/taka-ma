"""判定ログ — 日付別 jsonl ファイルに記録。

構築手順書: docs/procedures/04-ai-gateway.md Step 7（判定ログ — 運用改善の基盤）
"""

import json
import datetime
import os

LOG_DIR = "/opt/taka-ma/logs"


class YaTaLogger:
    """ya-ta の execution × depth 判定を日付別 jsonl に追記するロガー。

    分類/分解時にモデルが下した生判定（execution・depth・信頼度・理由）を残す。後段の
    Phase 2（誤判定パターン抽出 → 分類プロンプト改善）と confidence 閾値の較正
    （設計書 §2.2）が振り返る一次データになる。
    """

    def __init__(self, log_dir: str = LOG_DIR):
        """出力先ディレクトリを受け取る（既定は本番ログディレクトリ）。"""
        self.log_dir = log_dir

    def _log_path(self) -> str:
        """当日分のログファイルパスを返す。

        日付別（ya-ta-decisions-YYYY-MM-DD.jsonl）に分けることで、後の retention
        rotation で古い日付ごとまとめて削除・集計できるようにしている。
        """
        today = datetime.date.today().isoformat()
        return os.path.join(self.log_dir, f"ya-ta-decisions-{today}.jsonl")

    def log_decision(self, task: str, execution: str, depth, model: str,
                     reason: str, confidence: float, actual_result: str = ""):
        """1 件の判定を当日ログに追記する。

        Args:
            task: 判定対象のタスク指示文。
            execution: 実行方式の生判定（inline / agent）。orchestrator の写像・昇格の前の値を残す。
            depth: 深さの生判定（shallow / deep / None＝省略）。
            model: 指定された :モデル名（無ければ空文字）。
            reason: モデルが挙げた判定理由。
            confidence: 判定の信頼度。閾値（sonnet 落下）判断にも対応する値。
            actual_result: 実行後に分かった実結果（任意。判定の答え合わせ用）。
        """
        # 1 判定 = 1 行（jsonl）。日本語の理由を保つため ensure_ascii=False で追記する。
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "task": task,
            "execution": execution,
            "depth": depth,
            "model": model,
            "reason": reason,
            "confidence": confidence,
            "actual_result": actual_result,
        }
        with open(self._log_path(), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_decompose_call(self, task: str, fallback: bool, reason: str = "",
                           subtasks: int = 0):
        """分解 1 呼び出し分の成否を当日ログに追記する（設計書 §8.4.1）。

        既存の log_decision はサブタスク単位（1 呼び出しで複数行）のため、呼び出し数を
        分母とする「分解フォールバック率」（§8.4.1 ローカル脳の換装判断基準）が集計
        できない。1 呼び出し = 1 行の本エントリを追加し、集計スクリプト
        （decision_stats.py）の分母・分子をここから機械算出する。

        Args:
            task: 分解対象の指示文（先頭 200 字に切り詰めて記録）。
            fallback: フォールバック（パース不能・構造不正・実行失敗）が発動したか。
            reason: fallback=True のときの失敗種別。
            subtasks: 確定したサブタスク数（fallback 時は 1）。
        """
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "kind": "decompose_call",
            "task": task[:200],
            "fallback": fallback,
            "reason": reason,
            "subtasks": subtasks,
        }
        with open(self._log_path(), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_contract(self, origin, attempts: list[dict],
                     backend: str | None = None, degraded: bool = False):
        """契約化 1 回分の試行列を当日ログに追記する（設計書 §8.4.1）。

        契約化バックエンド換装判断（どのモデルで何が不合格だったか・縮退の頻度）の
        一次データ。判定ログと同じファイルに kind で区別して混載する（日付 rotation を
        二重に持たない）。

        Args:
            origin: 契約を確定したモデル（"local"=ローカル / モデル名=worker CLI /
                None=不成立）。
            attempts: 試行列 [{"model": 名前, "problems": 不合格理由リスト}, ...]。
            backend: 確定（または最終試行）のバックエンド（"worker_cli" / "local"）。
            degraded: CLI 呼び出し失敗によるローカル縮退が起きたか（§8.4）。
        """
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "kind": "contract",
            "origin": origin,
            "backend": backend,
            "degraded": degraded,
            "attempts": attempts,
        }
        with open(self._log_path(), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
