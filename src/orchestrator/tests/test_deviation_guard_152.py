"""worker 裁量逸脱の抑止（§8.10f 拡張）の検証。

検証する振る舞い（2026-08-22/24 実測の逸脱 2 型の構造的再発防止）:
- 型 A（大量追記）: acceptance `diff_limit` — HEAD コミットの numstat 実測が上限超過なら未達。
  実逸脱（一行指示に全面書き下ろし・当時の pushed/head_touches は素通し）を再現データで検出
- 型 B（環境改変）: decide デーモンの既定 deny — git init 等をタスク別規則の有無にかかわらず
  deny し、契約 directive 由来の allow_env に載るものだけ通す

decide_daemon.py はディレクトリ名にハイフンを含むためファイル直ロードする。
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types

_HERE = os.path.dirname(__file__)
_SRC = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator import Orchestrator  # noqa: E402
from orchestrator import contract as contract_rules  # noqa: E402
from orchestrator.grounding import GroundingVerifier  # noqa: E402

_DAEMON_PATH = os.path.join(_SRC, "approval-pipeline", "decide_daemon.py")


def _load_decide_daemon():
    spec = importlib.util.spec_from_file_location("decide_daemon_152", _DAEMON_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


decide_daemon = _load_decide_daemon()


# ── 契約両端: 既定 deny リストは contract.py と decide_daemon.py で同一 ──

def test_env_mutation_list_identical_both_ends():
    """producer（sa-ru の allow 抽出）⇄ consumer（decide の deny）のリスト完全一致。"""
    assert tuple(contract_rules.ENV_MUTATION_COMMANDS) == decide_daemon._ENV_MUTATION_DENY


# ── contract: diff_limit の検証・env_mutation_allows ──

def _validate_acceptance(acc):
    raw = {"directive": None, "constraints": [], "acceptance": acc,
           "workspace": None, "needs_repo": True}
    return contract_rules.validate_contract(raw, "一行だけ追記しろ")


def test_diff_limit_accepted_and_int_normalized():
    """diff_limit は max_lines（int または数字文字列→int 正規化）＋任意 path を受理する。"""
    contract, problems = _validate_acceptance(
        [{"kind": "diff_limit", "params": {"max_lines": 3, "path": "README.md"}}])
    assert problems == [] and contract["acceptance"][0]["params"]["max_lines"] == 3
    contract, problems = _validate_acceptance(
        [{"kind": "diff_limit", "params": {"max_lines": "5"}}])
    assert problems == [] and contract["acceptance"][0]["params"]["max_lines"] == 5


def test_diff_limit_rejects_bad_max_lines():
    """0 以下・bool・非数値・欠落・上限超は逸脱として fail-closed。"""
    for params in [{"max_lines": 0}, {"max_lines": -1}, {"max_lines": True},
                   {"max_lines": "abc"}, {}, {"max_lines": 10**9},
                   {"max_lines": "²"}]:  # isdigit=True だが int() 不可の上付き数字
        contract, problems = _validate_acceptance(
            [{"kind": "diff_limit", "params": params}])
        assert contract is None and problems, params


def test_diff_limit_is_repo_kind():
    """diff_limit は HEAD を見るリポジトリ系検査（needs_repo の機械補助対象）。"""
    assert "diff_limit" in contract_rules.REPO_KINDS


def test_env_mutation_allows_verbatim_only():
    """directive に含まれる環境改変コマンドだけを返す（大文字小文字・空白差は吸収）。"""
    assert contract_rules.env_mutation_allows(
        "git init して git  remote add origin x を実行しろ") == \
        ["git init", "git remote add"]
    assert contract_rules.env_mutation_allows("GIT INIT しろ") == ["git init"]
    assert contract_rules.env_mutation_allows("README を追記して push しろ") == []
    assert contract_rules.env_mutation_allows(None) == []
    assert contract_rules.env_mutation_allows("") == []


# ── 量指定既定（apply_default_acceptance の diff_limit 自動付与） ──

def _defaults(summary, acceptance=None):
    contract = {"directive": None, "constraints": [],
                "acceptance": list(acceptance or []), "workspace": None}
    return contract_rules.apply_default_acceptance(contract, summary)["acceptance"]


def test_default_diff_limit_on_explicit_line_count():
    """実逸脱の逐語「一行だけ追記しろ」に diff_limit(max_lines=3) が既定付与される。"""
    acc = _defaults("README に一行だけ追記しろ。内容は任せる")
    assert {"kind": "diff_limit", "params": {"max_lines": 3}} in acc
    acc = _defaults("CHANGELOG に 3 行追記して")
    assert {"kind": "diff_limit", "params": {"max_lines": 5}} in acc


def test_default_diff_limit_not_on_position_or_newline():
    """「N 行目」（位置参照）・「改行」（文字の話）・量指定なしには付与しない。"""
    for summary in ["README の 10 行目を修正して", "末尾に改行を追加して",
                    "README を追記して push しろ", "100 行追記して"]:
        acc = _defaults(summary)
        assert not any(a.get("kind") == "diff_limit" for a in acc), summary


def test_default_diff_limit_does_not_override_brain():
    """脳が立てた diff_limit があれば既定付与しない（上書き・重複禁止）。"""
    brain = [{"kind": "diff_limit", "params": {"max_lines": 3, "path": "README.md"}}]
    acc = _defaults("一行だけ追記しろ", acceptance=brain)
    assert [a for a in acc if a.get("kind") == "diff_limit"] == brain


# ── grounding: diff_limit の実測判定（実 git リポジトリで再現） ──

def _local_probe(command, timeout=30):
    r = subprocess.run(command, shell=True, capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def _make_repo():
    ws = tempfile.mkdtemp(prefix="ws152-")
    def git(*args):
        subprocess.run(["git", "-C", ws, *args], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    with open(os.path.join(ws, "README.md"), "w") as f:
        f.write("# title\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return ws, git


def test_diff_limit_pass_on_single_line_append():
    """一行追記（指示どおり）は max_lines=3 で PASS。"""
    ws, git = _make_repo()
    with open(os.path.join(ws, "README.md"), "a") as f:
        f.write("E2E test line\n")
    git("add", "-A"); git("commit", "-q", "-m", "append one line")
    report = GroundingVerifier(_local_probe).verify_acceptance(
        ws, [{"kind": "diff_limit", "params": {"max_lines": 3, "path": "README.md"}}])
    assert report.ok, report.text


def test_diff_limit_fails_on_mass_rewrite():
    """実逸脱の再現: 一行指示に対する全面書き下ろし（150 行）は未達に落ちる。"""
    ws, git = _make_repo()
    with open(os.path.join(ws, "README.md"), "w") as f:
        f.write("\n".join(f"line {i}" for i in range(150)) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "comprehensive rewrite")
    report = GroundingVerifier(_local_probe).verify_acceptance(
        ws, [{"kind": "diff_limit", "params": {"max_lines": 3, "path": "README.md"}}])
    assert not report.ok
    assert "diff_limit 未達" in report.note
    assert "acceptance_failed:diff_limit" in report.cause


def test_diff_limit_path_filter_counts_only_target():
    """path 指定時は当該ファイルの変更行数のみを数える（他ファイルの大変更は対象外）。"""
    ws, git = _make_repo()
    with open(os.path.join(ws, "README.md"), "a") as f:
        f.write("one line\n")
    with open(os.path.join(ws, "other.txt"), "w") as f:
        f.write("\n".join("x" for _ in range(200)) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "mixed")
    verifier = GroundingVerifier(_local_probe)
    report = verifier.verify_acceptance(
        ws, [{"kind": "diff_limit", "params": {"max_lines": 3, "path": "README.md"}}])
    assert report.ok, report.text
    # path 無指定なら全ファイル合計で未達
    report = verifier.verify_acceptance(
        ws, [{"kind": "diff_limit", "params": {"max_lines": 3}}])
    assert not report.ok


def test_diff_limit_absolute_path_normalized_to_workspace_relative():
    """脳が絶対パスで path を出しても（実機 E2E 2026-08-26 実測）、workspace 配下なら
    相対へ正規化して数える — 相対名との不一致で常に PASS になる空洞化を塞ぐ。"""
    ws, git = _make_repo()
    with open(os.path.join(ws, "README.md"), "w") as f:
        f.write("\n".join(f"line {i}" for i in range(150)) + "\n")
    git("add", "-A"); git("commit", "-q", "-m", "mass rewrite")
    report = GroundingVerifier(_local_probe).verify_acceptance(
        ws, [{"kind": "diff_limit",
              "params": {"max_lines": 3, "path": f"{ws}/README.md"}}])
    assert not report.ok and "diff_limit 未達" in report.note


def test_path_outside_workspace_fails_closed():
    """workspace 外の絶対パスは検査 FAIL（空振り PASS にしない・fail-closed）。"""
    ws, git = _make_repo()
    report = GroundingVerifier(_local_probe).verify_acceptance(
        ws, [{"kind": "file", "params": {"path": "/etc/hosts"}}])
    assert not report.ok and "workspace 外の絶対パス" in report.note


# ── decide デーモン: 環境改変コマンドの既定 deny ──

def _pending(command, tool="Bash"):
    return types.SimpleNamespace(tool_name=tool, tool_input={"command": command})


def _deny(monkeypatch, tmp, command, task_id="758daa02-e7f5-4f8f-af7d-8b0c1481f15c",
          rule=None, tool="Bash"):
    monkeypatch.setattr(decide_daemon, "_TASK_DENY_DIR", tmp)
    if rule is not None:
        with open(os.path.join(tmp, f"{task_id}.json"), "w") as f:
            json.dump(rule, f)
    return decide_daemon.DecideDaemon._task_deny_reason(task_id, _pending(command, tool))


def test_default_deny_git_init_without_rule_file(monkeypatch):
    """実逸脱の再現: タスク別規則が無くても git init は既定 deny（8/24 の git init を止める）。"""
    tmp = tempfile.mkdtemp()
    reason = _deny(monkeypatch, tmp, "cd /Users/x/Downloads && git init")
    assert reason and "既定 deny" in reason and "git init" in reason


def test_default_deny_all_env_mutation_commands(monkeypatch):
    """既定 deny リストの全コマンドが規則なしで deny される。"""
    tmp = tempfile.mkdtemp()
    for cmd in decide_daemon._ENV_MUTATION_DENY:
        assert _deny(monkeypatch, tmp, f"{cmd} something"), cmd


def test_allow_env_permits_only_listed_command(monkeypatch):
    """契約 directive 由来の allow_env に載るコマンドだけ通し、他の環境改変は deny のまま。"""
    tmp = tempfile.mkdtemp()
    rule = {"patterns": [], "sources": [], "allow_env": ["git init"]}
    assert _deny(monkeypatch, tmp, "git init", rule=rule) is None
    assert _deny(monkeypatch, tmp, "git remote add origin x", rule=rule)


def test_default_deny_applies_even_with_invalid_task_id(monkeypatch):
    """task_id 不正（規則ファイルを引けない）でも既定 deny は消えない（非対称を作らない）。"""
    tmp = tempfile.mkdtemp()
    assert _deny(monkeypatch, tmp, "git init", task_id="../etc")


def test_default_deny_normalizes_whitespace(monkeypatch):
    """空白差（連続スペース・タブ）で既定 deny を素通しできない。"""
    tmp = tempfile.mkdtemp()
    assert _deny(monkeypatch, tmp, "git   init")
    assert _deny(monkeypatch, tmp, "git\tremote\tadd origin x")


def test_non_dict_rule_file_treated_as_no_rule(monkeypatch):
    """規則ファイルが JSON 配列等（object でない）なら破損と同じ「規則なし」扱い —
    通常コマンドを全 deny に化けさせず、既定 deny は効いたまま。"""
    tmp = tempfile.mkdtemp()
    rule_path = os.path.join(tmp, "758daa02-e7f5-4f8f-af7d-8b0c1481f15c.json")
    with open(rule_path, "w") as f:
        f.write("[1, 2]")
    monkeypatch.setattr(decide_daemon, "_TASK_DENY_DIR", tmp)
    pending_ok = _pending("git status")
    assert decide_daemon.DecideDaemon._task_deny_reason(
        "758daa02-e7f5-4f8f-af7d-8b0c1481f15c", pending_ok) is None
    assert decide_daemon.DecideDaemon._task_deny_reason(
        "758daa02-e7f5-4f8f-af7d-8b0c1481f15c", _pending("git init"))


def test_normal_commands_and_non_bash_pass(monkeypatch):
    """通常コマンド・Bash 以外のツールは従来どおり素通し（Tier 判定へ進む）。"""
    tmp = tempfile.mkdtemp()
    assert _deny(monkeypatch, tmp, "git status && git push origin main") is None
    assert _deny(monkeypatch, tmp, "git init", tool="Write") is None


def test_task_pattern_deny_still_works(monkeypatch):
    """既存のタスク別禁止型拘束（patterns）は従来どおり deny する（退行なし）。"""
    tmp = tempfile.mkdtemp()
    rule = {"patterns": ["ssh-keygen"], "sources": ["鍵の再登録はするな"], "allow_env": []}
    reason = _deny(monkeypatch, tmp, "ssh-keygen -t ed25519", rule=rule)
    assert reason and "禁止型拘束" in reason


# ── sa-ru 側: _write_task_deny が allow_env を刻む ──

def _orch(tmp):
    orch = Orchestrator.__new__(Orchestrator)
    orch.task_deny_dir = tmp
    return orch


def test_write_task_deny_records_allow_env():
    """directive に環境改変コマンドが逐語で含まれるタスクは allow_env 付きで登録される。"""
    tmp = tempfile.mkdtemp()
    task = {"task_id": "758daa02-e7f5-4f8f-af7d-8b0c1481f15c",
            "directive": "git init && git commit -m x", "constraints": []}
    _orch(tmp)._write_task_deny(task)
    with open(os.path.join(tmp, f"{task['task_id']}.json")) as f:
        rule = json.load(f)
    assert rule["allow_env"] == ["git init"] and rule["patterns"] == []


def test_write_task_deny_skips_when_nothing_to_record():
    """patterns も allow_env も無いタスクはファイルを作らない（従来動作維持）。"""
    tmp = tempfile.mkdtemp()
    task = {"task_id": "758daa02-e7f5-4f8f-af7d-8b0c1481f15c",
            "directive": "git push origin main", "constraints": []}
    _orch(tmp)._write_task_deny(task)
    assert os.listdir(tmp) == []
