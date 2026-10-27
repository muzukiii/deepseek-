"""AstraBot 聊天服务包。

本文件刻意不 import 任何东西：原先这里直接写着 NoneBot 的 matcher 注册，于是
`import astrabot.chat_service.enhanced_memory` 也会连带拉起 nonebot + OneBot 适配器，
把零平台依赖的记忆/历史层拖进平台。注册代码已挪到 entry.py，由 bot.py 显式加载。
"""
