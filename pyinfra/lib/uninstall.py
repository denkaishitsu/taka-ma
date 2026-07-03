"""アンインストール・ランナー — install-manifest.jsonl を逆順(LIFO)再生して撤去。

設計書 §6.5 準拠。マニフェストを seq 降順で読み、各レコードの teardown を実行する。

安全方針:
- 既定は **dry-run**（計画表示のみ）。実際に撤去するには `--apply` を付ける。
- 常駐サービスの停止を最優先（teardown の seq 順で自然に達成。サービス登録は
  構築の後半なので逆順では先に解除される）。
- 共有資源・外部資産（teardown op=="skip"）は撤去しない。
- マニフェスト本体は data ディレクトリ配下にあるため、--apply 時はまず /tmp へ
  退避してから撤去を進める（data 削除で履歴を失わないため）。

使い方（対象機上）:
    /opt/taka-ma-env/bin/python /opt/taka-ma/lib/uninstall.py            # dry-run
    /opt/taka-ma-env/bin/python /opt/taka-ma/lib/uninstall.py --apply    # 実撤去
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

MANIFEST_PATH = "/opt/taka-ma/data/install-manifest.jsonl"


def load_records(path: str) -> list:
    """JSONL を読み、レコードのリストを返す。空行は無視。"""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _log(apply: bool, action: str):
    print(("APPLY " if apply else "DRY   ") + action)


def _pkg_name(spec: str) -> str:
    """pip パッケージ指定子からバージョン部を除いた名前を返す。

    例: "slack-bolt>=1.20,<2" -> "slack-bolt"
    """
    for sep in ("<", ">", "=", "!", "~", " ", "["):
        idx = spec.find(sep)
        if idx != -1:
            spec = spec[:idx]
    return spec.strip()


def teardown_one(rec: dict, apply: bool):
    """1 レコードの teardown を実行（または計画表示）。"""
    t = rec.get("teardown", {})
    op = t.get("op")
    seq = rec.get("seq")

    if op == "skip":
        print(f"SKIP  seq={seq} {rec.get('operation','')} "
              f"({t.get('reason','')})")
        return

    if op == "files.directory" and t.get("present") is False:
        path = t["path"]
        _log(apply, f"rm -rf {path}  (seq={seq})")
        if apply:
            shutil.rmtree(path, ignore_errors=True)
        return

    if op == "files.file" and t.get("present") is False:
        path = os.path.expanduser(t["path"])
        _log(apply, f"rm -f {path}  (seq={seq})")
        if apply and os.path.exists(path):
            os.remove(path)
        return

    if op == "brew.service" and t.get("running") is False:
        _log(apply, f"brew services stop {t['service']}  (seq={seq})")
        if apply:
            subprocess.run(["brew", "services", "stop", t["service"]],
                           check=False)
        return

    if op == "pip.uninstall":
        # バージョン指定子（>=,<,== 等）を除いたパッケージ名で uninstall
        names = [_pkg_name(p) for p in t.get("packages", [])]
        pip = os.path.join(t.get("virtualenv", "/opt/taka-ma-env"), "bin", "pip")
        _log(apply, f"{pip} uninstall -y {' '.join(names)}  (seq={seq})")
        if apply and names:
            subprocess.run([pip, "uninstall", "-y", *names], check=False)
        return

    if op == "npm.uninstall":
        g = ["-g"] if t.get("global") else []
        _log(apply, f"npm uninstall {' '.join(g)} {t['package']}  (seq={seq})")
        if apply:
            subprocess.run(["npm", "uninstall", *g, t["package"]], check=False)
        return

    # 以降は他デプロイ横展開で生じる teardown（前方互換のため対応）
    if op == "launchctl.bootout":
        label = t["label"]
        _log(apply, f"launchctl bootout + rm plist: {label}  (seq={seq})")
        if apply:
            subprocess.run(
                ["launchctl", "bootout",
                 f"gui/{os.getuid()}/{label}"], check=False)
            plist = os.path.expanduser(
                f"~/Library/LaunchAgents/{label}.plist")
            if os.path.exists(plist):
                os.remove(plist)
        return

    if op == "ollama.rm":
        _log(apply, f"ollama rm {t['model']}  (seq={seq})")
        if apply:
            subprocess.run(["ollama", "rm", t["model"]], check=False)
        return

    print(f"WARN  未対応 teardown op={op!r} seq={seq}（手動対応が必要）")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="実際に撤去する（既定は dry-run）")
    parser.add_argument("--manifest", default=MANIFEST_PATH,
                        help=f"マニフェストのパス（既定: {MANIFEST_PATH}）")
    args = parser.parse_args(argv)

    records = load_records(args.manifest)
    if not records:
        print(f"マニフェストが空か存在しません: {args.manifest}")
        return 0

    # seq 降順（LIFO）。
    records.sort(key=lambda r: r.get("seq", 0), reverse=True)

    if args.apply:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"/tmp/install-manifest.backup-{ts}.jsonl"
        shutil.copy(args.manifest, backup)
        print(f"manifest backup → {backup}")

    mode = "APPLY（実撤去）" if args.apply else "DRY-RUN（計画のみ）"
    print(f"=== uninstall {mode}: {len(records)} steps（seq 降順） ===")
    for rec in records:
        teardown_one(rec, args.apply)
    print("=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
