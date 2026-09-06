"""SSH 呼び出しの上限とハング診断の回帰テスト（設計書 §8.5・ADR 0002・#taka-ma/163.1）。

検証する振る舞い:
- timeout 無しの ssh 呼び出しが src 配下に存在しない: `subprocess.run / call / check_call /
  check_output` の第 1 引数がリスト literal で先頭が "ssh" のとき、`timeout=` キーワードを
  必ず持つ（2026-09-04 は timeout 無し ssh がスレッドプールを詰まらせた疑い）。
  AST で判定するため、コメントや文字列中の "ssh" には反応しない。
- クラスタ SSH client 設定テンプレートに接続上限 4 項目が入っている。
- install_hang_diagnostics() 登録後に SIGUSR1 を送ると、全スレッドのスタックが
  指定ストリームへ出る（分離プロセスで実測。faulthandler は C レベルのため
  イベントループ非依存であることの確認）。
"""

import ast
import pathlib
import subprocess
import sys
import textwrap

_HERE = pathlib.Path(__file__).resolve().parent
_SRC = _HERE.parent.parent
_REPO = _SRC.parent

_SSH_FUNCS = {"run", "call", "check_call", "check_output"}


def _is_ssh_argv(node: ast.AST) -> bool:
    """第 1 引数が ["ssh", ...] のリスト literal か。"""
    return (isinstance(node, ast.List) and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "ssh")


def _subprocess_func_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "subprocess":
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _find_ssh_calls_without_timeout(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _subprocess_func_name(node) not in _SSH_FUNCS:
            continue
        if not node.args or not _is_ssh_argv(node.args[0]):
            continue
        if not any(k.arg == "timeout" for k in node.keywords):
            bad.append(node.lineno)
    return bad


def _source_files():
    for p in _SRC.rglob("*.py"):
        if "/tests/" in str(p) or p.name.startswith("test_"):
            continue
        yield p


def test_no_ssh_subprocess_call_without_timeout():
    offenders = {}
    for p in _source_files():
        lines = _find_ssh_calls_without_timeout(p)
        if lines:
            offenders[str(p.relative_to(_SRC))] = lines
    assert not offenders, f"timeout 無しの ssh 呼び出し: {offenders}"


def test_detector_catches_a_timeout_less_ssh_call(tmp_path):
    """検出器自身の健全性: timeout 無しを拾い、timeout 有りと文字列中の ssh を拾わない。"""
    sample = tmp_path / "s.py"
    sample.write_text(textwrap.dedent('''
        import subprocess
        subprocess.run(["ssh", "mbp", "true"])                 # NG
        subprocess.run(["ssh", "mbp", "true"], timeout=5)      # OK
        subprocess.check_output(["ssh", "mbp", "x"])           # NG
        subprocess.run(["ls", "ssh"])                          # OK（先頭が ssh でない）
        x = "subprocess.run([\\"ssh\\"])"                        # OK（文字列）
    '''))
    assert _find_ssh_calls_without_timeout(sample) == [3, 5]


def test_cluster_ssh_config_has_connection_limits():
    text = (_REPO / "pyinfra" / "templates" / "ssh_config.j2").read_text()
    for key in ("ConnectTimeout 10", "ServerAliveInterval 15",
                "ServerAliveCountMax 2", "BatchMode yes"):
        assert key in text, key


def test_sigusr1_dumps_all_thread_stacks():
    """分離プロセスで登録→SIGUSR1→出力を実測する（faulthandler の C レベル動作）。"""
    diag = _SRC / "orchestrator" / "diagnostics.py"
    prog = textwrap.dedent(f'''
        import importlib.util, os, signal, sys, threading, time
        spec = importlib.util.spec_from_file_location("diag", {str(diag)!r})
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        sig = m.install_hang_diagnostics()
        t = threading.Thread(target=lambda: time.sleep(30), name="stuck-worker", daemon=True)
        t.start()
        os.kill(os.getpid(), sig)
        time.sleep(0.5)
        sys.stderr.flush()
    ''')
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert "Current thread" in r.stderr, r.stderr
    # 待機中のワーカースレッドも含まれる（all_threads=True）: メイン以外のスレッド見出しと、
    # その待機フレーム（time.sleep は C 関数のため、呼び出し元の <lambda> が最深フレーム）
    assert r.stderr.lower().count("thread 0x") >= 2, r.stderr
    assert "<lambda>" in r.stderr, r.stderr
