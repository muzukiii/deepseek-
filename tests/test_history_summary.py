"""前情滚动摘要的单元测试：阈值触发、增量合并、失败保持不变、摘要行读写与渲染。

无网络、无 API key：AI 调用一律打桩，数据库指向临时文件。
仓库没装 pytest，所以带 __main__ 兜底运行器：python tests/test_history_summary.py
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import threading
import types
from pathlib import Path

from astrabot.chat_service import ai_client, history_manager as hm
from astrabot.chat_service import history_summarizer as hs
from astrabot.chat_service.prompt_builder import build_summary_prompt, format_history

GROUP = 12345
KEEP = 25


def make_config(**over) -> types.SimpleNamespace:
    base = dict(
        summary_enabled=True,
        summary_trigger_messages=40,
        summary_batch_messages=60,
        summary_max_chars=600,
        summary_max_tokens=512,
        history_messages=KEEP,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def fresh_db() -> None:
    """把 history_manager 指到一个全新的临时库，绕开真实 data/。

    每个用例都用独立目录：Windows 下 SQLite 文件只要还有连接就删不掉，
    复用同一个路径会让后续 unlink() 报 WinError 32。
    """
    tmp = Path(tempfile.mkdtemp(prefix="astrabot_summary_test_"))
    hm.DATA_DIR = tmp
    hm.DB_PATH = tmp / "history.db"
    hm._local = threading.local()
    hm._db_initialized = False


async def seed(group_id: int, start: int, n: int) -> None:
    for i in range(start, start + n):
        await hm.append_message(group_id, {
            "time": 1_000_000 + i,
            "message_id": i,
            "user_id": str(1000 + i % 3),
            "user_name": f"群友{i % 3}",
            "message": f"消息{i:03d}",
            "msg_type": "user",
        })


def row_id_of(msg_text: str, group_id: int = GROUP) -> int:
    """水位线存的是 messages.id，而摘要行自己也占一个 AUTOINCREMENT 号，
    所以行 id 与"消息NNN"的序号会错开。断言一律按行 id 来，别写死数字。
    """
    conn = hm._get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM messages WHERE group_id = ? AND message = ? ORDER BY id DESC LIMIT 1",
        (group_id, msg_text),
    ).fetchone()
    assert row is not None, msg_text
    return int(row["id"])


class Stub:
    """记录每次收到的 prompt，并返回预设摘要文本。"""

    def __init__(self, reply: str = "群友们在讨论周末去哪爬山，最终定了武功山。"):
        self.reply = reply
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, max_tokens: int) -> str:
        self.prompts.append(prompt)
        return self.reply


def patch_stub(stub: Stub):
    original = ai_client.generate_summary
    ai_client.generate_summary = stub

    def restore() -> None:
        ai_client.generate_summary = original

    return restore


# ---------------------------------------------------------------------------

def test_below_trigger_is_noop():
    fresh_db()
    stub = Stub()
    restore = patch_stub(stub)
    try:
        async def run():
            await seed(GROUP, 1, 40)  # 窗口外只有 15 条，不到 40
            return await hs.maybe_summarize(GROUP, KEEP, make_config())

        changed = asyncio.run(run())
        assert changed is False, changed
        assert stub.prompts == [], "未达阈值却调用了 AI"
        text, wm = asyncio.run(hm.get_mid_term(GROUP))
        assert text == "" and wm == 0
    finally:
        restore()


def test_window_not_full_has_no_boundary():
    fresh_db()
    boundary = asyncio.run(hm.context_window_boundary(GROUP, KEEP))
    assert boundary is None, "空群不该有压缩边界"
    asyncio.run(seed(GROUP, 1, 10))
    boundary = asyncio.run(hm.context_window_boundary(GROUP, KEEP))
    assert boundary is None, "窗口未填满就不该压缩"


def test_first_summarize_writes_single_summary():
    fresh_db()
    stub = Stub()
    restore = patch_stub(stub)
    try:
        async def run():
            await seed(GROUP, 1, 70)
            ok = await hs.maybe_summarize(GROUP, KEEP, make_config())
            text, wm = await hm.get_mid_term(GROUP)
            rows = await hm.get_messages_by_type(GROUP, hm.MID_TERM_TYPE, limit=10)
            return ok, text, wm, rows

        ok, text, wm, rows = asyncio.run(run())
        assert ok is True
        assert text == stub.reply
        assert wm == row_id_of("消息045"), wm  # 70 条留 25 条窗口 → 只压到 消息045
        assert len(rows) == 1, rows
        assert len(stub.prompts) == 1
        assert "消息001" in stub.prompts[0] and "消息045" in stub.prompts[0]
        assert "消息046" not in stub.prompts[0], "窗口内的消息被一起压缩了"
    finally:
        restore()


def test_incremental_merge_only_compresses_delta():
    fresh_db()
    first = "群友们在讨论周末去哪爬山，最终定了武功山。"
    stub = Stub(reply=first)
    restore = patch_stub(stub)
    try:
        async def run():
            await seed(GROUP, 1, 70)
            assert await hs.maybe_summarize(GROUP, KEEP, make_config())
            await seed(GROUP, 71, 45)
            stub.reply = "合并后：爬山话题已定武功山，随后又争论起谁带帐篷。"
            ok = await hs.maybe_summarize(GROUP, KEEP, make_config())
            text, wm = await hm.get_mid_term(GROUP)
            rows = await hm.get_messages_by_type(GROUP, hm.MID_TERM_TYPE, limit=10)
            return ok, text, wm, len(rows), len(stub.prompts), stub.prompts[-1]

        ok, text, wm, n_rows, n_calls, last_prompt = asyncio.run(run())
        assert ok is True and n_calls == 2
        assert text == "合并后：爬山话题已定武功山，随后又争论起谁带帐篷。"
        assert wm == row_id_of("消息090"), wm
        assert "消息091" not in last_prompt, "最近窗口边缘的消息被压进去了"
        assert n_rows == 1, "滚动摘要必须是单条覆盖，而不是不断追加"
        assert first in last_prompt, "合并时没带上旧摘要"
        assert "消息071" in last_prompt and "消息090" in last_prompt
        assert "消息045" not in last_prompt, "已压缩过的消息被重复压缩"
    finally:
        restore()


def test_empty_ai_keeps_watermark():
    fresh_db()
    restore = patch_stub(Stub(reply="第一次成功"))
    try:
        async def run():
            await seed(GROUP, 1, 70)
            assert await hs.maybe_summarize(GROUP, KEEP, make_config())
            await seed(GROUP, 71, 45)
        asyncio.run(run())
    finally:
        restore()

    restore2 = patch_stub(Stub(reply="   "))
    try:
        async def run2():
            ok = await hs.maybe_summarize(GROUP, KEEP, make_config())
            text, wm = await hm.get_mid_term(GROUP)
            return ok, text, wm

        ok, text, wm = asyncio.run(run2())
        assert ok is False, "AI 返回空白却报告成功"
        assert text == "第一次成功" and wm == row_id_of("消息045"), (text, wm)
    finally:
        restore2()


def test_ai_exception_is_swallowed():
    fresh_db()

    async def boom(prompt: str, max_tokens: int) -> str:
        raise RuntimeError("provider down")

    original = ai_client.generate_summary
    ai_client.generate_summary = boom
    try:
        async def run():
            await seed(GROUP, 1, 70)
            return await hs.maybe_summarize(GROUP, KEEP, make_config())

        assert asyncio.run(run()) is False
    finally:
        ai_client.generate_summary = original


def test_disabled_switch():
    fresh_db()
    stub = Stub()
    restore = patch_stub(stub)
    try:
        async def run():
            await seed(GROUP, 1, 70)
            hs.schedule_summarize(GROUP, KEEP, make_config(summary_enabled=False))
            await asyncio.sleep(0.02)

        asyncio.run(run())
        assert stub.prompts == [], "summary_enabled=false 仍然跑了摘要"
    finally:
        restore()


def test_inflight_dedupe():
    fresh_db()
    calls = []

    async def slow(prompt: str, max_tokens: int) -> str:
        calls.append(prompt)
        await asyncio.sleep(0.05)
        return "同一批只该压一次"

    original = ai_client.generate_summary
    ai_client.generate_summary = slow
    try:
        async def run():
            await seed(GROUP, 1, 70)
            return await asyncio.gather(
                hs.maybe_summarize(GROUP, KEEP, make_config()),
                hs.maybe_summarize(GROUP, KEEP, make_config()),
            )

        results = asyncio.run(run())
        assert len(calls) == 1, calls
        assert sorted(results) == [False, True], results
    finally:
        ai_client.generate_summary = original


def test_trim_history_keeps_summary_row():
    fresh_db()

    async def run():
        await seed(GROUP, 1, 70)
        await hm.set_mid_term_memory(GROUP, "摘要内容", watermark=45, when=1_000_045)
        await hm.trim_history(GROUP, max_lines=3)
        text, wm = await hm.get_mid_term(GROUP)
        rows = await hm.get_messages_by_type(GROUP, hm.MID_TERM_TYPE, limit=5)
        kept = await hm.get_messages_by_type(GROUP, "user", limit=100)
        return text, wm, len(rows), len(kept)

    text, wm, n_summary, n_recent = asyncio.run(run())
    assert text == "摘要内容" and wm == 45
    assert n_summary == 1, "trim 把摘要行裁掉了"
    assert n_recent == 3, n_recent


def test_summary_lands_before_recent_window():
    fresh_db()

    async def run():
        await seed(GROUP, 1, 70)
        await hm.set_mid_term_memory(GROUP, "前情：他们约好了武功山", watermark=45, when=1_000_045)
        return await hm.select_context(GROUP, max_messages=KEEP)

    ctx = asyncio.run(run())
    kinds = [m.get("msg_type") for m in ctx]
    assert "mid_term_memory" in kinds
    idx = kinds.index("mid_term_memory")
    # 摘要的时间是被压缩批次的末尾，必须排在逐字窗口之前
    assert idx == 0, idx
    assert kinds[idx + 1] == "user"
    rendered = format_history(ctx)
    assert "【前情提要" in rendered
    assert "前情：他们约好了武功山" in rendered
    assert "消息046" in rendered, "窗口内的逐字历史丢了"


def test_build_summary_prompt_shape():
    p = build_summary_prompt(old_summary="旧摘要文本", history_text="群友0(1000)：消息001", max_chars=600)
    assert "旧摘要文本" in p
    assert "群友0(1000)：消息001" in p
    assert "600" in p
    assert "不要写成对话稿" in p


def test_clean_strips_fences_and_caps_length():
    cfg = make_config(summary_max_chars=20)
    assert hs._clean("```\n摘要正文\n```", 20) == "摘要正文"
    assert hs._clean("“摘要  正文”", 20) == "摘要 正文"
    long = hs._clean("一二三四五六七八九十" * 5, 20)
    assert len(long) == 21 and long.endswith("…"), long  # 20 字 + 省略号
    assert hs._clean("", 20) == ""
    assert hs._clean("```json\n{\n  \"a\": 1\n}```", 60) == '{\n"a": 1\n}'
    assert cfg.summary_max_chars == 20


def test_provider_chain_reused_by_summary():
    def cfg(tier2: bool, tier3: bool) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            API_PROTOCOL="OPENAI", API_BASE_URL="https://a/v1", API_KEY="k1", API_MODEL="m1",
            reasoning_effort="low",
            BACK_API_PROTOCOL="OPENAI", BACK_API_BASE_URL="https://b/v1",
            BACK_API_KEY="k2" if tier2 else "", BACK_API_MODEL="m2" if tier2 else "",
            back_reasoning_effort="low",
            THIRD_API_PROTOCOL="OPENAI", THIRD_API_BASE_URL="https://c/v1",
            THIRD_API_KEY="k3" if tier3 else "", THIRD_API_MODEL="m3" if tier3 else "",
            third_reasoning_effort="",
        )

    assert len(ai_client._provider_chain(cfg(False, False))) == 1
    chain = ai_client._provider_chain(cfg(True, True))
    assert [c[3] for c in chain] == ["m1", "m2", "m3"], chain
    assert chain[0][4] == "low" and chain[2][4] == "", chain


def test_generate_summary_falls_back_and_tags_usage_kind():
    seen: list[dict] = []

    async def fake_chat(**kw) -> dict:
        seen.append(kw)
        if kw["model"] == "m1":
            raise RuntimeError("tier1 down")
        return {"content": "  来自二档的摘要  "}

    original_chat = ai_client.chat
    original_config = ai_client.get_config
    ai_client.chat = fake_chat
    ai_client.get_config = lambda: types.SimpleNamespace(
        API_PROTOCOL="OPENAI", API_BASE_URL="https://a/v1", API_KEY="k1", API_MODEL="m1",
        reasoning_effort="low",
        BACK_API_PROTOCOL="OPENAI", BACK_API_BASE_URL="https://b/v1", BACK_API_KEY="k2",
        BACK_API_MODEL="m2", back_reasoning_effort="low",
        THIRD_API_KEY="", THIRD_API_MODEL="", THIRD_API_BASE_URL="", third_reasoning_effort="",
    )
    try:
        out = asyncio.run(ai_client.generate_summary("p", max_tokens=512))
        assert out == "来自二档的摘要", out
        assert len(seen) == 2, seen
        assert all(s["usage_kind"] == "summary" for s in seen), seen
        assert all(s.get("response_format") is None for s in seen), "摘要不该要求 JSON 返回"
        assert seen[1]["reasoning_effort"] == "low"
    finally:
        ai_client.chat = original_chat
        ai_client.get_config = original_config


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
