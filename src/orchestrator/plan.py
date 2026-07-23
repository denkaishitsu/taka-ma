"""計画プレビューと訂正 — 分解結果を実行前に人へ見せ、上書きを受け取る（設計書 §10.2.1）。

計画確認ゲート（§8.10b）が使う純粋ロジックを集める。ここには Slack I/O も LLM 呼び出しも
置かない（テストが実機・モデルに依存しないようにするため。LLM を要する自然言語訂正は
ai_gateway.plan_corrector が担い、本モジュールは出口の構造化パッチだけを受け取る）。

責務:
  - wave（トポロジ段）分割 — 実行本体と同一の依存グラフ解釈を共有する
  - weight（機械的/軽/中/重）の導出 — execution × depth からのみ。model からは逆算しない
  - プレビュー本文の整形
  - 訂正の簡易記法パース（決定的・LLM 不要）とパッチ適用・差分抽出
"""

import re

# execution × depth → 段階ラベル（設計書 §10.2.1 weight 導出規則）。
# model から逆算しないのは、model が上書き・昇格で動くため。逆算すると同じ深さの作業が
# 上書きのたびに違う重さで表示され、ラベルの意味が壊れる。
WEIGHT_INLINE = "機械的"
WEIGHT_SHALLOW = "軽"
WEIGHT_UNSPECIFIED = "中"
WEIGHT_DEEP = "重"

# 深さ語 → depth 値（簡易記法・訂正の日本語入力用）。None は「省略」（写像の unspecified）。
DEPTH_WORDS = {
    "deep": "deep", "重い": "deep", "重": "deep", "深い": "deep", "深く": "deep",
    "shallow": "shallow", "軽い": "shallow", "軽": "shallow", "浅い": "shallow", "浅く": "shallow",
    "中": None, "普通": None, "ふつう": None,
}

# 簡易記法 1 行の文法: 対象（all / 全部 / カンマ区切り step 番号）+ 空白 + 値（モデル名 or 深さ語）。
# 番号を錨にするため、対象の解釈に LLM を要さない（設計書 §10.2.1「訂正の入力経路」）。
_SIMPLE_RE = re.compile(
    r"\A(?P<targets>all|全部|\d+(?:\s*,\s*\d+)*)\s+(?P<value>\S+)\s*\Z", re.IGNORECASE)


def derive_weight(execution: str, depth) -> str:
    """execution × depth から weight ラベルを導出する（設計書 §10.2.1）。"""
    if execution == "inline":
        return WEIGHT_INLINE
    if depth == "shallow":
        return WEIGHT_SHALLOW
    if depth == "deep":
        return WEIGHT_DEEP
    return WEIGHT_UNSPECIFIED


def effective_deps(subtask: dict, step_set: set) -> list:
    """実行時に実際に待ち合わせる依存だけを返す（存在しない step への依存＝dangling は除外）。

    実行本体（_execute_subtask_in_chain の `if dep not in futures: continue`）と同じ解釈を
    ここに一本化する。プレビューの wave 分割・グラフ検証・実行の 3 者で解釈がズレると、
    「見せた段構成と実際の実行順が違う」事故になる（設計書 §10.2.1 の不変条件）。
    """
    return [d for d in (subtask.get("depends_on") or []) if d in step_set]


def compute_waves(subtasks: list[dict]) -> list[list[int]]:
    """サブタスクを wave（トポロジ段）へ束ねる。返り値は step 番号のリストのリスト。

    同一 wave 内は相互に依存せず並行実行され、wave をまたぐと直列になる。依存の解釈は
    effective_deps（＝実行本体と同一）に委ねる。循環がある入力（実行前検証で failed に
    倒される不正グラフ）では残りを最終段へまとめて返し、例外は投げない（プレビューは
    表示であり、グラフ不正の判定・失敗化は実行前検証の責務）。
    """
    step_set = {s["step"] for s in subtasks}
    remaining = {s["step"]: set(effective_deps(s, step_set)) for s in subtasks}
    order = [s["step"] for s in subtasks]  # 分解順を保って表示の並びを安定させる
    waves: list[list[int]] = []
    done: set = set()
    while remaining:
        ready = [st for st in order if st in remaining and remaining[st] <= done]
        if not ready:
            # 循環（実行前検証が failed に倒す不正グラフ）。残りを 1 段にまとめて打ち切る
            waves.append([st for st in order if st in remaining])
            break
        waves.append(ready)
        done.update(ready)
        for st in ready:
            del remaining[st]
    return waves


def build_view(subtasks: list[dict], resolve) -> list[dict]:
    """プレビュー表示用のビュー（step ごとの提示項目）を作る。

    resolve は写像テーブル解決関数（Orchestrator._plan_execution）。モデル解決の SSOT を
    実行側に置いたまま表示に流用するため注入で受ける（表示専用の写像を持たない）。
    """
    waves = compute_waves(subtasks)
    wave_of = {st: i + 1 for i, wave in enumerate(waves) for st in wave}
    width_of = {st: len(wave) for wave in waves for st in wave}
    view = []
    for s in subtasks:
        execution = s.get("execution", "agent")
        depth = s.get("depth")
        lane, candidates, user_specified = resolve(
            execution, depth, s.get("confidence"),
            s.get("model"), s.get("model_override"))
        view.append({
            "step": s["step"],
            "overview": s.get("command", ""),
            "execution": execution,
            "depth": depth,
            "weight": derive_weight(execution, depth),
            "model": candidates[0] if candidates else None,
            "escalation": list(candidates[1:]),
            "user_specified": user_specified,
            "overridden": bool(s.get("model_override")),
            "wave": wave_of.get(s["step"], 1),
            "parallel": width_of.get(s["step"], 1),
        })
    return view


def _model_display(item: dict) -> str:
    """1 サブタスクのモデル欄を組み立てる（実体名 + 由来 + 昇格先）。"""
    model = item["model"] or "（候補なし — matrix 不備）"
    if item["user_specified"]:
        return f"{model}（明示指定・昇格なし）"
    origin = "指定" if item["overridden"] else "自動"
    if item["escalation"]:
        return f"{model}（{origin}／昇格: {' → '.join(item['escalation'])}）"
    return f"{model}（{origin}／昇格なし）"


def format_plan(view: list[dict]) -> str:
    """計画プレビュー本文（テキスト段組み）を作る（設計書 §10.2.1「表示形式」）。"""
    waves: dict[int, list[dict]] = {}
    for item in view:
        waves.setdefault(item["wave"], []).append(item)
    lines = [f"【実行計画】サブタスク {len(view)} 件 / {len(waves)} 段"]
    for wave_no in sorted(waves):
        items = waves[wave_no]
        kind = f"並行 {len(items)} 件" if len(items) > 1 else "直列"
        lines.append(f"── 第{wave_no}段（{kind}）──")
        for item in items:
            axis = f"{item['execution']}/{item['depth']}" if item["depth"] else item["execution"]
            lines.append(f"  {item['step']}. {item['overview']}")
            lines.append(f"     {axis} ・ 重さ: {item['weight']} ・ model: {_model_display(item)}")
    lines.append("訂正例: `2 opus` / `2,4 sonnet` / `3 重い` / `all haiku`（自然文でも可）")
    return "\n".join(lines)


# ── 訂正（簡易記法 → 構造化パッチ） ──

def parse_simple_correction(text: str, valid_models: set) -> list[dict] | None:
    """簡易記法を構造化パッチ列へ変換する。解釈できなければ None を返す。

    None は「簡易記法ではない」の意で、呼び出し側は自然言語経路（ya-ta）へ回す。
    1 行でも解釈できなければ全体を None にする（部分適用はユーザーが何が効いたか
    分からなくなるため。設計書 §10.2.1）。
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    patches = []
    for line in lines:
        m = _SIMPLE_RE.match(line)
        if not m:
            return None
        raw_targets = m.group("targets").lower()
        if raw_targets in ("all", "全部"):
            steps = "all"
        else:
            steps = [int(n) for n in raw_targets.split(",")]
        value = m.group("value")
        if value in valid_models:
            patches.append({"steps": steps, "model": value})
        elif value in DEPTH_WORDS:
            patches.append({"steps": steps, "depth": DEPTH_WORDS[value]})
        else:
            return None
    return patches


def apply_patches(subtasks: list[dict], patches: list[dict],
                  valid_models: set) -> tuple[list[dict], list[str]]:
    """パッチを適用した新しいサブタスク列と、拒否した理由の列を返す。

    簡易記法・自然言語のどちらの経路も、出口はこの 1 つのパッチ適用に集約する
    （パーサを二重に持たない・設計書 §10.2.1）。上書きできるのは model / depth のみ。
    未知の step 番号・未登録モデルは適用せず理由を返す（黙って捨てない）。
    """
    updated = [dict(s) for s in subtasks]
    by_step = {s["step"]: s for s in updated}
    errors: list[str] = []
    for patch in patches or []:
        steps = patch.get("steps")
        if steps == "all" or steps is None:
            targets = list(by_step.values())
        else:
            targets = []
            for st in steps:
                if st in by_step:
                    targets.append(by_step[st])
                else:
                    errors.append(f"Step {st} は存在しません")
        if "model" in patch and patch["model"] is not None:
            model = patch["model"]
            if model not in valid_models:
                errors.append(f"未登録のモデルです: {model}")
            else:
                for s in targets:
                    # 上書きは model_override に載せる。`:モデル名` の明示指定（model）とは
                    # 別キーにするのは、明示指定が昇格を止めるのに対し、計画確認の上書きは
                    # 昇格ラダーを止めないため（設計書 §10.2.1）
                    s["model_override"] = model
        if "depth" in patch:
            depth = patch["depth"]
            if depth not in ("shallow", "deep", None):
                errors.append(f"不正な depth です: {depth}")
            else:
                for s in targets:
                    s["depth"] = depth
                    # depth を変えたら model は写像を引き直す（設計書 §10.2.1）。
                    # 直前の model 上書きは残さない（`3 重い` が opus へ解決されるのはこの再解決による）
                    if "model" not in patch:
                        s.pop("model_override", None)
    return updated, errors


class PlanService:
    """計画の生成・表示・訂正を束ねる（計画確認ゲートが呼ぶ窓口・設計書 §8.10b / §10.2.1）。

    協力者は注入で受ける。分解は ya-ta（TaskDecomposer）、自然言語訂正は ya-ta
    （PlanCorrector）、モデル写像は実行側の解決関数（Orchestrator._plan_execution）で、
    いずれも本サービスは独自の写像・パーサを持たない（表示と実行のズレを構造的に防ぐ）。
    """

    def __init__(self, decomposer, corrector, resolve, valid_models):
        self.decomposer = decomposer
        self.corrector = corrector
        self.resolve = resolve
        self.valid_models = set(valid_models)

    def build(self, summary: str, progress=None) -> list[dict]:
        """確定要約を ya-ta で分解し、プレビュー対象のサブタスク列を返す。"""
        return self.decomposer.decompose(summary, progress=progress)

    def view(self, subtasks: list[dict]) -> list[dict]:
        """実行と同じ写像でモデルを解決したビューを返す。"""
        return build_view(subtasks, self.resolve)

    def render(self, subtasks: list[dict]) -> str:
        """プレビュー本文を返す。"""
        return format_plan(self.view(subtasks))

    def correct(self, subtasks: list[dict], text: str,
                progress=None) -> tuple[list[dict], list[str], str | None]:
        """発話を訂正として解釈し、(更新後プラン, エコー行, 経路) を返す。

        経路は "simple"（簡易記法・即適用）/ "llm"（自然言語・差分エコーで再確認）/
        None（訂正ではない＝呼び出し側は通常の会話処理へ落とす）。自然言語経路で
        実質的な変化が無かった場合も None に倒す（誤検知でプランを触った体裁にしない）。
        """
        patches = parse_simple_correction(text, self.valid_models)
        route = "simple"
        if patches is None:
            patches = self.corrector.correct(subtasks, text, progress=progress)
            route = "llm"
            if not patches:
                return subtasks, [], None
        before = self.view(subtasks)
        updated, errors = apply_patches(subtasks, patches, self.valid_models)
        changes = diff_view(before, self.view(updated))
        if route == "llm" and not changes and not errors:
            return subtasks, [], None
        return updated, changes + errors, route


def diff_view(before: list[dict], after: list[dict]) -> list[str]:
    """適用前後のビューを比べ、変わった項目だけを 1 行ずつ返す（差分エコー・§10.2.1）。"""
    prev = {item["step"]: item for item in before}
    lines = []
    for item in after:
        old = prev.get(item["step"])
        if not old:
            continue
        changes = []
        for key, label in (("depth", "depth"), ("weight", "重さ"), ("model", "model")):
            if old[key] != item[key]:
                changes.append(f"{label} {old[key] or '省略'} → {item[key] or '省略'}")
        if changes:
            lines.append(f"Step {item['step']}: " + " / ".join(changes))
    return lines
