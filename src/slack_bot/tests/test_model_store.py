"""model_store 単体テスト — ya-ta.yaml の models をコメント保全したまま編集する。

実物の src/ai_gateway/config/ya-ta.yaml を一時ファイルへ複製し、それに対して add/update/
remove を行う。狙い: (1) CRUD が正しい (2) 手書きコメント・他セクションを壊さない
（safe_dump 退行の検出）。

構築手順書: docs/procedures/03-slack-bot.md（モデル管理）
"""

import os
from pathlib import Path

import pytest
import yaml

from services import model_store

# 実物の ya-ta.yaml（repo ルート基準）。tests/ から見て ../../../。
_REAL_YATA = Path(__file__).resolve().parents[3] / "src/ai_gateway/config/ya-ta.yaml"


@pytest.fixture
def yata_file(tmp_path, monkeypatch):
    """実物 ya-ta.yaml を複製した一時ファイルを編集対象に割り当てる。"""
    path = tmp_path / "ya-ta.yaml"
    path.write_text(_REAL_YATA.read_text())
    monkeypatch.setenv("TAKA_MA_YATA_PATH", str(path))
    return path


def test_load_models(yata_file):
    models = model_store.load_models()
    # 実物の登録（opus / sonnet / haiku / gemini / gemma 等）が読める
    assert "opus" in models and "gemma" in models
    assert models["gemma"]["type"] == "local"
    assert models["gemma"]["model_id"] == "gemma4:31b"


def test_add_local_model(yata_file):
    model_store.add_model("llama", {
        "full_name": "llama-4-8b", "type": "local", "command": "ollama",
        "methods": ["subprocess"], "model_id": "llama4:8b",
        "capabilities": ["light-task"], "description": "テスト追加",
    })
    models = model_store.load_models()
    assert models["llama"]["type"] == "local"
    assert models["llama"]["model_id"] == "llama4:8b"
    assert models["llama"]["methods"] == ["subprocess"]
    # 既存モデルは無傷（モデル版に依存しないよう存在と型のみ検証）
    assert "opus" in models
    assert models["opus"]["type"] == "api"


def test_add_api_model_with_flag(yata_file):
    model_store.add_model("opus47", {
        "full_name": "claude-opus-4.7", "type": "api", "vendor": "anthropic",
        "command": "claude", "methods": ["pty"], "model_flag": "--model opus-4.7",
    })
    models = model_store.load_models()
    # 値に空白・-- を含む model_flag が YAML として正しく往復する
    assert models["opus47"]["model_flag"] == "--model opus-4.7"
    assert models["opus47"]["vendor"] == "anthropic"


def test_add_duplicate_raises(yata_file):
    with pytest.raises(ValueError):
        model_store.add_model("opus", {
            "full_name": "x", "type": "api", "command": "claude", "methods": ["pty"]})


def test_add_missing_required_raises(yata_file):
    with pytest.raises(ValueError):
        model_store.add_model("x", {"type": "api"})   # full_name / command 欠落


def test_update_existing_field(yata_file):
    model_store.update_model("sonnet", {"model_flag": "--model sonnet-4.6-latest"})
    models = model_store.load_models()
    assert models["sonnet"]["model_flag"] == "--model sonnet-4.6-latest"
    # 他フィールドは保持
    assert models["sonnet"]["full_name"] == "claude-sonnet-4.6"


def test_update_inserts_new_field(yata_file):
    # haiku に description は元々あるが、無いフィールド（model_id）を挿入できる
    model_store.update_model("haiku", {"model_id": "haiku-local:1"})
    assert model_store.load_models()["haiku"]["model_id"] == "haiku-local:1"


def test_update_missing_raises(yata_file):
    with pytest.raises(ValueError):
        model_store.update_model("nope", {"vendor": "x"})


def test_remove_model(yata_file):
    model_store.remove_model("haiku")
    models = model_store.load_models()
    assert "haiku" not in models
    # 隣接エントリは残る
    assert "sonnet" in models and "gemini" in models


def test_remove_missing_raises(yata_file):
    with pytest.raises(ValueError):
        model_store.remove_model("nope")


def test_comments_and_other_sections_preserved(yata_file):
    """add→remove 後も手書きコメント・他トップレベルセクションが無傷であること。"""
    model_store.add_model("tmp", {
        "full_name": "t", "type": "local", "command": "ollama",
        "methods": ["subprocess"], "model_id": "t:1"})
    model_store.remove_model("tmp")
    text = yata_file.read_text()
    # OOM 回避の num_ctx コメント（safe_dump なら消える）
    assert "OOM" in text or "num_ctx" in text
    # 外部サービス追加例（suno）のコメントブロック
    assert "suno" in text
    # 他トップレベルセクションが値ごと保持されている
    data = yaml.safe_load(text)
    assert data["routing"]["category_defaults"]["heavy"][0] == "opus"
    assert data["concurrency"]["max_heavy_instances"] == 3
    assert data["ya-ta"]["num_ctx"] == 32768


def test_add_requires_methods(yata_file):
    # methods はルーティングの起動経路を決める核情報のため必須（usage 表記と一致）
    with pytest.raises(ValueError):
        model_store.add_model("x", {
            "full_name": "x", "type": "api", "command": "claude"})   # methods 欠落


def test_update_inserts_field_in_canonical_order(yata_file):
    # gemini に model_id を update 挿入 → _FIELD_ORDER 準拠で command の後・capabilities の前
    model_store.update_model("gemini", {"model_id": "gemini-local:1"})
    text = yata_file.read_text()
    block = text.split("  gemini:", 1)[1].split("\n  gemma:", 1)[0]
    assert block.index("command:") < block.index("model_id:") < block.index("capabilities:")
    # 値も往復する
    assert model_store.load_models()["gemini"]["model_id"] == "gemini-local:1"


def test_remove_last_model_keeps_comment_example(yata_file):
    # 末尾エントリ(gemma)削除でコメント例ブロック(suno)を巻き込まない
    model_store.remove_model("gemma")
    text = yata_file.read_text()
    assert "gemma" not in model_store.load_models()
    assert "suno" in text                       # コメント例は保持
    assert "外部サービス" in text


def test_write_is_atomic_no_tmp_left(yata_file):
    model_store.add_model("x", {
        "full_name": "x", "type": "local", "command": "ollama",
        "methods": ["subprocess"], "model_id": "x:1"})
    # 一時ファイル(.tmp)を残さない
    leftovers = [p for p in os.listdir(yata_file.parent) if p.endswith(".tmp")]
    assert leftovers == []


# --- Task #89 (M9): 壊れた ya-ta.yaml の YAMLError を ValueError に正規化 ---

def test_load_models_broken_yaml_raises_valueerror(tmp_path, monkeypatch):
    path = tmp_path / "ya-ta.yaml"
    path.write_text("models:\n  a: [unclosed\n")   # 不正 YAML
    monkeypatch.setenv("TAKA_MA_YATA_PATH", str(path))
    with pytest.raises(ValueError, match="解析に失敗"):
        model_store.load_models()
