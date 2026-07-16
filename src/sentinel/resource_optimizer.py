"""qu-e Resource Optimizer — worker LLM（heavy）並行数の動的調整（設計書 §4.2 / §8.14）。

メモリ使用率から heavy worker の推奨並行数を算出する。算出値は sa-ru へ SSH push され
（§8.14）、sa-ru 側で `max_heavy_instances` を動的更新する。
"""

import psutil


class ResourceOptimizer:
    """メモリ使用率から heavy worker の推奨並行数を導く（設計書 §4.2 / §8.14）。

    メモリが逼迫するほど並行数を絞り、余裕があれば上限まで許す。算出値は qu-e から
    sa-ru へ通知され、sa-ru 側で実際の `max_heavy_instances` 更新に使われる。
    判定はあくまで推奨であり、上限（max_instances）を超える値は返さない。
    """

    def __init__(self, config: dict):
        """並行数上限とスケール判定のメモリしきい値を設定から読む。

        Args:
            config: resource_optimization セクション。max_heavy_instances（上限）と
                scale_down / scale_up のメモリ使用率しきい値（パーセント）を持つ。
        """
        # 3 値とも qu-e.yaml の resource_optimization ブロックを唯一の源にする
        # （コード既定値なし。欠落は起動時に KeyError で即落とし診断位置を揃える）。
        # heavy worker 並行数の上限（権威値。sa-ru はこの範囲内で推奨値を受け取る）
        self.max_instances = config["max_heavy_instances"]
        # この使用率以上なら並行数を 2 段下げる（逼迫時の積極的な縮退）
        self.scale_down_threshold = config["scale_down_memory_threshold"]
        # この使用率以上なら並行数を 1 段下げる（余裕が減ってきた段階の控えめな縮退）
        self.scale_up_threshold = config["scale_up_memory_threshold"]

    def recommended_heavy_instances(self) -> int:
        """現在のメモリ使用率に基づき推奨 heavy worker 並行数を返す。

        使用率が高いほど段階的に並行数を減らす。減らしても最低 1 は確保し
        （worker 全停止を避ける）、余裕があれば上限値をそのまま返す。
        """
        mem = psutil.virtual_memory()
        # 逼迫度に応じて上限から段階的に差し引く。最低 1 は残して全停止を防ぐ
        if mem.percent >= self.scale_down_threshold:
            return max(1, self.max_instances - 2)
        elif mem.percent >= self.scale_up_threshold:
            return max(1, self.max_instances - 1)
        else:
            return self.max_instances

    def notify_payload(self, memory_warning: float, memory_critical: float) -> dict:
        """§8.14 リソース最適化通知の payload を生成する。

        推奨並行数に加え、現在のメモリ使用率と深刻度 level を同梱する。level は
        health_check と同じメモリしきい値（warning / critical 境界）で分類するため、
        ヘルスチェックと通知で深刻度の基準が揃う。
        """
        mem = psutil.virtual_memory()
        # health_check と同じ基準で深刻度を分類（通知の読み手が状態を一目で把握できる）
        if mem.percent >= memory_critical:
            level = "critical"
        elif mem.percent >= memory_warning:
            level = "warning"
        else:
            level = "normal"
        return {
            "recommended_heavy_instances": self.recommended_heavy_instances(),
            "memory_usage": mem.percent,
            "level": level,
        }
