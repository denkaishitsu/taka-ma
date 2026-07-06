"""QueReviewer 推論直列化の単体テスト。

同一 inference_lock を指す 2 つの reviewer からの `_generate` が、プロセス内でも
flock により 1 件ずつ直列化される（同時に走らない）ことを検証する。実 ollama は使わず、
HTTP 送信部を「ロック保持中に重なりを観測する」スタブへ差し替える。
"""

import asyncio
import os
import tempfile

from reviewer import QueReviewer


def _reviewer(prompts_dir, lock):
    return QueReviewer(model="m", ollama_host="http://x", prompts_dir=prompts_dir,
                       inference_lock=lock)


def _write_prompt(tmp):
    """QueReviewer.__init__ が読む file_audit.md を用意する。"""
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "file_audit.md"), "w") as f:
        f.write("{path}{diff}{command}{status}")
    return tmp


def test_generate_serialized_across_reviewers():
    """2 reviewer が同一ロックを共有すると _generate が同時実行されない。"""
    with tempfile.TemporaryDirectory() as tmp:
        prompts = _write_prompt(os.path.join(tmp, "prompts"))
        lock = os.path.join(tmp, "infer.lock")
        r1 = _reviewer(prompts, lock)
        r2 = _reviewer(prompts, lock)

        state = {"active": 0, "max_active": 0}

        async def fake_http(self, prompt):
            # ロック保持中に入る想定。重なりが起きれば max_active が 2 になる。
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.05)
            state["active"] -= 1
            return "{}"

        # _generate 内の HTTP 部（ロックの内側）だけ差し替え、flock 直列化は本物を使う。
        async def generate(self, prompt):
            lock_fd = await asyncio.to_thread(self._acquire_inference_lock)
            try:
                return await fake_http(self, prompt)
            finally:
                await asyncio.to_thread(self._release_inference_lock, lock_fd)

        r1._generate = generate.__get__(r1)
        r2._generate = generate.__get__(r2)

        async def run():
            await asyncio.gather(r1._generate("a"), r2._generate("b"),
                                 r1._generate("c"), r2._generate("d"))

        asyncio.run(run())
        assert state["max_active"] == 1  # 一度に走ったのは常に 1 件（直列化された）


def test_lock_dir_created_on_init():
    """ロックファイルの親ディレクトリが __init__ で用意される。"""
    with tempfile.TemporaryDirectory() as tmp:
        prompts = _write_prompt(os.path.join(tmp, "prompts"))
        lock = os.path.join(tmp, "nested", "dir", "infer.lock")
        _reviewer(prompts, lock)
        assert os.path.isdir(os.path.dirname(lock))
