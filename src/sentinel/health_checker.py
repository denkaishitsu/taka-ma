"""qu-e Health Checker — CPU/メモリ/ディスク/ネットワーク監視（設計書 §3 / §8.12）"""

import subprocess

import psutil


class HealthChecker:
    """qu-e マシンと sa-ru への到達性の健全性を一括点検する（設計書 §3 / §8.12）。

    CPU・メモリ・ディスク・ネットワークの 4 観点を個別に評価し、最も悪い状態へ
    集約した overall を含む結果を返す。各項目のしきい値は設定（thresholds）で与える。
    """

    def __init__(self, thresholds: dict, mac_mini_host: str):
        """しきい値と ping 対象ホストを受け取る。

        Args:
            thresholds: cpu_warning / memory_warning / memory_critical / disk_warning 等の
                各しきい値（パーセント）を持つ設定 dict。
            mac_mini_host: 到達性確認の ping 先となる sa-ru（Mac mini）のホスト名/IP。
        """
        self.thresholds = thresholds
        self.mac_mini_host = mac_mini_host

    def check_all(self) -> dict:
        """4 観点を点検し、各結果と最悪状態へ集約した overall をまとめて返す。

        critical が 1 つでもあれば overall=critical、無ければ warning の有無で warning、
        いずれも無ければ healthy。呼び出し側はこの overall で警告要否を判断する。
        """
        cpu = self._check_cpu()
        memory = self._check_memory()
        disk = self._check_disk()
        network = self._check_network()

        # 4 項目のうち最も悪い状態を全体状態とする（critical > warning > healthy）
        statuses = [cpu["status"], memory["status"], disk["status"], network["status"]]
        if "critical" in statuses:
            overall = "critical"
        elif "warning" in statuses:
            overall = "warning"
        else:
            overall = "healthy"

        return {
            "cpu": cpu,
            "memory": memory,
            "disk": disk,
            "network": network,
            "overall": overall,
        }

    def _check_memory(self) -> dict:
        """物理メモリ使用率を warning / critical の二段しきい値で評価する。

        warning を超えたら warning、さらに critical も超えたら critical へ昇格する
        （critical は warning を包含する関係のため、後段の判定で上書きする）。
        総量・使用量は GB 表示用に丸める。
        """
        mem = psutil.virtual_memory()
        status = "healthy"
        if mem.percent > self.thresholds["memory_warning"]:
            status = "warning"
        if mem.percent > self.thresholds["memory_critical"]:
            status = "critical"
        return {
            "total_gb": round(mem.total / 1e9, 1),
            "used_gb": round(mem.used / 1e9, 1),
            "percent": mem.percent,
            "status": status,
        }

    def _check_cpu(self) -> dict:
        """CPU 使用率を 1 秒サンプリングし、しきい値超過で warning とする。

        interval=1 は瞬間値のブレを避けるため 1 秒間の平均を取る指定。CPU は一過性で
        高騰しうるため critical までは設けず warning 止まりにしている。
        """
        percent = psutil.cpu_percent(interval=1)
        threshold = self.thresholds.get("cpu_warning", 90)
        return {"percent": percent, "status": "healthy" if percent < threshold else "warning"}

    def _check_disk(self) -> dict:
        """ルートパーティション（/）の使用率を点検し、しきい値超過で warning とする。

        空き容量逼迫はログ・監査 jsonl の書き込み失敗に直結するため監視する。
        """
        disk = psutil.disk_usage("/")
        threshold = self.thresholds.get("disk_warning", 90)
        return {
            "total_gb": round(disk.total / 1e9, 1),
            "free_gb": round(disk.free / 1e9, 1),
            "percent": round(disk.percent, 1),
            "status": "healthy" if disk.percent < threshold else "warning",
        }

    def _check_network(self) -> dict:
        """Mac mini への SSH 到達性を確認（10GbE 直結 / Tailscale VPN いずれか経由）。

        ping ではなく ssh で確認する。mac_mini_host は file_auditor.py の SSH push と
        共有する ~/.ssh/config の Host エイリアス（例: "mac-mini"）であり、ping は
        ~/.ssh/config を解釈しないため名前解決できない（実機で "cannot resolve" を確認・
        是正）。監視したいのは「監査アラートの SSH push が成立するか」そのものなので、
        実際に使う経路と同じ ssh コマンドで到達性を測るのが正確。
        """
        try:
            # BatchMode=yes: パスフレーズ入力を待たず鍵認証失敗なら即座に失敗させる。
            # ConnectTimeout=2: 2 秒で諦める。subprocess 全体にも 5 秒の保険を掛ける
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
                 self.mac_mini_host, "true"],
                capture_output=True, timeout=5,
            )
            # 接続・認証・コマンド実行まで成功=healthy、失敗（鍵/権限等）は warning
            status = "healthy" if result.returncode == 0 else "warning"
            return {"host": self.mac_mini_host, "status": status}
        except subprocess.TimeoutExpired:
            # ssh コマンド自体が返ってこない＝経路断とみなし critical
            return {"host": self.mac_mini_host, "status": "critical", "error": "ssh timeout"}
        except Exception as e:
            # ssh 不在等の予期しない失敗も到達不能扱いで critical
            return {"host": self.mac_mini_host, "status": "critical", "error": str(e)}
