"""NoneBot 插件入口：注册群消息、私聊、撤回处理器，并加载用户插件。

这段代码原先住在 chat_service/__init__.py 里。放在包 __init__ 的后果是：只要
`import astrabot.chat_service.enhanced_memory`（哪怕只想要记忆层），Python 会先
执行包初始化，于是连带拉起 nonebot 与 OneBot 适配器。记忆/历史层自身零平台依赖，
却被包装层拖进了平台。挪到本文件后，__init__.py 变空，同一套代码就能在没有
nonebot 的环境（比如 AstrBot 侧）当库 import。

加载点在 bot.py：nonebot.load_all_plugins(["astrabot.chat_service.entry"], ["astrabot"])。
"""

from __future__ import annotations

from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, NoticeEvent, PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import PRIVATE

from astrabot.chat_service.desktop_notify import send_desktop_notification
from astrabot.chat_service.history_manager import delete_by_message_id
from astrabot.chat_service.message_handler import handle_group_message
from astrabot.chat_service.poke_handler import handle_poke
from astrabot.chat_service.plugin_loader import PluginLoader
from astrabot.chat_service.private_handler import handle_private_message
from astrabot.chat_service.reply_controller import ReplyController
from astrabot.logmanager.logger import logger

reply = on_message(priority=10)


@reply.handle()
async def handle(bot: Bot, event: GroupMessageEvent):
    await handle_group_message(bot, event)


notice_handler = on_notice()

private_reply = on_message(permission=PRIVATE, priority=10, block=True)


@private_reply.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent):
    await handle_private_message(bot, event)


@notice_handler.handle()
async def handle_notice(bot: Bot, event: NoticeEvent):
    data = event.model_dump()
    notice_type = data.get("notice_type")
    sub_type = data.get("sub_type")

    # 戳一戳在不同实现里字段位置不同：
    # 有的是 notice_type="poke"，SnowLuma 则是 notice_type="notify" + sub_type="poke"
    if notice_type == "poke" or (notice_type == "notify" and sub_type == "poke"):
        await handle_poke(bot, data)
        return

    if notice_type == "group_recall":
        group_id = data.get("group_id")
        message_id = data.get("message_id")
        if not group_id or not message_id:
            return
        ReplyController.add_recalled(group_id, message_id)
        deleted = await delete_by_message_id(group_id, message_id)
        if deleted:
            logger.info(f"Removed recalled message {message_id} from history in group {group_id}")
        else:
            logger.debug(f"Recalled message {message_id} not found in history (group {group_id})")

    elif notice_type == "bot_offline":
        msg = data.get("message", "")
        tag = data.get("tag", "")
        logger.warning(f"Bot offline detected: tag={tag}, message={msg}")
        send_desktop_notification("AstraBot 离线通知", "QQ凭证过期，请重新登录")


PluginLoader.load_all()
