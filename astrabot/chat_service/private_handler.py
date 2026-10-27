"""私聊消息处理：独立保存每个用户的上下文并生成私聊回复。"""

from __future__ import annotations

import time
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, PrivateMessageEvent

from astrabot.chat_service.ai_client import analyze_images, generate_reply
from astrabot.chat_service.history_manager import (
    append_message,
    get_history_since,
    select_context,
    set_image_descriptions,
    trim_history,
)
from astrabot.chat_service.history_summarizer import schedule_summarize
from astrabot.chat_service.image_utils import download_image_base64
from astrabot.chat_service.prompt_builder import (
    build_image_analysis_prompt,
    build_main_prompt,
    build_search_inject_prompt,
    format_history,
    format_reply_lines,
)
from astrabot.chat_service.search_engine import (
    MAX_SEARCH_ROUNDS,
    SEARCH_PROMISE_NUDGE,
    promises_search,
    run_search,
)
from astrabot.chat_service.sticker_gallery import sticker_prompt_catalog, sticker_url
from astrabot.config import get_config
from astrabot.logmanager.logger import logger


def _wanted_search(reply_json, config) -> str:
    """取模型这一轮想要的搜索词；总开关关掉或字段不合法时返回空串。

    search_enabled=false 时整条付费路径都不该走，所以在这里就掐掉，
    免得调用方还要各自判一次。
    """
    if not isinstance(reply_json, dict):
        return ""
    query = reply_json.get("search", "")
    if not isinstance(query, str):
        return ""
    query = query.strip()
    if query and not config.search_enabled:
        logger.info(f"Search disabled by config, skipping private request: {query}")
        return ""
    return query


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
    image_seg_idx: list[int] = []  # 成功下载的图在存储 segments 里的下标（第 k 张图 = 下标 k）
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
            image_seg_idx.append(image_count)

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
        focus_user_id=str(user_id),
    )
    schedule_summarize(conversation_id, config.history_messages, config)

    # 识别本轮私聊里的图片，把描述并入文本，让模型真的"看到"图
    if images_b64:
        ctx = "".join(
            f"{m.get('user_name','?')}：{m.get('message','')}\n"
            for m in history[-3:]
        )
        analysis_prompt = build_image_analysis_prompt(config.image_analyzer, ctx)
        result = await analyze_images(images_b64, analysis_prompt)
        result = result if isinstance(result, dict) else {}
        keys = sorted(result)
        desc_by_pos: list[tuple[int, str]] = []
        for i, seg_idx in enumerate(image_seg_idx):
            desc = result.get(f"image_{i + 1}")
            if not isinstance(desc, str) or not desc.strip():
                # 模型没按 image_N 约定给键时，退化成"排序后按位置取"
                desc = result.get(keys[i]) if i < len(keys) else ""
            if isinstance(desc, str) and desc.strip():
                desc_by_pos.append((seg_idx, desc.strip()))
        if desc_by_pos:
            desc_text = "；".join(d for _, d in desc_by_pos)
            text = f"{text} [图片][图片描述：{desc_text}]".strip() if text else f"[图片][图片描述：{desc_text}]"
            # 描述要写回历史：消息是在识图之前落库的，不回写的话这张图只有本轮
            # "看得见"，下一轮再提就只剩模型自己上一句的转述，搜索词会褪成
            # "XX 光环 角色"这种泛词。
            try:
                await set_image_descriptions(
                    conversation_id,
                    {int(event.message_id): dict(desc_by_pos)},
                )
            except Exception as e:
                logger.debug(f"Failed to persist private image descriptions: {e}")
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

    async def _deliver(text_out: str, sticker_id=None) -> None:
        """把一条回复发出去并记进历史（私聊仍是"多气泡"风格）。"""
        if not text_out:
            return
        image_url = await sticker_url(sticker_id)
        # 先按"只在句末换行"整理再按行拆分：以前直接 split("[/]")，
        # 模型断在句中时会把一句话劈成两条消息。
        text_out = format_reply_lines(text_out)
        bubbles = [part.strip() for part in text_out.split("\n") if part.strip()]
        for index, part in enumerate(bubbles):
            segments = [MessageSegment.text(part)]
            if index == 0 and image_url:
                segments.append(MessageSegment.image(image_url))
            await bot.send_private_msg(user_id=user_id, message=segments)
        flat = text_out.replace("\n", " ").strip()
        await append_message(conversation_id, {
            "time": int(time.time()),
            "message_id": 0,
            "user_id": str(bot.self_id),
            "user_name": config.name_cn,
            "message": flat,
            "msg_type": "bot",
            "segments": [{"type": "text", "text": flat}],
        })
        logger.info(f"Replied to private user {user_id}")

    try:
        base_prompt = prompt
        reply_text, reply_json = await generate_reply(prompt)
        search_query = _wanted_search(reply_json, config)
        if not search_query and config.search_enabled and promises_search(reply_text):
            # 私聊最伤人：说了"等我几分钟，马上汇报"却没有 search 字段，
            # 对面就一直挂着。用一次纠正性重生成兜底。
            logger.info(f"Private reply promised a search without a search field, retrying once (user {user_id})")
            reply_text, reply_json = await generate_reply(prompt + "\n\n" + SEARCH_PROMISE_NUDGE)
            search_query = _wanted_search(reply_json, config)

        search_round = 0
        while search_query and search_round < MAX_SEARCH_ROUNDS:
            search_round += 1
            # 搜索要跑几秒到几十秒，先把已经想好的那句发出去，
            # 否则模型说的"等我查一下"会变成已读不回。
            await _deliver(reply_text)
            logger.info(f"Private search round {search_round} for user {user_id}: {search_query}")

            context = f"私聊中 {user_name}({user_id}) 说：{text}"
            search_since = int(time.time())
            search_result = await run_search(search_query, context)
            new_msgs = await get_history_since(conversation_id, search_since)
            search_summary = search_result.get("summary", "") if isinstance(search_result, dict) else ""
            if not search_summary:
                logger.warning(f"Private search returned empty result for user {user_id}")

            search_inject = build_search_inject_prompt(
                search_summary=search_summary,
                new_messages_since_search=format_history(new_msgs),
                search_failed=not search_summary,
                current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            logger.debug(f"===== PRIVATE SEARCH INJECT round {search_round} =====\n{search_inject}\n===== END =====")
            reply_text, reply_json = await generate_reply(base_prompt + "\n\n" + search_inject)
            search_query = _wanted_search(reply_json, config)

        if not reply_text:
            logger.warning(f"No private reply generated for user {user_id}")
            return
        await _deliver(reply_text, reply_json.get("sticker_id"))
        await trim_history(conversation_id)
    except Exception as exc:
        logger.exception(f"Private reply failed for user {user_id}: {exc}")
