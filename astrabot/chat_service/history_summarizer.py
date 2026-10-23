"""历史滚动摘要：把超出最近窗口的旧消息压成一条【前情提要】。

进 prompt 的成本由"塞进去多少条消息"决定，而有效记忆由"还记得多久之前"决定，
原本这两件事是绑死的：想让机器人记起更早的事，只能把历史拉得更长。这里把它们解开
——最近窗口逐字保留，窗口外的旧消息压缩成一条长度封顶的摘要并常驻上下文
（`select_context` 里 `mid_term_memory` 的 pinned 通道）。于是 prompt 规模基本恒定，
但可回溯的时间跨度能一直往后延伸，不再受模型窗口大小的限制。
"""

from __future__ import annotations

import asyncio
import re

from astrabot.chat_service import ai_client, history_manager
from astrabot.chat_service.prompt_builder import build_summary_prompt, format_history
from astrabot.logmanager.logger import logger

# 摘要请求明确要求"直接输出正文"，但模型偶尔仍会套一层 ``` 围栏
_FENCE = re.compile(r"^```[a-zA-Z]*\n?")
_FENCE_END = re.compile(r"\n?```$")

# create_task 的返回值必须持有引用，否则任务可能被垃圾回收后静默不执行
_BG_TASKS: set[asyncio.Task] = set()
# 同一个群同时只跑一个摘要任务：并发跑会让两次摘要读到同一批消息，后写的覆盖先写的
_RUNNING: set[int] = set()


def schedule_summarize(group_id: int, keep_messages: int, config) -> None:
    """把摘要投递到后台执行，绝不占用当前回复的耗时。"""
    if not getattr(config, "summary_enabled", False):
        return
    task = asyncio.create_task(maybe_summarize(group_id, keep_messages, config))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def maybe_summarize(group_id: int, keep_messages: int, config) -> bool:
    """压缩一批旧消息。返回是否真的写入了新摘要。"""
    if group_id in _RUNNING:
        return False
    _RUNNING.add(group_id)
    try:
        return await _compress_once(group_id, keep_messages, config)
    except Exception as e:
        logger.debug(f"[summary] group {group_id} skipped: {e}")
        return False
    finally:
        _RUNNING.discard(group_id)


async def _compress_once(group_id: int, keep_messages: int, config) -> bool:
    boundary = await history_manager.context_window_boundary(group_id, keep_messages)
    if boundary is None:
        return False

    old_text, watermark = await history_manager.get_mid_term(group_id)
    pending = await history_manager.count_compressible(group_id, watermark, boundary)
    if pending < config.summary_trigger_messages:
        return False

    chunk = await history_manager.get_compressible(
        group_id, watermark, boundary, config.summary_batch_messages
    )
    if not chunk:
        return False

    prompt = build_summary_prompt(
        old_summary=old_text,
        history_text=format_history(chunk),
        max_chars=config.summary_max_chars,
    )
    raw = await ai_client.generate_summary(prompt, max_tokens=config.summary_max_tokens)
    text = _clean(raw, config.summary_max_chars)
    if not text:
        # 失败就不动水位线：原始消息仍完整在库里，下一轮会重新尝试同一批
        logger.info(f"[summary] group {group_id}: 摘要为空，{pending} 条继续待压缩")
        return False

    last = chunk[-1]
    await history_manager.set_mid_term_memory(
        group_id, text, int(last["id"]), int(last.get("time") or 0)
    )
    logger.info(
        f"[summary] group {group_id}: 压缩 {len(chunk)} 条（水位→{last['id']}），"
        f"摘要 {len(old_text)}→{len(text)} 字"
    )
    return True


def _clean(text: str, max_chars: int) -> str:
    """剥掉模型偶尔加上的代码围栏/外层引号，压平空行，并做长度封顶。"""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("```"):
        t = _FENCE.sub("", t, count=1)
        t = _FENCE_END.sub("", t, count=1).strip()
    elif len(t) > 1 and t[0] in "\"“‘'" and t[-1] in "\"”’'":
        t = t[1:-1].strip()
    t = "\n".join(" ".join(ln.split()) for ln in t.splitlines() if ln.strip())
    if len(t) > max_chars:
        t = t[:max_chars].rstrip() + "…"
    return t
