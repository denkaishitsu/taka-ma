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
