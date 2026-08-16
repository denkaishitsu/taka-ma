"""完了報告の実出力グラウンディング — worker の自己申告を検証コマンド実出力で裏取りする。

設計書 §8.9「完了報告の実出力グラウンディング（虚偽完了報告の禁止）」。

worker の最終出力は LLM の生成テキストであり、「push しました」「コミットしました」という
主張がそのまま人間に届くと、実体のない完了報告（2026-08-10 インシデント F1）になる。
本モジュールは worker 出力から主張（push / commit）を検出し、sa-ru 自身が workspace に対して
実行した検証コマンド（git log / git ls-remote / ls）の rc・実出力**だけ**から判定と報告文を
機械的に組み立てる。worker のテキストは「どの検証を要するか」の選別にのみ使い、
判定の根拠には使わない。

検証コマンドの実行手段（SSH）は呼び出し側から関数として注入する
（RemoteProcessManager.run_ssh_probe。単体テストでは偽物に差し替える）。
"""

import logging
import re
import shlex

logger = logging.getLogger("sa-ru.grounding")

# worker 出力から「push した」という主張を検出する（日英・過去/完了形の言い回しを拾う）。
# 誤検出（push の話題に触れただけ）は過剰検証＝安全側に倒れるだけで、虚偽完了は生まない。
_PUSH_CLAIM_RE = re.compile(
    r"push(?:ed)?|プッシュ", re.IGNORECASE)
# 「コミットした」という主張の検出（同上。commit を含む語で広めに拾う）
_COMMIT_CLAIM_RE = re.compile(
    r"commit(?:ted)?|コミット", re.IGNORECASE)
# 明確に否定された言及（「push はしていません」等）は主張として扱わない。正直な未実施報告に
# 「⚠ 未完了」警告を重ねると報告の信頼性を下げるため。判定できない曖昧な言及は主張扱い
# （検証する側）に倒す。
_NEG_AFTER_RE = re.compile(
    r"^[^\n。、．，.,!?！？（）()]{0,8}?(?:ていません|ていない|しません|しないで|せず|"
    r"できません|できなかった|されていません|未実施|未了)")
_NEG_BEFORE_RE = re.compile(
    r"(?:\bnot|n't|\bnever|\bwithout)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


def _has_affirmative_claim(text: str, word_re: re.Pattern) -> bool:
    """word_re の言及のうち、明確な否定を伴わないものが 1 つでもあれば主張ありとみなす。"""
    for m in word_re.finditer(text):
        if _NEG_AFTER_RE.search(text[m.end():m.end() + 24]):
            continue
        if _NEG_BEFORE_RE.search(text[max(0, m.start() - 24):m.start()]):
            continue
        return True
    return False

# 検証コマンドの 1 本あたりの SSH タイムアウト（秒）。ls-remote はネットワークを跨ぐため
# 他より長めに取る。完了通知を無限に遅らせない上限でもある。
PROBE_TIMEOUT_SEC = 30
LS_REMOTE_TIMEOUT_SEC = 60

# 報告へ載せる 1 コマンド出力の上限文字数（証跡として十分な範囲で通知の肥大を防ぐ）
_PROBE_OUTPUT_MAX_CHARS = 1500


class GroundingReport:
    """グラウンディング検証 1 回分の結果。

    ok:      検出した主張がすべて実出力で裏付けられたか（主張が無ければ True）。
    note:    ok=False のときの短い理由（通知ヘッダに載せる。例「push 未確認」）。
    text:    実行した検証コマンドと rc・実出力を列挙した証跡ブロック（通知・結果ファイルに併記）。
    summary: 会話還流の先頭に置く 1 行判定（worker 自己申告の上書き用）。
    workspace: 検証対象の workspace（呼び出し側が設定。会話への紐付けに使う）。
    """

    def __init__(self, ok: bool, note: str, text: str, summary: str,
                 workspace: str | None = None):
        self.ok = ok
        self.note = note
        self.text = text
        self.summary = summary
        self.workspace = workspace


def detect_claims(worker_text: str) -> dict:
    """worker 出力（全サブタスク分の連結で良い）から検証対象の主張を検出する。"""
    text = worker_text or ""
    return {
        "push": _has_affirmative_claim(text, _PUSH_CLAIM_RE),
        "commit": _has_affirmative_claim(text, _COMMIT_CLAIM_RE),
    }


class GroundingVerifier:
    """workspace に検証コマンドを実行し、実出力だけから完了報告の裏取りを行う。

    run_probe: callable(command: str, timeout: int) -> (rc: int, output: str)。
        非 0 終了を例外化せず rc で返すこと（RemoteProcessManager.run_ssh_probe）。
        SSH 不達等の実行不能は rc=-1 で表現される。
    """

    def __init__(self, run_probe):
        self._run_probe = run_probe

    def _probe(self, lines: list[str], command: str,
               timeout: int = PROBE_TIMEOUT_SEC) -> tuple[int, str]:
        """検証コマンドを 1 本実行し、証跡ブロックへ「$ cmd / rc / 出力」を追記して結果を返す。"""
        rc, output = self._run_probe(command, timeout)
        lines.append(f"$ {command} (rc={rc})")
        out = (output or "").strip()
        if len(out) > _PROBE_OUTPUT_MAX_CHARS:
            out = out[:_PROBE_OUTPUT_MAX_CHARS] + "\n…（以降略）"
        lines.append(out if out else "（出力なし）")
        return rc, (output or "").strip()

    def verify(self, workspace: str, worker_text: str) -> GroundingReport:
        """worker の主張を workspace の実状態と突き合わせ、判定と証跡を返す。

        判定規則（設計書 §8.9）:
          - push 主張: remote 側の当該ブランチ先端がローカル HEAD と一致して初めて「確認済み」。
            それ以外（remote 未設定・不一致・rc 非 0・SSH 不達）はすべて「push 未完了」。
          - commit 主張: `git log -1` で実ハッシュが取れて初めて「確認済み」。
          - ファイル生成: 主張の有無に依らず `ls -la` 実出力を常に証跡へ含める
            （ファイル生成報告に実パス・ls 実出力を必須とする要件の担保）。
        """
        claims = detect_claims(worker_text)
        ws = shlex.quote(workspace)
        lines = [f"【実測確認】sa-ru が workspace で実行（workspace: {workspace}）"]
        problems: list[str] = []

        # workspace の実在とファイル一覧（ファイル生成報告の裏付け。常に取る）。
        # 取得失敗を「未完了」へ倒すのは検証対象の主張があるときだけ。主張なしのタスクまで
        # 一時的な SSH 不達で ⚠ 警告すると、警告が常態化して本物の未完了が埋もれる
        ls_rc, _ = self._probe(lines, f"ls -la {ws}")
        if ls_rc != 0 and (claims["push"] or claims["commit"]):
            problems.append("workspace を確認できない")

        # git リポジトリかどうか（以降の git 検証の前提）
        repo_rc, _ = self._probe(lines, f"git -C {ws} rev-parse --is-inside-work-tree")
        is_repo = (repo_rc == 0)

        head_hash = ""
        if is_repo:
            # ローカル HEAD の実ハッシュ（コミット報告の根拠）
            log_rc, log_out = self._probe(
                lines, f"git -C {ws} log -1 --format='%H %s'")
            if log_rc == 0 and log_out:
                head_hash = log_out.split()[0]
            if claims["commit"] and not head_hash:
                problems.append("コミットを確認できない（git log 実出力なし）")
        elif claims["commit"]:
            problems.append("コミットを確認できない（workspace が git リポジトリでない）")

        if claims["push"]:
            if not is_repo:
                problems.append("push は未完了（workspace が git リポジトリでない）")
            else:
                branch_rc, branch = self._probe(
                    lines, f"git -C {ws} rev-parse --abbrev-ref HEAD")
                remote_hash = ""
                if branch_rc == 0 and branch:
                    lsr_rc, lsr_out = self._probe(
                        lines,
                        f"git -C {ws} ls-remote origin {shlex.quote(branch)}",
                        timeout=LS_REMOTE_TIMEOUT_SEC)
                    if lsr_rc == 0 and lsr_out:
                        remote_hash = lsr_out.split()[0]
                else:
                    lines.append("（ブランチ名を解決できないため ls-remote を実行できない）")
                if not remote_hash:
                    problems.append("push は未完了（remote 側でコミットを確認できない）")
                elif head_hash and remote_hash != head_hash:
                    problems.append(
                        "push は未完了（remote 先端がローカル HEAD と不一致: "
                        f"remote={remote_hash[:12]} local={head_hash[:12]}）")
                elif not head_hash:
                    problems.append("push は未完了（ローカル HEAD を確認できない）")

        ok = not problems
        note = "・".join(problems)
        if ok:
            confirmed = []
            if claims["commit"] and head_hash:
                confirmed.append(f"コミット {head_hash[:12]} を実測確認")
            if claims["push"]:
                confirmed.append("push を remote 実出力で確認")
            summary = ("（実測確認済み: " + "、".join(confirmed) + "）") if confirmed \
                else "（実測確認: 主張された成果物の検証項目なし。証跡は結果ファイル参照）"
            lines.append("判定: " + ("、".join(confirmed) if confirmed
                                     else "push/コミットの主張なし（ls 実出力のみ記録）"))
        else:
            summary = f"（実測確認の結果、未完了: {note}）"
            lines.append(f"判定: {note}")
        return GroundingReport(ok=ok, note=note, text="\n".join(lines), summary=summary)
