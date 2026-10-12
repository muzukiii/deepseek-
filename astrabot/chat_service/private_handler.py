"""私聊消息处理：独立保存每个用户的上下文并生成私聊回复。"""

from __future__ import annotations

import time
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, PrivateMessageEvent

from astrabot.chat_service.ai_client import generate_reply
from astrabot.chat_service.history_manager import append_message, get_recent_messages, trim_history
from astrabot.chat_service.prompt_builder import build_main_prompt, format_history
from astrabot.chat_service.sticker_gallery import sticker_prompt_catalog, sticker_url
from astrabot.config import get_config
from astrabot.logmanager.logger import logger


async def handle_private_message(bot: Bot, event: PrivateMessageEvent):
    config = get_config()
    user_id = int(event.user_id)
    conversation_id = -abs(user_id)
    user_name = event.sender.card or event.sender.nickname or str(user_id)
    text = event.get_plaintext().strip()
    if not text:
        text = "[图片]" if any(seg.type == "image" for seg in event.get_message()) else ""
    if not text:
        return

    await append_message(conversation_id, {
        "time": int(time.time()),
        "message_id": event.message_id,
        "user_id": str(user_id),
        "user_name": user_name,
        "message": text,
        "msg_type": "user",
        "segments": [{"type": "text", "text": text}],
    })
    history = await get_recent_messages(conversation_id, count=15)
    catalog = await sticker_prompt_catalog()
    extra_style = config.extra_style
    if catalog:
        extra_style += "\n\n可选表情图片目录（仅在语境明显适合时发送一张；否则 sticker_id 为 null）：\n" + catalog
    prompt = build_main_prompt(
        config=config,
        plugin_section="",
        formatted_history=format_history(history),
        trigger_marker=f"{user_name}({user_id})：{text}  <--你正在进行私聊，直接自然地回复对方。",
        current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    prompt = prompt.replace(config.extra_style, extra_style, 1)
    try:
        reply_text, reply_json = await generate_reply(prompt)
        if not reply_text:
            logger.warning(f"No private reply generated for user {user_id}")
            return
        image_url = await sticker_url(reply_json.get("sticker_id"))
        for index, part in enumerate(reply_text.split("[/]")):
            if part.strip():
                segments = [MessageSegment.text(part.strip())]
                if index == 0 and image_url:
                    segments.append(MessageSegment.image(image_url))
                await bot.send_private_msg(user_id=user_id, message=segments)
        await append_message(conversation_id, {
            "time": int(time.time()),
            "message_id": 0,
            "user_id": str(bot.self_id),
            "user_name": config.name_cn,
            "message": reply_text.replace("[/]", " ").strip(),
            "msg_type": "bot",
            "segments": [{"type": "text", "text": reply_text.replace("[/]", " ").strip()}],
        })
        await trim_history(conversation_id)
        logger.info(f"Replied to private user {user_id}")
    except Exception as exc:
        logger.exception(f"Private reply failed for user {user_id}: {exc}")
