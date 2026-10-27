"""记忆导出与"写记忆规矩"的回归测试：文件内容结构、上传失败回退、超长拒收。

背景：`/memory list` 内联发送时，一个群 388 条记忆会渲染成 23K 字（实测），
所以改成导出 txt 发群文件；同时给 memorize 数组补上"只记稳定事实"的约束，
否则模型会把每轮事件都写成叙事句。
纯打桩，不联网、不调 LLM、不碰真实 data/。
无 pytest 时 `python tests/test_memory_export.py` 直跑。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrabot.chat_service import memory_commands as mc
from astrabot.chat_service import message_handler as mh
from astrabot.chat_service import prompt_builder as pb

GROUP = 4242


def patch(target, name, value):
    old = getattr(target, name)
    setattr(target, name, value)
    return lambda: setattr(target, name, old)


def fake_long(rows):
    return lambda group_id, *a, **k: [
        {"id": i, "content": c, "created_at": ts, "created_by": None} for i, c, ts in rows
    ]


def run() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="astrabot_mem_export_"))
    restores = [patch(mc, "_EXPORT_DIR", tmp)]
    # 两条同一天、一条另一天；第 3 条超长用来验证截断
    rows = [
        (7, "点叔喜欢被叫点哥", 1793000000),
        (9, "小池是群里的 AI 助手", 1793000100),
        (12, "长" * 260, 1792000000),
    ]
    restores.append(patch(mc, "get_long_term_memories", fake_long(rows)))
    try:
        spec = mc.build_memory_export(GROUP, "long")
    finally:
        for r in restores:
            r()

    text = spec["path"].read_text(encoding="utf-8")
    assert spec["path"].exists() and spec["name"].startswith("长期记忆_"), spec["name"]
    assert "总数：3 条" in text, text[:200]
    assert "时间跨度：" in text and "按月：" in text, text[:300]
    assert "[2026-10" in text, "应按日期分组"
    assert "m7 点叔喜欢被叫点哥" in text and "m9 小池是群里的 AI 助手" in text, text
    assert ("长" * 200 + "…") in text and ("长" * 201) not in text, "单条应截断到 200 字"
    # 倒序：新日期在旧日期之前
    assert text.index("[2026-10") < text.index("[2026-09") if "[2026-09" in text else True
    assert "已导出长期记忆 3 条" in spec["caption"] and spec["name"] in spec["caption"], spec["caption"]
    assert spec["fallback"].count("\n") <= mc._INLINE_TOP + 1, "回退文本只列最近 20 条"
    print("PASS 长期记忆导出：统计头、日期分组、m 标签、单条截断、回退上限")

    # 事实记忆：f 标签 + 访问次数
    restores = []
    restores.append(patch(mc, "_EXPORT_DIR", tmp))
    restores.append(patch(mc, "get_recent_facts", lambda gid, limit=200: [
        {"id": 311, "fact": "兰山的昵称是点叔", "created_at": 1793000000, "access_count": 3},
    ]))
    try:
        spec_f = mc.build_memory_export(GROUP, "facts")
    finally:
        for r in restores:
            r()
    tf = spec_f["path"].read_text(encoding="utf-8")
    assert "f311 (访问3) 兰山的昵称是点叔" in tf, tf
    assert "最近 200 条" in tf, tf[:200]
    print("PASS 事实记忆导出：f 标签带访问次数")

    # 命令入口：列表→文件，其它命令→原样（spec 为 None）
    restores = []
    restores.append(patch(mc, "_EXPORT_DIR", tmp))
    restores.append(patch(mc, "get_long_term_memories", fake_long(rows)))
    try:
        handled, caption, spec = mc.handle_memory_command_with_export(GROUP, "1", "/memory list")
        assert handled and spec and spec["path"].exists(), (handled, caption, bool(spec))
        handled2, text2, spec2 = mc.handle_memory_command_with_export(GROUP, "1", "/memory help")
        assert handled2 and spec2 is None and "记忆管理命令" in text2, text2[:80]
    finally:
        for r in restores:
            r()
    print("PASS 命令入口：/memory list 出文件，其它命令原样返回")

    # 空库不该生成空文件
    restores = [patch(mc, "_EXPORT_DIR", tmp), patch(mc, "get_long_term_memories", lambda *a, **k: [])]
    try:
        handled, text, spec = mc.handle_memory_command_with_export(GROUP, "1", "/memory list")
        assert handled and spec is None and "还没有长期记忆" in text, (text, spec)
    finally:
        for r in restores:
            r()
    print("PASS 空记忆库不生成文件")

    # 上传失败 → 回退内联列表
    sent: list[str] = []

    class BoomBot:
        async def call_api(self, *a, **k):
            raise RuntimeError("bridge says no")

    async def fake_send(bot, event, group_id, text):
        sent.append(text)

    old_send = mh._send_reply
    mh._send_reply = fake_send
    try:
        asyncio.run(mh._send_command_result(
            BoomBot(), None, GROUP, "统计行",
            {"path": tmp / "x.txt", "name": "x.txt", "fallback": "回退列表"},
        ))
    finally:
        mh._send_reply = old_send
    assert sent == ["回退列表"], sent
    print("PASS 上传失败时改发回退列表")

    # 写记忆规矩进了 prompt
    cfg = types.SimpleNamespace(
        name="Astra", name_cn="阿斯特拉", person_setting="测试人设",
        output_style="输出 JSON", reply_style="自然", extra_style="无",
    )
    prompt = pb.build_main_prompt(cfg, "", "聊天记录", "触发行")
    assert pb._MEMORIZE_GUIDE in prompt, "写记忆的规矩应出现在 prompt 中"
    assert "不要放：一次性的对话事件" in prompt
    print("PASS 写记忆规矩已注入 prompt")

    # 超长 memorize 被拒收
    captured: list[str] = []
    from astrabot.chat_service import enhanced_memory as em

    old_add = em.add_long_term_memory
    em.add_long_term_memory = lambda gid, content, **k: captured.append(content) or True
    try:
        from astrabot.chat_service.memory_manager import process_memorize

        process_memorize(GROUP, ["正常的一条记忆", "长" * 200, "   ", "另一条正常记忆"])
    finally:
        em.add_long_term_memory = old_add
    assert captured == ["正常的一条记忆", "另一条正常记忆"], captured
    print("PASS 超过 120 字的 memorize 被拒收，空白条目跳过")


    # m/f 前缀精确删除；纯数字仍走"先长期、再事实"的旧顺序
    calls: list[tuple[str, int]] = []
    restores = [
        patch(mc, "delete_long_term_memory", lambda gid, i: calls.append(("m", i)) or (i == 7)),
        patch(mc, "delete_fact", lambda gid, i: calls.append(("f", i)) or (i == 311)),
    ]
    try:
        _, t = mc.handle_memory_command(GROUP, "1", "/memory delete m7")
        assert "已删除长期记忆 m7" in t, t
        _, t = mc.handle_memory_command(GROUP, "1", "/memory delete f311")
        assert "已删除事实记忆 f311" in t, t
        _, t = mc.handle_memory_command(GROUP, "1", "/memory delete m999")
        assert "找不到长期记忆 m999" in t, t
        calls.clear()
        _, t = mc.handle_memory_command(GROUP, "1", "/memory delete 7")
        assert calls[0] == ("m", 7), calls
        # 前缀形式必须仍被判定为破坏性，否则会绕过白名单那道门
        assert mc.is_destructive_command("/memory delete m12")
        assert mc.is_destructive_command("/memory delete f311")
        assert mc.is_destructive_command("/删除记忆 m3")
    finally:
        for r in restores:
            r()
    print("PASS m/f 前缀精确删除、纯数字兼容、前缀形式仍算破坏性命令")


def main() -> int:
    failures = 0
    try:
        run()
    except AssertionError as e:
        failures += 1
        print(f"FAIL {e}")
    except Exception as e:
        failures += 1
        import traceback

        print(f"FAIL unexpected: {type(e).__name__}: {e}")
        traceback.print_exc()
    print("1/1 passed" if not failures else "0/1 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
