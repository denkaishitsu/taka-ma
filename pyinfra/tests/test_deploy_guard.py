"""未マージ配備ガード（pyinfra/deploys/_guard.py・#taka-ma/141）の単体テスト。

pyinfra 本体に依存しないよう、判定は evaluate()（純粋関数）で、git 連携は
一時リポジトリを実際に作って collect_state() / ensure_merged_head() で検証する。

実行例（リポジトリのルートから）:
  uv run --with pytest python -m pytest pyinfra/tests/test_deploy_guard.py -v
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploys"))
import _guard  # noqa: E402


# --- evaluate()（純粋関数）: 判定ロジックそのもの ---

def test_evaluate_merged_origin_main_passes():
    ok, reason = _guard.evaluate("a" * 40, {"origin/main": True}, False)
    assert ok
    assert "origin/main" in reason


def test_evaluate_merged_local_main_only_passes():
    """remote 未設定（origin/main 参照なし）でもローカル main に含まれれば可。"""
    ok, _ = _guard.evaluate("a" * 40, {"main": True}, False)
    assert ok


def test_evaluate_unmerged_stops_with_bypass_hint():
    ok, reason = _guard.evaluate(
        "b" * 40, {"origin/main": False, "main": False}, False)
    assert not ok
    assert "含まれていません" in reason
    assert _guard.ALLOW_UNMERGED_ENV in reason  # 迂回手段を案内する


def test_evaluate_unmerged_bypassed_by_flag():
    ok, reason = _guard.evaluate(
        "b" * 40, {"origin/main": False, "main": False}, True)
    assert ok
    assert _guard.ALLOW_UNMERGED_ENV in reason  # 迂回した事実を明示する


def test_evaluate_no_head_stops():
    """git 不在・リポジトリ外（HEAD 解決不能）は検査不能として停止する。"""
    ok, _ = _guard.evaluate(None, {}, False)
    assert not ok


def test_evaluate_no_main_refs_stops():
    """main 参照が一つも無い（判定不能）場合も黙って通さない。"""
    ok, reason = _guard.evaluate("c" * 40, {}, False)
    assert not ok
    assert _guard.ALLOW_UNMERGED_ENV in reason


# --- collect_state() / ensure_merged_head(): 実 git リポジトリで結線を検証 ---

def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test",
         *args],
        cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """main ブランチにコミット 1 つを持つ一時リポジトリ。"""
    _git(["init", "-b", "main"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "c1"], tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_bypass_env(monkeypatch):
    """テスト外環境の迂回フラグに影響されない。"""
    monkeypatch.delenv(_guard.ALLOW_UNMERGED_ENV, raising=False)


def test_collect_state_on_main(repo):
    head, merged = _guard.collect_state(repo)
    assert head and len(head) == 40
    assert merged == {"main": True}  # origin/main は無いので載らない


def test_collect_state_reads_origin_main(repo):
    _git(["update-ref", "refs/remotes/origin/main", "main"], repo)
    _, merged = _guard.collect_state(repo)
    assert merged == {"origin/main": True, "main": True}


def test_ensure_merged_head_passes_on_main(repo):
    _guard.ensure_merged_head(repo)  # 例外が出なければ通過


def test_ensure_merged_head_stops_on_unmerged_branch(repo):
    """事故の再現形: main から分岐した未マージ worktree ブランチの HEAD は停止。"""
    _git(["checkout", "-b", "task-135"], repo)
    _git(["commit", "--allow-empty", "-m", "unmerged"], repo)
    with pytest.raises(SystemExit) as exc:
        _guard.ensure_merged_head(repo)
    assert "含まれていません" in str(exc.value)


def test_ensure_merged_head_bypassed_by_env(repo, monkeypatch, capsys):
    _git(["checkout", "-b", "task-135"], repo)
    _git(["commit", "--allow-empty", "-m", "unmerged"], repo)
    monkeypatch.setenv(_guard.ALLOW_UNMERGED_ENV, "1")
    _guard.ensure_merged_head(repo)  # 迂回により停止しない
    assert _guard.ALLOW_UNMERGED_ENV in capsys.readouterr().err  # 迂回を明示


def test_ensure_merged_head_env_other_value_still_stops(repo, monkeypatch):
    """迂回は "1" のみ。それ以外の値では緩めない。"""
    _git(["checkout", "-b", "task-135"], repo)
    _git(["commit", "--allow-empty", "-m", "unmerged"], repo)
    monkeypatch.setenv(_guard.ALLOW_UNMERGED_ENV, "true")
    with pytest.raises(SystemExit):
        _guard.ensure_merged_head(repo)


def test_ensure_merged_head_stops_outside_repo(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _guard.ensure_merged_head(tmp_path)
    assert "解決できません" in str(exc.value)


def test_ensure_merged_head_stops_without_main_ref(tmp_path):
    _git(["init", "-b", "trunk"], tmp_path)
    _git(["commit", "--allow-empty", "-m", "c1"], tmp_path)
    with pytest.raises(SystemExit) as exc:
        _guard.ensure_merged_head(tmp_path)
    assert "判定ができません" in str(exc.value)
