"""戳一戳处理：被戳时生成一句符合人设的回应。

与群消息不同，戳一戳没有消息文本，因此这里构造一个轻量的上下文
直接调用 AI，而不是走完整的 handle_group_message 流程（后者依赖
GroupMessageEvent 的诸多字段）。
"""

from __future__ import annotations

import asyncio
import re
import time

from nonebot.adapters.onebot.v11 import Bot, MessageSegment

from astrabot.chat_service.ai_client import generate_reply
from astrabot.chat_service.history_manager import (
    append_message,
    select_context,
)
from astrabot.chat_service.message_handler import (
    _append_with_at,
    _get_group_name_map,
    resolve_plain_at,
)
from astrabot.chat_service.prompt_builder import format_history
from astrabot.config import get_config
from astrabot.logmanager.logger import logger

# 同一群短时间内被反复戳时的冷却，避免刷屏
_POKE_COOLDOWN = 3.0
_last_poke: dict[int, float] = {}


def _build_poke_prompt(user_name: str, user_id: str, history_text: str) -> str:
    config = get_config()
    return (
        f"你的名字是{config.name_cn}，你的设定是{config.person_setting}。\n\n"
        f"你需要按照如下的格式输出：\n{config.output_style}\n\n"
        f"你的说话风格如下：\n{config.reply_style}\n\n"
        f"除此之外，有一些额外的要求需要你注意：\n{config.extra_style}\n\n"
        "这是最近的聊天记录：\n"
        f"{history_text}\n\n"
        f"{user_name}({user_id}) 刚刚戳了戳你（戳一戳），没有附带任何文字。"
        " <--对方戳了你，准备回应"
    )


async def _safe_nickname(bot: Bot, group_id, user_id) -> str:
    if not group_id:
        return str(user_id)
    try:
        info = await bot.get_group_member_info(
            group_id=int(group_id), user_id=int(user_id)
        )
        return info.get("card") or info.get("nickname") or str(user_id)
    except Exception:
        return str(user_id)


async def handle_poke(bot: Bot, data: dict) -> None:
    """被戳后：回戳对方，并发一句符合人设的回应。"""
    group_id = data.get("group_id")
    user_id = data.get("user_id")
    target_id = data.get("target_id")

    # 只响应"戳机器人自己"，且不响应机器人自己发起的戳
    if str(target_id) != str(bot.self_id) or str(user_id) == str(bot.self_id):
        return

    config = get_config()

    if group_id and config.enabled_groups and int(group_id) not in config.enabled_groups:
        return

    # 冷却：同一群短时间内只响应一次，避免连点刷屏
    if group_id:
        now = time.time()
        last = _last_poke.get(int(group_id), 0.0)
        if now - last < _POKE_COOLDOWN:
            logger.debug(f"Poke from {user_id} skipped (cooldown)")
            return
        _last_poke[int(group_id)] = now

    logger.info(f"Poked by {user_id} in group {group_id}")

    if not config.poke_reply:
        return

    # 先回戳对方
    if config.poke_back:
        try:
            if group_id:
                await bot.call_api(
                    "group_poke", group_id=int(group_id), user_id=int(user_id)
                )
            else:
                await bot.call_api("friend_poke", user_id=int(user_id))
        except Exception as e:
            logger.warning(f"Failed to poke back {user_id}: {e}")

    # 再生成一句回应
    user_name = await _safe_nickname(bot, group_id, user_id)
    history_text = "（暂无聊天记录）"
    if group_id:
        try:
            history = await select_context(int(group_id), max_messages=10)
            history_text = format_history(history)
        except Exception as e:
            logger.debug(f"Poke: failed to load history: {e}")

    prompt = _build_poke_prompt(user_name, user_id, history_text)

    reply_text = ""
    try:
        reply_text, _ = await asyncio.wait_for(generate_reply(prompt), timeout=60)
    except asyncio.TimeoutError:
        logger.warning("Poke reply timed out")
    except Exception as e:
        logger.error(f"Poke reply failed: {e}")

    if not reply_text:
        return

    # 与群消息一致地处理标记：
    #   普通「@昵称」 -> [at:QQ号]
    #   [/]         -> 换行
    # 最后由 _append_with_at 把 [at:QQ号] 变成真正的 @ 消息段。
    self_id = int(bot.self_id)
    if "@" in reply_text:
        name_to_qq = await _get_group_name_map(bot, group_id) if group_id else {}
        for own in (config.name_cn, config.name):
            if own:
                name_to_qq.pop(str(own).strip(), None)
        reply_text = resolve_plain_at(reply_text, name_to_qq)

    reply_text = re.sub(rf"\[at:{self_id}\]", "", reply_text)
    reply_text = "\n".join(
        part.strip() for part in reply_text.split("[/]") if part.strip()
    )
    reply_text = re.sub(r"[ \t]{2,}", " ", reply_text).strip()
    if not reply_text:
        return

    plain_for_history = re.sub(r"\[at:\d+\]", "", reply_text).strip()

    segments: list = []
    _append_with_at(segments, reply_text)
    if not segments:
        return

    try:
        if group_id:
            await bot.send_group_msg(group_id=int(group_id), message=segments)
            await append_message(
                int(group_id),
                {
                    "time": int(time.time()),
                    "user_id": str(bot.self_id),
                    "user_name": config.name_cn,
                    "message": plain_for_history,
                    "msg_type": "bot",
                    "images": None,
                },
            )
        else:
            await bot.send_private_msg(user_id=int(user_id), message=segments)
        logger.info(f"Replied to poke from {user_name}({user_id}): {reply_text[:50]}")
    except Exception as e:
        logger.error(f"Failed to send poke reply: {e}")
