"""タスク実行モデル（worker LLM）デプロイ — MBP 上の 3 モデル統合配備。

構築手順書: docs/procedures/06-task-models.md（Pyinfra対応）

配備対象(2024-06-12時点):
- Claude Code（Opus 4.8、heavy 主軸、PTY 経路、起動コマンド: claude）
- Antigravity CLI（Gemini 3.1 Pro、PTY/subprocess 両対応、起動コマンド: agy）
- Gemma 4 31B（light、ollama、subprocess、`ollama run gemma4:31b`）

認証はサブスク + 初回 OAuth（API キー発行は不要）。詳細は構築手順書 Step 3 参照。

設計書 §1.3 / §2.3-2.5 / §7 / §8.4.x / §8.5-8.7 参照。
"""

import os
import sys
from pathlib import Path

import yaml
from pyinfra.operations import brew, files, server

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _manifest import record  # noqa: E402

# Step 2-1: Node.js（Claude Code / Antigravity CLI の前提）
brew.packages(packages=["node"], present=True)
record("task-models", "brew.packages node", "node",
       {"op": "skip", "reason": "shared homebrew package (node)"})

# Step 2-2: Claude Code インストール（heavy 主軸、起動コマンド: claude）
server.shell(commands=["npm install -g @anthropic-ai/claude-code"])
record("task-models", "npm install -g @anthropic-ai/claude-code",
       "@anthropic-ai/claude-code",
       {"op": "skip",
        "reason": "shared global npm CLI (claude-code) — 撤去禁止（実行基盤・他PRJ依存）"})

# Step 2-3: Antigravity CLI インストール（PTY/subprocess 両対応、起動コマンド: agy）
# 公式インストール（binary は ~/.local/bin/agy に配置。SSH 非対話シェルの PATH に注意）
server.shell(commands=["curl -fsSL https://antigravity.google/cli/install.sh | bash"])
record("task-models", "install agy (Antigravity CLI)",
       "agy",
       {"op": "skip",
        "reason": "shared global CLI (agy) — 撤去禁止（実行基盤・他PRJ依存）"})

# Step 2-3b: worker 用 tmux サーバ（GUI セッション起源）の launchd 常駐
# agy の認証は macOS keychain 管理で、SSH セッションからは keychain を読めない
# （実測 2026-07-03: 素の SSH=認証失敗、GUI 起源 tmux 経由=成功）。
# GUI ログイン時に tmux サーバを起動しておき、SSH からの worker CLI 実行は
# このサーバ内で行う（設計書 §8.6 / 構築手順書 06 Step 2 参照）。
_home = os.path.expanduser("~")
files.template(
    src="pyinfra/templates/com.taka-ma.worker-tmux.plist.j2",
    dest=f"{_home}/Library/LaunchAgents/com.taka-ma.worker-tmux.plist",
    home=_home,
)
server.shell(commands=[
    # bootout は XPC 経由で非同期に片付くため、直後に bootstrap すると解体未完了の
    # ジョブと衝突し "Bootstrap failed: 5: Input/output error" になることを qu-e の
    # 同一パターンで実機再現（#68 E2E T07）。固定 sleep 1 でも足りない場合が実機で
    # あったため、bootstrap 成功まで最大 5 回・1 秒間隔でリトライする（リトライ上限
    # 到達時は exit 1 して pyinfra へ失敗を伝播する。until をそのまま抜けると exit 0
    # になり偽成功を報告してしまうため）。
    "launchctl bootout gui/$(id -u)/com.taka-ma.worker-tmux 2>/dev/null; "
    "i=0; until launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.taka-ma.worker-tmux.plist; do "
    "i=$((i+1)); if [ $i -ge 5 ]; then exit 1; fi; sleep 1; done",
])
record("task-models", "launchd com.taka-ma.worker-tmux", "com.taka-ma.worker-tmux",
       {"op": "launchctl.bootout", "label": "com.taka-ma.worker-tmux"})

# Step 2-4: Gemma 4 31B（light 軽量タスク、約 20GB、Q4_K_M）
# モデル名は ya-ta.yaml の gemma 登録（model_id）を単一ソースとして参照。
# 来歴は component="models" で記録（host グローバルな ollama rm のため、
# 複数 deploy が同一モデルを宣言しても upsert で 1 レコードに集約）。
_gemma_model_id = yaml.safe_load(Path("src/ai_gateway/config/ya-ta.yaml").read_text())["models"]["gemma"]["model_id"]
server.shell(commands=[f"ollama pull {_gemma_model_id}"])
record("models", f"ollama pull {_gemma_model_id}", _gemma_model_id,
       {"op": "ollama.rm", "model": _gemma_model_id})

# Step 2-5: 配備確認（version / list）
server.shell(commands=[
    "node --version",
    "claude --version || true",        # 初回は OAuth 未実施でも version は返る
    "PATH=$HOME/.local/bin:$PATH agy --version || true",
    "ollama list | grep gemma4 || true",
])
