"""モデル自動監視 — 候補の検証・実測・適合判定・入替提案の無人パイプライン（設計書 §7.4）。

何のためのモジュールか:
    ウォッチリスト（config/model_watch.yaml）に人間が登録した候補モデルについて、
    「一次ソース検証 → 稼働機での実常駐実測 → 容量適合判定 → Slack 提案」を無人で行う。
    候補の発見（リスト登録）と最終採用判断（Approve / Reject）は人間に残す（半自動の原則）。

設計上の要点:
    - 検証は機械可読な一次ソース（HuggingFace API / ollama レジストリ manifest）への
      HTTP 取得のみで行い、LLM を介さない（AI 出力のスペック混入を構造的に排除）。
    - 実測は本番稼働機のメモリを圧迫しない固定手順（§7.4 実測プロトコル）:
      現行モデルを解放 → 実測ガード（予測常駐で事前見積り）→ 役割と同じ num_ctx で
      ロード → `ollama ps` 取得 → 即解放 → 現行モデル復帰。異常時も解放・復帰を必ず行う。
    - 適合判定は model_monitor.evaluate_swap（予算 − 最小余裕 の不等式）に委譲する。
    - 提案は §8.10 の承認ファイル（status=pending）＋ §8.9 の Block Kit
      （action_id: approve_action / reject_action。slack_bot/handlers/actions.py と対応）で
      既存の承認経路へ載せる。決着後の入替はランブック B（人間ゲートの先）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ai_gateway.model_monitor import (
    DEFAULT_CAPACITY_PATH,
    SwapProposal,
    evaluate_swap,
    format_proposal,
    host_budget_gb,
    load_capacity,
)

DEFAULT_WATCH_PATH = Path(__file__).parent / "config" / "model_watch.yaml"

# §8.10 の承認ディレクトリ（u-zu resolve_approval / sa-ru と同じ既定値・同じ環境変数で SSOT 化）
APPROVAL_DIR = os.environ.get("TAKA_MA_APPROVAL_DIR", "/opt/taka-ma/data/approvals")

# 一次ソースのエンドポイント（キュレート済み。ここ以外へは取得に行かない＝自動スクレイピング禁止の担保）
OLLAMA_REGISTRY = "https://registry.ollama.ai/v2/library/{repo}/manifests/{tag}"
HF_MODEL_API = "https://huggingface.co/api/models/{repo}"
HF_CONFIG_RAW = "https://huggingface.co/{repo}/raw/main/config.json"

HTTP_TIMEOUT_SEC = 30
PULL_TIMEOUT_SEC = 3600     # 18GB 級の pull を見込む
LOAD_TIMEOUT_SEC = 900      # コールドロード（ディスク→RAM）を見込む


@dataclass
class VerifiedSpec:
    """一次ソースから機械取得した候補スペック。取得できなかった項目は None（推測で埋めない）。"""

    model: str                       # ollama タグ
    hf_repo: str
    weights_gb: float | None = None  # ollama manifest の model レイヤ実サイズ（量子化重み）
    blob_total_gb: float | None = None  # manifest 全レイヤ合計（pull されるディスクサイズ）
    hf_context: int | None = None    # HF config.json の max_position_embeddings
    license: str | None = None
    vision: bool | None = None       # config.json に vision_config がある / アーキ名に VL を含む
    architectures: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)   # 実際に取得できた URL（根拠の提示用）
    errors: list[str] = field(default_factory=list)    # 取得失敗の記録（隠さない）


@dataclass
class Measurement:
    """稼働機での実測結果（`ollama ps` 由来。ここに推測値は入らない）。"""

    size_gb: float               # 実常駐（重み+KV）
    context: int | None          # ロード時の実効 num_ctx
    host: str
    restored: bool               # 現行モデルの常駐を復帰できたか
    log: list[str] = field(default_factory=list)


def _fetch(url: str, headers: dict | None = None) -> bytes:
    """一次ソースの 1 URL を取得する（タイムアウト付き・リトライなし。失敗は呼び出し側で記録）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "taka-ma-model-watch"} | (headers or {}))
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        return resp.read()


def _fetch_json(url: str, headers: dict | None = None) -> dict:
    return json.loads(_fetch(url, headers).decode("utf-8"))


def verify_candidate(model: str, hf_repo: str) -> VerifiedSpec:
    """候補スペックを一次ソースから取得する（検証段）。

    取得先は ollama レジストリ manifest（量子化サイズ）と HuggingFace（コンテキスト長・
    ライセンス・モダリティ）。取得失敗は errors に残し、値は None のままにする
    （後段の実測ガードは weights_gb が無ければ実測を中止する＝安全側）。
    """
    spec = VerifiedSpec(model=model, hf_repo=hf_repo)

    repo, _, tag = model.partition(":")
    url = OLLAMA_REGISTRY.format(repo=repo, tag=tag or "latest")
    try:
        manifest = _fetch_json(url, {"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        layers = manifest.get("layers", [])
        model_bytes = sum(l["size"] for l in layers if l.get("mediaType", "").endswith("image.model"))
        total_bytes = sum(l["size"] for l in layers)
        if model_bytes:
            spec.weights_gb = round(model_bytes / 1e9, 1)
        spec.blob_total_gb = round(total_bytes / 1e9, 1)
        spec.sources.append(url)
    except Exception as e:  # noqa: BLE001 — 取得失敗の種類は記録して続行（部分的な検証を許す）
        spec.errors.append(f"ollama manifest 取得失敗: {url}: {e}")

    url = HF_MODEL_API.format(repo=hf_repo)
    try:
        info = _fetch_json(url)
        spec.license = (info.get("cardData") or {}).get("license") or next(
            (t.split(":", 1)[1] for t in info.get("tags", []) if t.startswith("license:")), None)
        spec.sources.append(url)
    except Exception as e:  # noqa: BLE001
        spec.errors.append(f"HF model API 取得失敗: {url}: {e}")

    url = HF_CONFIG_RAW.format(repo=hf_repo)
    try:
        cfg = _fetch_json(url)
        text_cfg = cfg.get("text_config") or cfg
        ctx = text_cfg.get("max_position_embeddings")
        spec.hf_context = int(ctx) if ctx else None
        spec.architectures = cfg.get("architectures") or []
        spec.vision = bool(cfg.get("vision_config")) or any(
            "vl" in a.lower() or "vision" in a.lower() for a in spec.architectures)
        spec.sources.append(url)
    except Exception as e:  # noqa: BLE001
        spec.errors.append(f"HF config.json 取得失敗: {url}: {e}")

    return spec


class HostRunner:
    """稼働機上でコマンドを実行する（ssh エイリアス指定時は SSH、null は本機）。

    実測系コマンド（ollama / curl→localhost:11434）は「当該 host の上」でしか意味を
    持たないため、到達方法だけをここに閉じ込める（通信方式は SSH のみ・§8.1）。
    """

    def __init__(self, ssh: str | None, ollama_bin: str):
        self.ssh = ssh
        self.ollama = ollama_bin

    def run(self, cmd: str, timeout: int) -> tuple[int, str]:
        argv = ["ssh", self.ssh, cmd] if self.ssh else ["/bin/sh", "-c", cmd]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()


_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB)")


def parse_ollama_ps(text: str) -> dict[str, dict]:
    """`ollama ps` の出力を {モデル名: {size_gb, context}} に解析する。

    列構成はバージョンで揺れるため位置に依存しない: SIZE は「数値 GB/MB」パターン、
    CONTEXT は 512 以上の裸の整数トークン（% や ID に混ざらない）として拾う。
    """
    result: dict[str, dict] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("NAME"):
            continue
        tokens = line.split()
        name = tokens[0]
        m = _SIZE_RE.search(line)
        if not m:
            continue
        size_gb = float(m.group(1)) / (1000 if m.group(2) == "MB" else 1)
        context = None
        for tok in tokens[1:]:
            if tok.isdigit() and int(tok) >= 512:
                context = int(tok)
        result[name] = {"size_gb": size_gb, "context": context}
    return result


def _load_model(runner: HostRunner, model: str, num_ctx: int | None, keep_alive: int, timeout: int) -> tuple[int, str]:
    """ollama HTTP API（localhost）で 1 トークン生成し、モデルを指定 num_ctx でロードする。

    CLI `ollama run` は num_ctx を渡せないため HTTP API を使う（§8.1: localhost API は
    SSH で host に入ってから叩く＝ポートは開けない）。keep_alive は秒数（-1 = 常駐）。
    """
    payload: dict = {"model": model, "prompt": "hi", "stream": False,
                     "keep_alive": keep_alive, "options": {"num_predict": 1}}
    if num_ctx:
        payload["options"]["num_ctx"] = num_ctx
    body = json.dumps(payload)
    return runner.run(
        f"curl -s --max-time {timeout - 10} http://localhost:11434/api/generate -d '{body}'", timeout)


def _runner_for_host(host: str, host_cfg: dict, ssh_override: str | None,
                     on_host: str | None) -> HostRunner:
    """対象 host へのランナーを作る。ローカル実行（ssh 未設定）はホスト同一性を要求する。

    `ssh: null` は「model_watch を実行している本機＝当該 host」の意。実行機がどの host かは
    設定から分からないため、`--on-host`（or 環境変数 TAKA_MA_MODEL_WATCH_HOST）の明示宣言と
    一致しない限りローカル実行を拒否する（fail-closed）。これが無いと、mac-mini 上の定期実行が
    MBP 役割（light/que）の候補を mac-mini 上で誤実測・誤 pull する事故が起きる。
    """
    ssh = ssh_override or host_cfg.get("ssh")
    if ssh is None:
        declared = on_host or os.environ.get("TAKA_MA_MODEL_WATCH_HOST")
        if declared != host:
            raise RuntimeError(
                f"ホスト同一性ガード: 役割の稼働機 {host} への SSH 設定が無く、実行機の宣言"
                f"（--on-host / TAKA_MA_MODEL_WATCH_HOST={declared}）とも一致しないため実行を中止。"
                f"本機が {host} なら --on-host {host} を、別マシンなら --ssh <alias> を指定する")
    return HostRunner(ssh, host_cfg["ollama_bin"])


def measure_candidate(candidate: dict, capacity: dict, watch: dict,
                      ssh_override: str | None = None,
                      on_host: str | None = None) -> Measurement:
    """候補の実常駐を対象役割の稼働機で実測する（§7.4 実測プロトコル）。

    メモリ保護のため (1) 現行モデルを先に解放し候補と同時常駐させない
    (2) 予測常駐（検証済み重み × kv_margin）で事前見積りし、収まる見込みが無ければ
    ロード自体を中止する (3) 取得後は即解放し、現行モデルの常駐を復帰する。
    (2) の重み値が無い（一次ソース検証失敗）場合も中止する（安全側）。
    """
    role = candidate["role"]
    model = candidate["model"]
    role_info = capacity["roles"][role]
    host = role_info["host"]
    host_cfg = watch["hosts"][host]
    runner = _runner_for_host(host, host_cfg, ssh_override, on_host)
    log: list[str] = []

    weights_gb = candidate.get("_verified_weights_gb")
    if not weights_gb:
        raise RuntimeError("実測ガード: 検証済み重みサイズが無いため実測を中止（verify 段の取得失敗を先に解消する）")

    # (1) 現状把握と現行モデルの一時解放（候補と同時常駐させない）
    rc, out = runner.run(f"{runner.ollama} ps", 60)
    if rc != 0:
        raise RuntimeError(f"ollama ps 失敗: {out}")
    before = parse_ollama_ps(out)
    log.append(f"ps(before): {before}")
    current = role_info["model"]
    current_was_resident = current in before
    if current_was_resident:
        rc, out = runner.run(f"{runner.ollama} stop {current}", 120)
        log.append(f"stop {current}: rc={rc}")
        if rc != 0:
            raise RuntimeError(f"現行モデルの解放に失敗、実測を中止: {out}")

    # (2) 実測ガード: 解放後の常駐合計 + 予測常駐 が予算（RAM − 予約）を超える見込みなら中止
    rc, out = runner.run(f"{runner.ollama} ps", 60)
    resident = parse_ollama_ps(out) if rc == 0 else {}
    resident_total = sum(v["size_gb"] for v in resident.values())
    expected = float(weights_gb) * float(watch.get("kv_margin", 1.3))
    budget_all = host_budget_gb(capacity, host)
    log.append(f"guard: 常駐 {resident_total:g}GB + 予測 {expected:g}GB vs 予算 {budget_all:g}GB")
    if resident_total + expected > budget_all:
        _restore(runner, current, current_was_resident, log)
        raise RuntimeError(
            f"実測ガード: 予測常駐 {expected:g}GB は現状の空きに収まらない見込みのため中止"
            f"（常駐 {resident_total:g}GB / 予算 {budget_all:g}GB）")

    measured: dict | None = None
    try:
        # pull（ディスクのみ・冪等）→ 役割と同じ num_ctx でロード → ps 取得
        rc, out = runner.run(f"{runner.ollama} pull {model}", PULL_TIMEOUT_SEC)
        log.append(f"pull {model}: rc={rc}")
        if rc != 0:
            raise RuntimeError(f"ollama pull 失敗: {out[-500:]}")
        num_ctx = role_info.get("context")
        rc, out = _load_model(runner, model, num_ctx, keep_alive=300, timeout=LOAD_TIMEOUT_SEC)
        log.append(f"load {model} (num_ctx={num_ctx}): rc={rc}")
        if rc != 0:
            raise RuntimeError(f"候補ロード失敗: {out[-500:]}")
        rc, out = runner.run(f"{runner.ollama} ps", 60)
        after = parse_ollama_ps(out) if rc == 0 else {}
        log.append(f"ps(loaded): {after}")
        measured = after.get(model)
        if not measured:
            raise RuntimeError(f"ロード後の ollama ps に候補 {model} が見えない: {out}")
    finally:
        # (3) 異常時も候補を必ず解放し、常駐を実測前の状態へ復帰する（本番メモリ保護の要）。
        # 解放完了を待ってから復帰ロードするのは、解放中の再ロードがメモリ二重計上となり
        # 同居モデルの追い出しを誘発するため（2026-08-26 実機 E2E で観測）。
        rc, _ = runner.run(f"{runner.ollama} stop {model}", 120)
        log.append(f"stop {model}: rc={rc}")
        _wait_unloaded(runner, model, log)
        restored = _restore(runner, current, current_was_resident, log)
        restored = _restore_coresidents(runner, before, current, log) and restored

    return Measurement(size_gb=measured["size_gb"], context=measured.get("context"),
                       host=host, restored=restored, log=log)


def _wait_unloaded(runner: HostRunner, model: str, log: list[str],
                   attempts: int = 12, interval_sec: int = 5) -> bool:
    """候補の解放完了（`ollama ps` から消える）を待つ。

    `ollama stop` は即時に返るが実解放は遅れることがあり、解放が終わる前に次のロードを
    始めると ollama が空き不足とみなして keep_alive=-1 の同居モデルまで追い出す
    （2026-08-26 実機 E2E で sa-ru 会話脳の脱落として観測）。復帰ロード前に必ず待つ。
    """
    import time
    for i in range(attempts):
        rc, out = runner.run(f"{runner.ollama} ps", 60)
        if rc == 0 and model not in parse_ollama_ps(out):
            return True
        time.sleep(interval_sec)
    log.append(f"解放待ちタイムアウト: {model} が ps に残存")
    return False


def _restore_coresidents(runner: HostRunner, before: dict[str, dict], current: str,
                         log: list[str]) -> bool:
    """実測前に常駐していた同居モデルの脱落を検出し、元の context で常駐へ戻す。

    現行モデル（current）は _restore が担うため対象外。ps(before) を正として突き合わせ、
    外れたモデルを keep_alive=-1・元の num_ctx で再ロードし、ps で復帰を確認する
    （ロードの都度追い出しが連鎖しうるため、1 モデルずつ順に確認する）。
    """
    rc, out = runner.run(f"{runner.ollama} ps", 60)
    resident = parse_ollama_ps(out) if rc == 0 else {}
    ok = rc == 0
    for name, info in before.items():
        if name == current or name in resident:
            continue
        log.append(f"同居モデルの脱落検出: {name} → 再ロード")
        rc, out2 = _load_model(runner, name, info.get("context"), keep_alive=-1,
                               timeout=LOAD_TIMEOUT_SEC)
        if rc != 0:
            log.append(f"同居モデル再ロード失敗: {name}: {out2[-300:]}")
            ok = False
            continue
        rc, out = runner.run(f"{runner.ollama} ps", 60)
        resident = parse_ollama_ps(out) if rc == 0 else {}
        if name not in resident:
            log.append(f"同居モデル復帰未確認: {name}")
            ok = False
    log.append(f"ps(final): {resident}")
    return ok


def _restore(runner: HostRunner, current: str, was_resident: bool, log: list[str]) -> bool:
    """現行モデルの常駐を復帰する（元々常駐していなければ何もしない）。

    keep_alive=-1（無期限常駐）で戻すのは本番の常駐方式に合わせるため（§7.2 / ya-ta の
    HTTP API 呼び出しは毎回 keep_alive を指定するので、次のリクエストでも上書きされる）。
    """
    if not was_resident:
        return True
    rc, out = _load_model(runner, current, None, keep_alive=-1, timeout=LOAD_TIMEOUT_SEC)
    log.append(f"restore {current}: rc={rc}")
    if rc != 0:
        log.append(f"restore 失敗詳細: {out[-300:]}")
        return False
    rc, out = runner.run(f"{runner.ollama} ps", 60)
    ok = rc == 0 and current in parse_ollama_ps(out)
    log.append(f"ps(restored): {parse_ollama_ps(out) if rc == 0 else out}")
    return ok


def build_slack_blocks(request_id: str, p: SwapProposal, spec: VerifiedSpec,
                       meas: Measurement) -> list:
    """入替提案の Block Kit。action_id / value は u-zu handlers/actions.py の
    approve_action / reject_action（request_id 汎用）と対応する（§8.9/§8.10 の再利用）。"""
    verdict = ("✅ 余裕を残して収まる" if p.acceptable
               else ("⚠️ 収まるが常用余裕不足" if p.fits else "❌ 予算超過"))
    return [
        {"type": "header",
         "text": {"type": "plain_text", "text": ":arrows_counterclockwise: モデル入替提案"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*役割:*\n{p.role}（{p.host}）"},
            {"type": "mrkdwn", "text": f"*現行 → 候補:*\n{p.current_model} → {p.candidate_model}"},
            {"type": "mrkdwn", "text": f"*実測常駐:*\n{p.candidate_size_gb:g}GB（num_ctx {meas.context}）"},
            {"type": "mrkdwn",
             "text": (f"*容量判定:*\n{verdict}（同居 {p.coexisting_gb:g} + 候補 {p.candidate_size_gb:g} "
                      f"≤ 予算 {p.budget_gb:g} − 余裕 {p.min_headroom_gb:g}、残 {p.headroom_gb:g}GB）")},
            {"type": "mrkdwn",
             "text": (f"*一次ソース検証:*\n重み {spec.weights_gb}GB / ctx {spec.hf_context} / "
                      f"license {spec.license} / vision {spec.vision}")},
            {"type": "mrkdwn", "text": f"*Request ID:*\n{request_id}"},
        ]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*根拠:* {p.rationale or '（未記載）'}"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
             "style": "primary", "action_id": "approve_action", "value": request_id},
            {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
             "style": "danger", "action_id": "reject_action", "value": request_id},
        ]},
    ]


def propose(p: SwapProposal, spec: VerifiedSpec, meas: Measurement, watch: dict,
            send_slack: bool = True) -> str:
    """提案を記録し（正本 JSON ＋ §8.10 承認ファイル）、Slack へ提示する。request_id を返す。

    承認ファイルは status=pending で作成し、u-zu のボタン押下（approve_action /
    reject_action → resolve_approval）が status を決着させる。決着後の入替はランブック B。
    """
    import uuid
    request_id = f"model-swap-{uuid.uuid4().hex[:12]}"
    now = datetime.datetime.now().astimezone().isoformat()

    record = {
        "request_id": request_id,
        "type": "model_swap",
        "created_at": now,
        "proposal": asdict(p),
        "verified": asdict(spec),
        "measurement": asdict(meas),
    }
    record_dir = Path(watch.get("record_dir", "/opt/taka-ma/data/model_watch"))
    record_dir.mkdir(parents=True, exist_ok=True)
    record_path = record_dir / f"{request_id}.json"
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    approval = {
        "request_id": request_id,
        "type": "model_swap",
        "task_id": "",   # タスク実行系ではないため空（sa-ru の保留走査は task_id 一致で拾うので干渉しない）
        "command": f"model-swap {p.role}: {p.current_model} -> {p.candidate_model}",
        "tool_name": "model_swap",
        "risk_reason": "モデル入替提案（承認後にランブック B で入替）",
        "status": "pending",
        "created_at": now,
    }
    os.makedirs(APPROVAL_DIR, exist_ok=True)
    with open(os.path.join(APPROVAL_DIR, f"{request_id}.json"), "w", encoding="utf-8") as f:
        json.dump(approval, f, ensure_ascii=False, indent=2)

    if send_slack:
        from dotenv import load_dotenv
        from slack_sdk import WebClient
        load_dotenv("/opt/taka-ma/config/.env")
        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        client.chat_postMessage(
            channel=os.environ["SLACK_CHANNEL_ID"],
            text=f"モデル入替提案: {p.role} {p.current_model} -> {p.candidate_model} (ID: {request_id})",
            blocks=build_slack_blocks(request_id, p, spec, meas),
        )
    return request_id


def load_watch(path: Path = DEFAULT_WATCH_PATH) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def is_production_model(model: str, capacity: dict) -> bool:
    """model が現行の本番モデル（capacity roles のどれか）かを返す。

    cleanup（ollama rm＝破壊的）の誤射防止: 却下済み「候補」の撤去専用であり、
    ウォッチリストの誤登録経由で現行モデルを消さない。
    """
    return any(info["model"] == model for info in capacity["roles"].values())


def run_pipeline(candidate: dict, capacity: dict, watch: dict, *,
                 send_slack: bool, skip_measure: bool = False,
                 skip_propose: bool = False,
                 ssh_override: str | None = None,
                 on_host: str | None = None) -> int:
    """1 候補を 検証 → 実測 → 判定 → 提示 まで通す。終了コード 0 = 提案まで完了（合否は提案に載る）。"""
    model, role = candidate["model"], candidate["role"]
    print(f"== 候補 {model}（役割 {role}）==")

    spec = verify_candidate(model, candidate["hf_repo"])
    print(f"検証: 重み {spec.weights_gb}GB / blob {spec.blob_total_gb}GB / ctx {spec.hf_context} "
          f"/ license {spec.license} / vision {spec.vision}")
    for e in spec.errors:
        print(f"  検証エラー: {e}")

    if skip_measure:
        print("実測をスキップしました（--skip-measure）。提案は出せません。")
        return 1

    candidate = dict(candidate, _verified_weights_gb=spec.weights_gb)
    meas = measure_candidate(candidate, capacity, watch, ssh_override=ssh_override,
                             on_host=on_host)
    for line in meas.log:
        print(f"  {line}")
    print(f"実測: 実常駐 {meas.size_gb:g}GB / num_ctx {meas.context} / 現行復帰 {meas.restored}")
    if not meas.restored:
        print("⚠️ 現行モデルの常駐復帰を確認できませんでした。host の ollama ps を確認してください。")

    proposal = evaluate_swap(capacity, role, model, meas.size_gb,
                             rationale=candidate.get("rationale", ""))
    print(format_proposal(proposal))

    if skip_propose:
        print("提案の記録・提示をスキップしました（--no-propose。判定結果は上記のとおり）。")
        return 0
    request_id = propose(proposal, spec, meas, watch, send_slack=send_slack)
    print(f"提案を記録しました: request_id={request_id}（Slack 送信: {'あり' if send_slack else 'なし'}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="モデル自動監視パイプライン（設計書 §7.4）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="ウォッチリストの候補を 検証→実測→判定→提示 まで実行")
    p_run.add_argument("--candidate", help="対象を 1 候補（ollama タグ）に絞る")
    p_run.add_argument("--no-slack", action="store_true", help="Slack 送信を行わない（記録と標準出力のみ）")
    p_run.add_argument("--skip-measure", action="store_true", help="検証段のみ実行（ネットワークのみ・実機に触れない）")
    p_run.add_argument("--no-propose", action="store_true",
                       help="判定まで実行し、提案の記録・提示をしない（別マシンからの dry 実行用。"
                            "承認ファイル・提案 JSON は本番配置の mac-mini でのみ書く）")
    p_run.add_argument("--ssh", help="実測 host への SSH エイリアスを一時上書き（別マシンから実行する場合）")
    p_run.add_argument("--on-host", help="model_watch を実行している稼働機名（model_capacity.yaml の hosts キー）。"
                                         "ssh 未設定の host をローカル実行するにはこの宣言が必須（誤実機ガード）")

    p_st = sub.add_parser("status", help="提案の決着状況を一覧（承認ファイルの status を読む）")

    p_cl = sub.add_parser("cleanup", help="却下済み候補をディスクから撤去（ollama rm）")
    p_cl.add_argument("--candidate", required=True)
    p_cl.add_argument("--ssh", help="対象 host への SSH エイリアスを一時上書き")
    p_cl.add_argument("--on-host", help="run と同じ誤実機ガード用の実行機宣言")

    args = parser.parse_args(argv)
    watch_cfg = load_watch()
    watch = watch_cfg["watch"]
    capacity = load_capacity(DEFAULT_CAPACITY_PATH)

    if args.cmd == "run":
        targets = [c for c in watch_cfg["candidates"]
                   if not args.candidate or c["model"] == args.candidate]
        if not targets:
            print(f"ウォッチリストに候補がありません: {args.candidate or '(全件)'}")
            return 1
        rc = 0
        for cand in targets:
            try:
                rc |= run_pipeline(cand, capacity, watch, send_slack=not args.no_slack,
                                   skip_measure=args.skip_measure,
                                   skip_propose=args.no_propose, ssh_override=args.ssh,
                                   on_host=args.on_host)
            except Exception as e:  # noqa: BLE001 — 1 候補の失敗で他候補を止めない
                print(f"候補 {cand['model']} の処理に失敗: {e}")
                rc = 1
        return rc

    if args.cmd == "status":
        found = False
        for name in sorted(os.listdir(APPROVAL_DIR)) if os.path.isdir(APPROVAL_DIR) else []:
            if not (name.startswith("model-swap-") and name.endswith(".json")):
                continue
            with open(os.path.join(APPROVAL_DIR, name), encoding="utf-8") as f:
                rec = json.load(f)
            print(f"{rec['request_id']}: {rec['status']}  {rec.get('command', '')}"
                  f"  decided_by={rec.get('decided_by', '-')}")
            found = True
        if not found:
            print("未決着の model-swap 提案はありません。")
        return 0

    if args.cmd == "cleanup":
        # 対象候補の役割から host を引く（ウォッチリストに無い候補は撤去対象を特定できないため拒否）
        cand = next((c for c in watch_cfg["candidates"] if c["model"] == args.candidate), None)
        if not cand:
            print(f"ウォッチリストに無い候補です: {args.candidate}")
            return 1
        if is_production_model(args.candidate, capacity):
            print(f"拒否: {args.candidate} は現行の本番モデル（model_capacity.yaml roles）。"
                  f"cleanup は却下済み候補の撤去専用で、本番モデルの削除には使えない")
            return 1
        host = capacity["roles"][cand["role"]]["host"]
        host_cfg = watch["hosts"][host]
        runner = _runner_for_host(host, host_cfg, args.ssh, args.on_host)
        rc, out = runner.run(f"{runner.ollama} rm {args.candidate}", 120)
        print(out or f"rm rc={rc}")
        return rc

    return 2


if __name__ == "__main__":
    sys.exit(main())
