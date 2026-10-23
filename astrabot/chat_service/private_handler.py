"""私聊消息处理：独立保存每个用户的上下文并生成私聊回复。"""

from __future__ import annotations

import time
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, PrivateMessageEvent

from astrabot.chat_service.ai_client import analyze_images, generate_reply
from astrabot.chat_service.history_manager import append_message, select_context, trim_history
from astrabot.chat_service.history_summarizer import schedule_summarize
from astrabot.chat_service.image_utils import download_image_base64
from astrabot.chat_service.prompt_builder import (
    build_image_analysis_prompt,
    build_main_prompt,
    format_history,
)
from astrabot.chat_service.sticker_gallery import sticker_prompt_catalog, sticker_url
from astrabot.config import get_config
from astrabot.logmanager.logger import logger


async def handle_private_message(bot: Bot, event: PrivateMessageEvent):
    config = get_config()
    user_id = int(event.user_id)
    conversation_id = -abs(user_id)
    user_name = event.sender.card or event.sender.nickname or str(user_id)
    text = event.get_plaintext().strip()

    # 收集图片并下载：此前私聊只把图片记作字符串 "[图片]"，
    # 从不下载也不识别，导致机器人对私聊图片完全"看不见"，
    # 只能凭上文瞎猜（日志思维链里出现过 "Another image, no description"）。
    images_b64: list[str] = []
    image_count = 0
    for seg in event.get_message():
        if seg.type != "image":
            continue
        image_count += 1
        url = seg.data.get("url", "")
        if not url:
            continue
        got = await download_image_base64(url)
        if got:
            images_b64.append(got)

    if not text and image_count == 0:
        return

    await append_message(conversation_id, {
        "time": int(time.time()),
        "message_id": event.message_id,
        "user_id": str(user_id),
        "user_name": user_name,
        "message": text,
        "msg_type": "user",
        # 图片只留占位，base64 不落库（与群聊一致，避免数据库膨胀）
        "segments": [{"type": "text", "text": text}]
        + [{"type": "image"} for _ in range(image_count)],
    })
    # 私聊最能聊长（一对一），也最吃"超出窗口的记忆"，所以和群聊一样走 select_context：
    # 只有它会带上 pinned 的前情摘要，get_recent_messages 只会看最近若干条。
    # 同时排除本轮消息，避免它在历史与触发行里各出现一次。
    history = await select_context(
        conversation_id,
        max_messages=config.history_messages,
        exclude_message_id=event.message_id,
    )
    schedule_summarize(conversation_id, config.history_messages, config)

    # 识别本轮私聊里的图片，把描述并入文本，让模型真的"看到"图
    image_descs: list[str] = []
    if images_b64:
        ctx = "".join(
            f"{m.get('user_name','?')}：{m.get('message','')}\n"
            for m in history[-3:]
        )
        analysis_prompt = build_image_analysis_prompt(config.image_analyzer, ctx)
        result = await analyze_images(images_b64, analysis_prompt)
        image_descs = [v for _, v in sorted(result.items()) if v]
        if image_descs:
            desc_text = "；".join(image_descs)
            text = f"{text} [图片][图片描述：{desc_text}]".strip() if text else f"[图片][图片描述：{desc_text}]"
        else:
            logger.debug(f"Private image analysis returned nothing for user {user_id}")

    catalog = await sticker_prompt_catalog(query=text)
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
