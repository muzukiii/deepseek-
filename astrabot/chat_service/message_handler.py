"""群消息处理核心：接收消息 → 判断是否回复 → 构建 prompt → 调用 AI → 发送回复"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from astrabot.chat_service.ai_client import analyze_images, chat, generate_reply
from astrabot.chat_service.history_manager import (
    append_message,
    get_history_since,
    get_recent_messages,
    select_context,
    trim_history,
    set_image_descriptions,
)
from astrabot.chat_service.image_utils import download_image_base64
from astrabot.chat_service import mood
from astrabot.chat_service.memory_manager import (
    is_user_ignored,
    get_personal_memorize,
    process_memorize,
    process_personal_memorize,
    process_ignore_fields,
)
from astrabot.chat_service.enhanced_memory import (
    extract_facts_from_conversation,
    build_memory_context,
)

from astrabot.chat_service.plugin_loader import PluginLoader
from astrabot.chat_service.prompt_builder import (
    build_image_analysis_prompt,
    build_main_prompt,
    build_message_text,
    build_search_inject_prompt,
    build_trigger_line,
    format_history,
    TRIGGER_AT,
    TRIGGER_MAIN,
    TRIGGER_IN_PROGRESS,
    TRIGGER_INTERRUPT,
)
from astrabot.chat_service.reply_controller import ReplyController, should_reply
from astrabot.chat_service.search_engine import run_search
from astrabot.chat_service.sticker_gallery import resolve_sticker
from astrabot.config import get_config
from astrabot.logmanager.logger import logger

TOTAL_TIMEOUT = 360
MAX_SEARCH_ROUNDS = 3

# 群成员「昵称 -> QQ号」映射缓存：group_id -> (mapping, expire_at)
_NAME_MAP_CACHE: dict[int, tuple[dict[str, int], float]] = {}
_NAME_MAP_TTL = 600.0
_NAME_MAP_MAX = 500
_MUTE_CACHE_MAX = 500
_MUTE_TTL = 30.0


def _prune_ttl_cache(cache: dict, now: float, cap: int) -> None:
    """约定 cache 值为 (payload, expire_at)：超过 cap 时先清过期项，
    仍超限则按到期时间淘汰最旧者。只删缓存，命中失败会回源重取，不影响正确性。"""
    if len(cache) <= cap:
        return
    for k in [k for k, v in cache.items() if v[1] <= now]:
        cache.pop(k, None)
    excess = len(cache) - cap
    if excess > 0:
        ordered = sorted(cache.items(), key=lambda kv: kv[1][1])
        for k, _ in ordered[:excess]:
            cache.pop(k, None)


async def handle_group_message(bot: Bot, event: GroupMessageEvent):
    """群消息入口：解析消息段 → 存历史 → 判断是否需要回复 → 进入回复流程"""
    config = get_config()
    group_id = event.group_id

    if config.enabled_groups and group_id not in config.enabled_groups:
        return

    user_id = str(event.user_id)
    plain_text = event.get_plaintext()
    # 先用缓存昵称，未命中时用 QQ 号占位；真正的网络查询推迟到确认要回复之后。
    cached_name = _NAME_CACHE.get((group_id, event.user_id))
    user_name = cached_name[0] if cached_name and cached_name[1] > time.time() else user_id

    record_time = int(time.time())
    images_b64 = []
    segments = []
    if event.reply:
        try:
            # OneBot 协议中 event.reply 是快捷访问，但可能为 None 或缺少数据
            reply_obj = event.reply
            sender = reply_obj.sender
            try:
                plain = reply_obj.message.get_plaintext()
            except Exception:
                plain = str(reply_obj.message)
            reply_seg = {
                "type": "reply",
                "user_id": str(sender.user_id),
                "user_name": sender.card or sender.nickname or "",
                "message": plain[:100],
            }
            segments.append(reply_seg)
            logger.debug(f"Reply segment resolved via event.reply: {reply_seg}")
        except Exception as e:
            logger.warning(f"Failed to extract reply from event.reply: {e}")
    for seg in event.get_message():
        if seg.type == "reply":
            try:
                original = await bot.get_msg(message_id=int(seg.data["id"]))
                sender = original.get("sender", {})
                msg_segments = original.get("message", [])
                if isinstance(msg_segments, list):
                    plain = "".join(
                        s.get("data", {}).get("text", "") for s in msg_segments if s.get("type") == "text"
                    )
                else:
                    plain = str(msg_segments)
                reply_seg = {
                    "type": "reply",
                    "user_id": str(sender.get("user_id", "")),
                    "user_name": sender.get("card") or sender.get("nickname", ""),
                    "message": plain[:100],
                }
                segments.append(reply_seg)
                logger.debug(f"Reply segment resolved via get_msg: {reply_seg}")
            except Exception as e:
                # 三级降级：get_msg → DB 查询 → 占位文本
                logger.warning(f"get_msg failed for reply {seg.data.get('id')}: {e}, falling back to DB")
                reply_id = int(seg.data["id"])
                try:
                    from astrabot.chat_service.history_manager import _get_db
                    conn = _get_db()
                    conn.row_factory = sqlite3.Row
                    cur = conn.execute(
                        "SELECT * FROM messages WHERE group_id = ? AND message_id = ? LIMIT 1",
                        (group_id, reply_id),
                    )
                    row = cur.fetchone()
                    if row:
                        segments.append({
                            "type": "reply",
                            "user_id": row["user_id"],
                            "user_name": row["user_name"],
                            "message": row["message"][:100],
                        })
                    else:
                        segments.append({"type": "reply", "user_id": "", "user_name": "", "message": f"消息{reply_id}"})
                except Exception:
                    segments.append({"type": "reply", "user_id": "", "user_name": "", "message": f"消息{reply_id}"})
        elif seg.type == "text":
            t = seg.data.get("text", "")
            if t:
                segments.append({"type": "text", "text": t})
        elif seg.type == "at":
            qq = seg.data.get("qq", "")
            name = ""
            if qq and qq != "all":
                try:
                    name = await _get_user_display_name(bot, group_id, int(qq))
                    if name == str(qq):
                        name = ""
                except Exception:
                    pass
            segments.append({"type": "at", "qq": qq, "name": name})
        elif seg.type == "image":
            url = seg.data.get("url", "")
            b64 = await download_image_base64(url) if url else None
            if b64:
                images_b64.append(b64)
            # 历史里只留 URL 与（识别后回填的）描述：base64 图片动辄
            # 十几 MB，存进数据库会让 history.db 迅速膨胀（实测曾达
            # 860MB，其中 97% 是图片）。URL 很小，且能据此重新下载，
            # 因此既控制了体积，又保留了后续识别的能力。
            segments.append({"type": "image", "url": url})

    record = {
        "time": record_time,
        "message_id": event.message_id,
        "user_id": user_id,
        "user_name": user_name,
        "message": plain_text,
        "msg_type": "user",
        # 同样不落库，只保留文本与图片占位
        "images": None,
        "segments": segments,
    }
    await append_message(group_id, record)
    await trim_history(group_id)

    # 命令消息：交给命令处理器执行，不走 AI 回复流程。
    # 原先一律 return 丢弃，导致 /用量、/memory 等命令从未生效。
    if plain_text.startswith("/"):
        handled, reply_text = _run_command(bot, group_id, user_id, plain_text, config)
        if handled:
            if reply_text:
                await _send_reply(bot, event, group_id, reply_text)
            return
        # 未识别的命令同样不进入 AI 流程
        logger.debug(f"Unknown command ignored: {plain_text[:30]}")
        return

    # 纯文本记忆指令（记住 / 帮我记住 / 请记住 …）：走与斜杠命令相同的
    # 权限与入库路径，否则该分支永远不可达（此前只有 "/" 前缀才路由到命令）。
    from astrabot.chat_service.memory_commands import check_implicit_memory_request

    _mem_content = check_implicit_memory_request(plain_text)
    if _mem_content is not None:
        handled, reply_text = _run_command(
            bot, group_id, user_id, "记住：" + _mem_content, config
        )
        if handled:
            if reply_text:
                await _send_reply(bot, event, group_id, reply_text)
            return

    is_at = event.is_tome()
    is_keyword = any(plain_text.startswith(k) for k in config.keyword) if config.keyword else False

    # 忽略名单：通常是群里的另一个机器人。即使被它 @ 也不回应，
    # 否则两个机器人会陷入无限互相回复。
    if str(user_id) in config.ignore_users:
        logger.info(
            f"Ignored message from {user_name}({user_id}) in group {group_id}: "
            f"user is in ignore_users"
        )
        return

    # 生气冷落：模型此前判定被此人惹到，在有效期内无视对方的任何消息
    if config.mood_enabled and mood.is_angry(group_id, user_id):
        left = mood.remaining_seconds(group_id, user_id)
        logger.info(
            f"Ignored message from {user_name}({user_id}) in group {group_id}: "
            f"still angry, {left}s left"
        )
        return

    if is_user_ignored(group_id, user_id, is_at):
        logger.debug(f"Ignored user {user_name}({user_id}) in group {group_id}")
        return


    if await _is_bot_muted(bot, group_id):
        logger.info(f"Bot is muted in group {group_id}, skipping reply")
        return

    # 连续互回限制：同一人已被连续回复 max_reply_streak 次就停下。
    # 恢复方式有两种，任一满足即可：
    #   1) 别人插话 —— 换人即归零
    #   2) 冷却期满 —— 默认 5 分钟，避免没人说话时永久卡住
    if config.max_reply_streak > 0:
        streak = ReplyController.peek_streak(group_id, user_id)
        if streak >= config.max_reply_streak:
            cooldown = config.reply_streak_cooldown
            if ReplyController.is_cooling_down(group_id, user_id, cooldown):
                left = ReplyController.cooldown_left(group_id, cooldown)
                logger.info(
                    f"Skipped reply to {user_name}({user_id}) in group {group_id}: "
                    f"reply streak reached {streak}/{config.max_reply_streak}, "
                    f"cooldown {left}s left."
                )
                return
            logger.info(
                f"Reply streak cooldown for {user_name}({user_id}) in group "
                f"{group_id} expired; resuming."
            )

    lock = ReplyController.get_lock(group_id)
    if lock.locked():
        # 机器人正在回复时，只有 @ 或关键词触发的消息会排队等待
        if is_at or is_keyword:
            ReplyController.add_pending_at(group_id, {
                "user_id": user_id,
                "user_name": user_name,
                "segments": segments,
                "images_b64": images_b64,
                "record_time": record_time,
            })
            logger.info(f"Queued pending @ from {user_name}({user_id}) in group {group_id}")
        return

    if not should_reply(is_at, ReplyController.is_replying(group_id), config, is_keyword):
        logger.debug(f"Ignored message from {user_name}({user_id}) in group {group_id}: probability miss")
        return

    ReplyController.set_replying(group_id, True)
    # 累加连续互回次数：换了人会自动归零
    streak = ReplyController.bump_streak(group_id, user_id)
    if config.max_reply_streak > 0:
        if streak >= config.max_reply_streak:
            # 刚到上限，启动冷却计时；期满即恢复
            ReplyController.set_blocked(group_id)
            logger.info(
                f"Reply streak for {user_name}({user_id}) in group {group_id} "
                f"reached {streak}/{config.max_reply_streak}; cooling down for "
                f"{int(config.reply_streak_cooldown)}s"
            )
        elif streak > 1:
            logger.debug(
                f"Reply streak for {user_name}({user_id}) in group {group_id}: "
                f"{streak}/{config.max_reply_streak}"
            )
    try:
        await asyncio.wait_for(
            _reply_flow(bot, event, group_id, user_id, user_name, images_b64, segments, record_time, is_at, config),
            timeout=TOTAL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"Reply flow timeout for group {group_id}")
    except Exception as e:
        logger.error(f"Reply flow error for group {group_id}: {e}")
    finally:
        ReplyController.set_replying(group_id, False)


def _run_command(bot, group_id: int, user_id: str, text: str, config):
    """执行命令，返回 (是否已处理, 回复文本)。

    命令既支持斜杠（/memory、/用量），也支持纯文本记忆指令（记住：XXX）。

    权限：
    - 配置了 command_allowed_users 时，只有白名单内的 QQ 能执行任意命令；
    - 未配置白名单（空白名单）时，只读/新增类命令仍放行，但破坏性命令
      （删除、清空）一律 fail-safe 拒绝，避免任意群友一键清空记忆。
    """
    from astrabot.chat_service.memory_commands import (
        handle_memory_command,
        is_destructive_command,
    )

    allowed = config.command_allowed_users
    if allowed:
        if str(user_id) not in allowed:
            logger.info(
                f"Command '{text[:20]}' denied for {user_id} (not in whitelist)"
            )
            return True, ""
    elif is_destructive_command(text):
        logger.warning(
            f"Destructive command '{text[:20]}' blocked for {user_id}: "
            "command_allowed_users 未配置（fail-safe 拒绝）"
        )
        return True, "删除/清空记忆需要先配置 command_allowed_users 白名单"

    try:
        return handle_memory_command(int(group_id), str(user_id), text)
    except Exception as e:
        logger.error(f"Command handler failed for '{text[:30]}': {e}")
        return True, ""


async def _extract_reply_context(bot: Bot, group_id: int, event: GroupMessageEvent, config) -> dict:
    reply_segment = None
    reply_message_id = None
    for seg in event.get_message():
        if seg.type == "reply":
            reply_message_id = int(seg.data["id"])
            break

    if event.reply:
        try:
            reply_obj = event.reply
            sender = reply_obj.sender
            try:
                plain = reply_obj.message.get_plaintext()
            except Exception:
                plain = str(reply_obj.message)
            reply_segment = {
                "type": "reply",
                "user_id": str(sender.user_id),
                "user_name": sender.card or sender.nickname or "",
                "message": plain[:100],
            }
        except Exception as e:
            logger.warning(f"Failed to extract reply context from event.reply: {e}")

    if reply_message_id is None:
        return {"segment": reply_segment, "message_text": "", "image_desc": ""}

    reply_text = ""
    reply_images_b64: list[str] = []
    try:
        original = await bot.get_msg(message_id=reply_message_id)
        sender = original.get("sender", {})
        msg_segments = original.get("message", [])
        if isinstance(msg_segments, list):
            parts: list[str] = []
            for s in msg_segments:
                if s.get("type") == "text":
                    parts.append(s.get("data", {}).get("text", ""))
                elif s.get("type") == "image":
                    url = s.get("data", {}).get("url", "")
                    b64 = await download_image_base64(url) if url else None
                    if b64:
                        reply_images_b64.append(b64)
            reply_text = "".join(parts)
        else:
            reply_text = str(msg_segments)
        if reply_segment is None:
            reply_segment = {
                "type": "reply",
                "user_id": str(sender.get("user_id", "")),
                "user_name": sender.get("card") or sender.get("nickname", ""),
                "message": reply_text[:100],
            }
    except Exception as e:
        logger.warning(f"Failed to fetch reply message {reply_message_id}: {e}")
        if not reply_text and reply_segment:
            reply_text = str(reply_segment.get("message") or "")

    reply_image_desc = ""
    if reply_images_b64:
        ctx = reply_text or (reply_segment.get("message", "") if reply_segment else "")
        analysis_prompt = build_image_analysis_prompt(config.image_analyzer, ctx)
        reply_image_result = await analyze_images(reply_images_b64, analysis_prompt)
        reply_desc_parts = [v for _, v in sorted(reply_image_result.items())]
        reply_image_desc = "; ".join(reply_desc_parts)

    return {
        "segment": reply_segment,
        "message_text": reply_text,
        "image_desc": reply_image_desc,
    }


async def _reply_flow(
    bot: Bot,
    event: GroupMessageEvent,
    group_id: int,
    user_id: str,
    user_name: str,
    images_b64: list[str],
    current_segments: list[dict],
    record_time: int,
    is_at: bool,
    config,
):
    """核心回复流程：构建上下文 → 插件预处理 → 图片分析 → 拼接 prompt → AI 调用（含搜索） → 发送回复"""
    # 只有确定要回复时才查询真实昵称（命中缓存则无网络开销）
    user_name = await _get_user_display_name(bot, group_id, int(user_id))

    # 在处理中途检查触发消息是否已被撤回
    if ReplyController.is_recalled(group_id, event.message_id):
        logger.info(f"Trigger message {event.message_id} was recalled, aborting reply")
        return

    plain_text = "".join(
        seg.get("text", "") for seg in current_segments if seg.get("type") == "text"
    )

    plugin_since = int(time.time())  # 记录插件执行前的时间戳，后续用于获取执行期间的新消息

    history = await select_context(
        group_id,
        max_messages=config.history_messages,
        exclude_message_id=event.message_id,
    )
    reply_context = await _extract_reply_context(bot, group_id, event, config)

    current_image_descs: list[str] = []
    image_desc = ""
    if images_b64:
        ctx = "".join(
            f"{m.get('user_name','?')}：{m.get('message','')}\n"
            for m in history[-3:]
        )
        analysis_prompt = build_image_analysis_prompt(config.image_analyzer, ctx)
        current_image_result = await analyze_images(images_b64, analysis_prompt)
        desc_parts = [v for _, v in sorted(current_image_result.items())]
        current_image_descs = desc_parts
        image_desc = "; ".join(desc_parts)

    plugin_hook_result = await PluginLoader.execute_chain(
        bot=bot,
        event=event,
        history=history,
        image_desc=image_desc,
        reply_context=reply_context,
        config=config,
    )
    plugin_result = plugin_hook_result.merged
    re_exec_append = plugin_hook_result.re_exec_append
    pre_prompt_parts = plugin_hook_result.pre_prompt_parts

    plugin_section = PluginLoader.get_plugin_section(
        event=event, enabled=config.plugin_exec_enabled
    )

    if plugin_result and plugin_result.get("skip_main") and not re_exec_append:
        # 插件要求跳过主 AI 调用，直接使用插件返回的回复
        reply_text = plugin_result.get("reply", "")
        if reply_text:
            await _send_reply(bot, event, group_id, reply_text, record_reply=True)
        return

    history_image_map = _collect_images_from_history(history, max_images=3)
    if history_image_map:
        # 历史里只存了 URL，识别时按需重新下载（base64 不落库以免撑爆数据库）
        all_b64: list[str] = []
        for _, _, url in history_image_map:
            got = await download_image_base64(url)
            if got:
                all_b64.append(got)
        ctx = "".join(
            f"{m.get('user_name','?')}：{m.get('message','')}\n"
            for m in history[-3:]
        )
        analysis_prompt = build_image_analysis_prompt(config.image_analyzer, ctx)
        analysis_result = await analyze_images(all_b64, analysis_prompt) if all_b64 else {}
        history_image_descs: dict[tuple[int, int], str] = {}
        desc_by_pos: dict[tuple[int, int], str] = {}
        for (msg_idx, seg_idx, _), (_, desc) in zip(history_image_map, sorted(analysis_result.items())):
            history_image_descs[(msg_idx, seg_idx)] = desc
            desc_by_pos[(msg_idx, seg_idx)] = desc

        # 把描述回写到历史记录，之后同一张图不必重复识别（省钱且更快）
        if desc_by_pos:
            try:
                updates: dict[int, dict] = {}
                for (msg_idx, seg_idx), desc in desc_by_pos.items():
                    if msg_idx >= len(history):
                        continue
                    mid = history[msg_idx].get("message_id")
                    if mid:
                        updates.setdefault(int(mid), {})[seg_idx] = desc
                saved = await set_image_descriptions(group_id, updates)
                if saved:
                    logger.debug(f"Persisted image descriptions for {saved} message(s)")
            except Exception as e:
                logger.debug(f"Failed to persist image descriptions: {e}")

        # 顺手把刚识别过的图存进本地表情库：复用已有的图片与描述，
        # 不额外调用 API，因此不产生花费。
        if config.sticker_enabled and config.sticker_autosave:
            try:
                from astrabot.chat_service import sticker_store

                for (_, _, b64), (_, desc) in zip(
                    history_image_map, sorted(analysis_result.items())
                ):
                    raw = sticker_store.image_b64_to_bytes(b64)
                    if raw and desc:
                        sticker_store.save_sticker(
                            raw, desc, max_count=config.sticker_max_count
                        )
            except Exception as e:
                logger.debug(f"Sticker autosave skipped: {e}")
    else:
        history_image_descs = {}

    pending_list = ReplyController.pop_pending_at(group_id)
    has_pending = bool(pending_list)

    # 构建触发标记：有排队消息时，当前消息标记为 IN_PROGRESS，排队消息标记为 INTERRUPT
    trigger_lines = []
    current_text = build_message_text(current_segments, current_image_descs)
    if has_pending:
        trigger_lines.append(build_trigger_line(user_name, user_id, current_text, TRIGGER_IN_PROGRESS))
        for p in pending_list:
            p_text = build_message_text(p["segments"], [])
            trigger_lines.append(build_trigger_line(p["user_name"], p["user_id"], p_text, TRIGGER_INTERRUPT))
    else:
        trigger_lines.append(build_trigger_line(
            user_name, user_id, current_text,
            TRIGGER_AT if is_at else TRIGGER_MAIN,
        ))
    trigger_marker = "\n".join(trigger_lines)

    formatted_history = format_history(history, history_image_descs)


    # 长期记忆已收敛为 SQLite 单一真源（memory_ctx["long_term"] 始终注入），
    # 旧 JSON 的 memory_section 回退退役：启动时 migrate 已把 JSON 记忆并入
    # SQLite，这里恒为空，避免"两套编号"导致模型删除时错位。
    memory_section = ""


    personal_memories = get_personal_memorize(group_id)
    # 收集当前消息涉及的所有用户 ID，用于加载相关个人印象
    relevant_uids = {user_id}
    for seg in event.get_message():
        if seg.type == "at":
            qq = seg.data.get("qq", "")
            if qq and qq.isdigit():
                relevant_uids.add(qq)
    reply_to_uid = await _get_reply_user_id(bot, event)
    if reply_to_uid:
        relevant_uids.add(reply_to_uid)


    memory_ctx = build_memory_context(
        group_id=group_id,
        current_message=plain_text,
        max_facts=config.memory_facts,
        max_long_term=config.long_term_memories,
        # 发言人是本次回复的核心对象，其事实记忆优先；被 @ / 被回复的人次之。
        priority_user_ids={str(user_id)} | ({reply_to_uid} if reply_to_uid else set()),
        priority_ratio=config.memory_priority_ratio,
    )
    logger.debug(f"Enhanced memory context: LTM={len(memory_ctx['long_term'])} chars, Facts={len(memory_ctx['facts'])} chars")


    personal_lines = []
    for uid in relevant_uids:
        memo = personal_memories.get(uid)
        if memo:
            name = await _get_user_display_name(bot, group_id, int(uid))
            personal_lines.append(f"{name}({uid})：{memo}")
    personal_memory_section = "\n".join(personal_lines) if personal_lines else ""

    override_prompt = plugin_result.get("override_prompt") if plugin_result else None
    append_prompt = plugin_result.get("append_prompt") if plugin_result else None
    pre_prompt = "\n\n".join(part for part in pre_prompt_parts if part)

    if override_prompt:
        main_prompt = override_prompt
        if pre_prompt:
            main_prompt += "\n\n" + pre_prompt
    else:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        main_prompt = build_main_prompt(
            config=config,
            plugin_section=plugin_section,
            formatted_history=formatted_history,
            trigger_marker=trigger_marker,
            memory_section=memory_section,
            personal_memory_section=personal_memory_section,
            current_time=current_time,
            long_term_memory=memory_ctx["long_term"],
            relevant_facts=memory_ctx["facts"],
        )
        inject_parts = [part for part in [pre_prompt, append_prompt] if part]
        if inject_parts:
            main_prompt += "\n\n" + "\n\n".join(inject_parts)

        # 注入本地表情目录，让模型知道可以发表情（此前只有私聊做了这件事）
        if config.sticker_enabled and config.sticker_catalog_size > 0:
            try:
                from astrabot.chat_service.sticker_gallery import (
                    sticker_prompt_catalog,
                )

                catalog = await sticker_prompt_catalog(
                    limit=config.sticker_catalog_size,
                    query=current_text,
                )
                if catalog:
                    main_prompt += (
                        "\n\n可选表情图片目录（仅在语境明显适合时挑一张，"
                        "在 JSON 里用 sticker_id 给出它的 id；"
                        "不需要或没有合适的就填 null，不要强行发表情）：\n"
                        + catalog
                    )
            except Exception as e:
                logger.debug(f"Sticker catalog skipped: {e}")

    if re_exec_append:
        # re_exec 插件要求：将插件输出注入历史后，获取执行期间新消息，让 AI 重新构思
        await append_message(group_id, {
            "time": int(time.time()),
            "message_id": 0,
            "user_id": str(bot.self_id),
            "user_name": "",
            "message": re_exec_append[:500],
            "images": None,
            "segments": [{"type": "text", "text": re_exec_append[:500]}],
        })
        new_msgs = await get_history_since(group_id, plugin_since)
        new_msgs_text = format_history(new_msgs)
        inject_parts = [re_exec_append]
        if new_msgs_text:
            inject_parts.append("插件执行期间群友的新发言：")
            inject_parts.append(new_msgs_text)
            inject_parts.append("请结合这些信息，重新构思回复。")
        main_prompt += "\n\n" + "\n".join(inject_parts)

    if ReplyController.is_recalled(group_id, event.message_id):
        # 在真正调用 AI 之前再次检查，因为 prompt 构建阶段可能耗时较长
        logger.info(f"Trigger message {event.message_id} recalled during prep, aborting")
        return

    logger.debug(f"===== AI PROMPT for group {group_id} =====\n{main_prompt}\n===== END PROMPT =====")

    reply_text, reply_json = await _ai_call_with_search(bot, event, group_id, user_name, user_id, plain_text, main_prompt)

    memorize = reply_json.get("memorize")
    if memorize:
        process_memorize(group_id, memorize, memory_ctx["long_term_ids"])

    personal_memorize = reply_json.get("personal_memorize")
    if personal_memorize:
        process_personal_memorize(group_id, personal_memorize)

    ignoreing_reply = reply_json.get("ignoreing_reply")
    ignore_even_at = reply_json.get("ignore_even_at")
    if ignoreing_reply or ignore_even_at:
        process_ignore_fields(group_id, ignoreing_reply, ignore_even_at)

    # 生气：模型判定被对方惹到时，短暂冷落对方（默认 10 分钟），
    # 期间无视其消息，到期自动恢复。
    if config.mood_enabled:
        angry = str(reply_json.get("angry", "") or "").strip().lower()
        if angry in ("true", "1", "yes", "on"):
            reason = str(reply_json.get("angry_reason", "") or "").strip()
            secs = config.mood_angry_seconds
            try:
                raw_secs = reply_json.get("angry_seconds")
                if raw_secs is not None:
                    secs = max(30, min(int(float(raw_secs)), 24 * 3600))
            except (TypeError, ValueError):
                pass
            mood.set_angry(group_id, user_id, seconds=secs, reason=reason)

    # 插件工具执行（模型驱动的 execute() 调用）默认关闭：仅当 plugin_exec_enabled
    # 为真时才会进入。开启后仍对每轮设上限，并对每次 execute 施加超时与异常兜底，
    # 避免任意外部插件代码把整条回复链路拖死或放大付费调用。
    if config.plugin_exec_enabled:
        for _round in range(config.plugin_exec_max_rounds):
            # 检查 AI 回复中是否包含插件调用字段，若有则执行插件并重新生成回复
            plugin_val = None
            exec_fn = None
            exec_plugin = None
            for p in PluginLoader.plugins:
                v = reply_json.get(p.name)
                if v is not None and isinstance(v, str) and v.strip():
                    plugin_val = v
                    exec_fn = getattr(p.module, "execute", None)
                    exec_plugin = p
                    break
            if not exec_fn or not plugin_val:
                break
            if reply_text:
                await _send_reply(bot, event, group_id, reply_text, reply_json=reply_json, record_reply=True)
            try:
                output = await asyncio.wait_for(
                    exec_fn(
                        plugin_val,
                        group_id=group_id,
                        plugin_config=getattr(exec_plugin, "settings", None),
                    ),
                    timeout=config.plugin_exec_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Plugin {exec_plugin.name} execute timed out after {config.plugin_exec_timeout}s"
                )
                break
            except Exception as e:
                logger.error(f"Plugin {exec_plugin.name} execute failed: {e}")
                break
            if not isinstance(output, str):
                output = str(output or "")
            logger.info(f"Plugin executed: {plugin_val}")
            logger.debug(f"Plugin output:\n{output[:1000]}")
            plugin_inject = f"你执行了命令 `{plugin_val}`，输出如下：\n{output[:2000]}"
            main_prompt += "\n\n" + plugin_inject
            await append_message(group_id, {
                "time": int(time.time()), "message_id": 0,
                "user_id": str(bot.self_id), "user_name": "",
                "message": plugin_inject[:500], "images": None,
                "segments": [{"type": "text", "text": plugin_inject[:500]}],
            })
            reply_text, reply_json = await generate_reply(main_prompt)

    if not reply_text:
        logger.info(f"No reply generated for group {group_id}")
        return

    await _send_reply(bot, event, group_id, reply_text, reply_json=reply_json, record_reply=True)


    if reply_text and user_id and user_id != str(bot.self_id) and plain_text:
        # 异步提取对话事实，不阻塞回复发送
        asyncio.create_task(
            extract_facts_from_conversation(
                group_id, user_id, user_name, plain_text, reply_text
            )
        )


async def _ai_call_with_search(
    bot: Bot,
    event: GroupMessageEvent,
    group_id: int,
    user_name: str,
    user_id: str,
    plain_text: str,
    main_prompt: str,
) -> tuple[str, dict]:
    """调用 AI 生成回复，若 AI 请求搜索则进行多轮联网搜索后重新生成"""
    reply_text, reply_json = await generate_reply(main_prompt)

    search_round = 0
    search_query = reply_json.get("search", "")
    if search_query and reply_text:
        # 搜索期间先发送已有回复，搜索完成后再次生成补充回复
        await _send_reply(bot, event, group_id, reply_text, reply_json=reply_json, record_reply=True)

    while search_query and search_round < MAX_SEARCH_ROUNDS:
        if ReplyController.is_recalled(group_id, event.message_id):
            logger.info(f"Trigger message {event.message_id} recalled during search, aborting")
            return reply_text, reply_json

        search_round += 1
        logger.info(f"Search round {search_round}: {search_query}")

        context = f"群 {group_id} 中 {user_name}({user_id}) 说：{plain_text}"

        search_since = int(time.time())
        search_result = await run_search(search_query, context)
        new_msgs = await get_history_since(group_id, search_since)
        new_msgs_text = format_history(new_msgs)

        search_summary = search_result.get("summary", "")
        search_failed = not search_summary
        if search_failed:
            logger.warning("Search returned empty result")

        search_inject = build_search_inject_prompt(
            search_summary=search_summary,
            new_messages_since_search=new_msgs_text,
            search_failed=search_failed,
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        enhanced_prompt = main_prompt + "\n\n" + search_inject
        logger.debug(f"===== SEARCH INJECT PROMPT round {search_round} =====\n{search_inject}\n===== END SEARCH INJECT =====")
        reply_text, reply_json = await generate_reply(enhanced_prompt)
        search_query = reply_json.get("search", "")

    return reply_text, reply_json


def _collect_images_from_history(history: list[dict], max_images: int = 3) -> list[tuple[int, int, str]]:
    """从历史消息中倒序收集最近「尚未识别」的图片。

    注意：图片 base64 已不再落库（曾导致 history.db 涨到 860MB），
    因此这里只能拿到段里保存的 URL。返回值第三项是该图的 URL，
    没有 URL 的段落跳过。
    """
    result: list[tuple[int, int, str]] = []
    for msg_idx in range(len(history) - 1, -1, -1):
        segs = history[msg_idx].get("segments") or []
        for seg_idx in range(len(segs) - 1, -1, -1):
            seg = segs[seg_idx]
            if seg.get("type") != "image":
                continue
            # 已经有描述的不必重复识别
            if seg.get("desc"):
                continue
            url = seg.get("url") or ""
            if url:
                result.append((msg_idx, seg_idx, url))
            if len(result) >= max_images:
                break
        if len(result) >= max_images:
            break
    result.reverse()  # 翻转为从旧到新，保持时间顺序
    return result


def _collect_undescribed_images(history: list[dict], max_images: int = 3) -> list[tuple[int, int, str]]:
    """收集历史中已存 URL 但没有描述的图片，返回 (msg_idx, seg_idx, url)。"""
    return _collect_images_from_history(history, max_images=max_images)


_mute_cache: dict[int, tuple[bool, float]] = {}


async def _is_bot_muted(bot: Bot, group_id: int) -> bool:
    """检查机器人在群内是否被禁言，结果缓存 30 秒"""
    now = time.time()
    cached = _mute_cache.get(group_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        self_id = int(bot.self_id)
        info = await bot.get_group_member_info(group_id=group_id, user_id=self_id)
        shut_up = info.get("shut_up_timestamp", 0)
        muted = shut_up > now
        _mute_cache[group_id] = (muted, now + _MUTE_TTL)
        _prune_ttl_cache(_mute_cache, now, _MUTE_CACHE_MAX)
        if muted:
            logger.info(f"Bot muted in group {group_id} until {shut_up}")
        return muted
    except Exception as e:
        logger.warning(f"Failed to check mute status for group {group_id}: {e}")
        return False


def _append_with_at(segments: list, text: str):
    """将文本中的 [at:qq号] 标记转换为 MessageSegment.at"""
    parts = re.split(r"\[at:(\d+)\]", text)
    for j, p in enumerate(parts):
        if j % 2 == 0:
            if p:
                segments.append(MessageSegment.text(p))
        else:
            segments.append(MessageSegment.at(int(p)))


def resolve_plain_at(text: str, name_to_qq: dict[str, int]) -> str:
    """把模型写的普通「@昵称」转换成真正的 [at:QQ号] 标记。

    模型经常不遵守 [at:QQ号] 的输出约定，而是直接写「@昵称」。
    那在 QQ 里只是普通文字，对方既不会收到提醒也不会高亮。
    这里根据已知的群成员昵称做一次确定性替换，保证 @ 真的生效。
    """
    if not text or "@" not in text or not name_to_qq:
        return text

    # 长昵称优先，避免 "小明" 抢先匹配掉 "小明的朋友"
    for name in sorted(name_to_qq, key=len, reverse=True):
        if not name:
            continue
        qq = name_to_qq[name]
        # 已经是 [at:...] 形式的不动；只替换裸露的 @昵称
        pattern = re.compile(r"@" + re.escape(name) + r"(?!\d)(?!\])")
        if pattern.search(text):
            text = pattern.sub(f"[at:{qq}]", text)

    return text


async def _get_group_name_map(bot: Bot, group_id: int) -> dict[str, int]:
    """获取「群昵称 -> QQ号」映射，结果缓存 10 分钟。"""
    now = time.time()
    cached = _NAME_MAP_CACHE.get(group_id)
    if cached and cached[1] > now:
        return cached[0]

    mapping: dict[str, int] = {}
    try:
        members = await bot.get_group_member_list(group_id=group_id)
        for m in members or []:
            uid = m.get("user_id")
            if uid is None:
                continue
            for key in (m.get("card"), m.get("nickname")):
                if key and str(key).strip():
                    mapping[str(key).strip()] = int(uid)
    except Exception as e:
        logger.debug(f"Failed to fetch group member list for {group_id}: {e}")
        return cached[0] if cached else {}

    _NAME_MAP_CACHE[group_id] = (mapping, now + _NAME_MAP_TTL)
    _prune_ttl_cache(_NAME_MAP_CACHE, now, _NAME_MAP_MAX)
    return mapping


async def _send_reply(
    bot: Bot,
    event: GroupMessageEvent,
    group_id: int,
    text: str,
    reply_json: dict | None = None,
    record_reply: bool = False,
):
    """发送回复消息，支持 [/] 分段发送和 [at:qq] 引用，可选记录到历史"""
    # 引用回复：可通过 .env 的 quote_reply 控制；模型也可用 is_reply_to 单独指定
    cfg_for_quote = get_config()
    is_reply_to = (reply_json or {}).get("is_reply_to", cfg_for_quote.quote_reply)

    # 模型常写「@昵称」而不用 [at:QQ号]，这里做一次确定性转换，
    # 保证 @ 真的能提醒到人（普通文字形式的 @ 不会触发任何通知）。
    self_id = int(bot.self_id)
    if "@" in text:
        cfg = get_config()
        name_to_qq = await _get_group_name_map(bot, group_id)
        # 机器人自己的名字不应被当成 @ 目标，否则会 @ 到自己
        for own in (cfg.name_cn, cfg.name):
            if own:
                name_to_qq.pop(str(own).strip(), None)
        text = resolve_plain_at(text, name_to_qq)

    # 模型偶尔直接写 [at:机器人自己]，@ 自己毫无意义，降级为普通文字
    text = re.sub(rf"\[at:{self_id}\]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

    # [/] 原本是拆成多条消息发送（模仿真人连发多气泡），现在合并为
    # 单条消息、用换行分隔，避免刷屏并让回答更完整。
    joined = "\n".join(
        part.strip() for part in text.split("[/]") if part.strip()
    )

    segments: list = []
    _append_with_at(segments, joined)
    if is_reply_to:
        segments.insert(0, MessageSegment.reply(event.message_id))

    message = segments[0] if len(segments) == 1 else segments
    image_url = await resolve_sticker((reply_json or {}).get("sticker_id"))
    if image_url:
        if not isinstance(message, list):
            message = [message]
        try:
            message.append(MessageSegment.image(image_url))
        except Exception as e:
            logger.warning(f"Failed to attach sticker {image_url}: {e}")
    await bot.send_group_msg(group_id=group_id, message=message)

    if record_reply:
        config = get_config()
        plain = re.sub(r"\[at:\d+\]", "", joined)

        plain = plain.replace("[/]", "\n").strip()
        reply_record = {
            "time": int(time.time()),
            "user_id": str(bot.self_id),
            "user_name": config.name_cn,
            "message": plain,
            "msg_type": "bot",
            "images": None,
        }
        await append_message(group_id, reply_record)

    logger.info(f"Replied to group {group_id}: {text[:50]}...")


async def _get_reply_user_id(bot: Bot, event: GroupMessageEvent) -> str | None:
    if event.reply:
        try:
            return str(event.reply.sender.user_id)
        except Exception:
            pass
    for seg in event.get_message():
        if seg.type == "reply":
            try:
                msg_id = int(seg.data["id"])
                original = await bot.get_msg(message_id=msg_id)
                sender = original.get("sender", {})
                return str(sender.get("user_id", ""))
            except Exception:
                return None
    return None


# 群成员昵称缓存：(group_id, user_id) -> (name, expire_at)
# get_group_member_info 是一次网络往返，耗时 0.3~4 秒；同一成员在 TTL 内
# 直接复用缓存，可显著降低每条消息的处理延迟。
_NAME_CACHE: dict[tuple[int, int], tuple[str, float]] = {}
_NAME_CACHE_TTL = 600.0


async def _get_user_display_name(bot: Bot, group_id: int, user_id: int) -> str:
    key = (group_id, user_id)
    now = time.time()
    cached = _NAME_CACHE.get(key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
        name = info.get("card") or info.get("nickname", str(user_id))
    except Exception:
        # 失败时沿用旧缓存（若有），否则退回 QQ 号
        name = cached[0] if cached else str(user_id)

    _NAME_CACHE[key] = (name, now + _NAME_CACHE_TTL)
    _prune_ttl_cache(_NAME_CACHE, now, 2000)
    return name
