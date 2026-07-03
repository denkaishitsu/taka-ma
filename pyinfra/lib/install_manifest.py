"""インストール来歴マニフェスト — 構築の各ステップを jsonl に記録。

設計書 §6.5 準拠。ターゲットホストの /opt/taka-ma/data/install-manifest.jsonl へ
1 行 = 1 ステップで記録する。記録は「構築する AI」が pyinfra(自動)・手動の
両方について行い、アンインストールはこのマニフェストを seq 降順(LIFO)で再生する。

冪等性: pyinfra は冪等で再デプロイが常用されるため、本マニフェストも
**現在のインストール状態の正本**として upsert で管理する。自然キー
(host, component, operation) が一致する既存レコードは seq を据え置いたまま
更新し、重複追記しない。新規キーのみ採番（既存最大 seq + 1）して追加する。

機微情報（鍵パス・トークン・API キー値）は記録しない（種別・宛先のみ）。
"""
import datetime
import json
import os

MANIFEST_PATH = "/opt/taka-ma/data/install-manifest.jsonl"


def _read_all(path: str) -> list:
    """JSONL を全件読み込む。ファイル無し/空行は無視。"""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _write_all(path: str, records: list):
    """全件を JSONL でアトミックに書き戻す（house style: ensure_ascii=False）。

    各 record() 呼び出しで全件 read→write するため、書き込み途中で中断すると
    マニフェストが壊れる。同一ディレクトリの一時ファイルへ書いてから
    os.replace で原子的に差し替え、常に整合した状態を保つ。
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _key(rec: dict) -> tuple:
    return (rec.get("host"), rec.get("component"), rec.get("operation"))


def record(component: str, operation: str, target: str, teardown: dict,
           host: str, source: str = "pyinfra", status: str = "completed",
           path: str = MANIFEST_PATH) -> int:
    """1 ステップを upsert し、その seq を返す。

    teardown は撤去に必要な対称オペレーション
    （例: {"op": "files.directory", "path": "...", "present": False}、
    {"op": "skip", "reason": "..."}）。

    自然キー (host, component, operation) が既存と一致すれば seq 据え置きで
    更新（重複させない）。新規なら既存最大 seq + 1 を採番して追加する。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    records = _read_all(path)
    ts = datetime.datetime.now().astimezone().isoformat()
    key = (host, component, operation)

    for r in records:
        if _key(r) == key:
            # 既存リソースの再適用: seq は据え置き、内容を更新。
            r.update({
                "ts": ts, "source": source, "target": target,
                "teardown": teardown, "status": status,
            })
            _write_all(path, records)
            return r["seq"]

    seq = max((r.get("seq", 0) for r in records), default=0) + 1
    records.append({
        "seq": seq,
        "ts": ts,
        "host": host,
        "source": source,
        "component": component,
        "operation": operation,
        "target": target,
        "teardown": teardown,
        "status": status,
    })
    _write_all(path, records)
    return seq
