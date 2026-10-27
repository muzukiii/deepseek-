"""日志管理：统一日志格式，同时输出到文件

logger 优先用 NoneBot 官方那个（实测 `nonebot.logger is loguru.logger`，同一个对象），
没有 nonebot 的环境（例如把记忆/历史层当库引到 AstrBot 侧）退回 loguru —— 拿到的是
同一个 logger，行为不变，只是少一层平台依赖。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

try:
    from nonebot import logger
except ImportError:
    from loguru import logger

_DATA_DIR_ENV = os.environ.get("ASTRA_DATA_DIR")
# 不设 ASTRA_DATA_DIR 时路径与历史完全一致（面板与启动器读的就是这个目录）。
# 这里不 import chat_service.paths 是为了避免 logmanager -> chat_service 的反向依赖。
LOG_DIR = (
    Path(_DATA_DIR_ENV) / "logs"
    if _DATA_DIR_ENV
    else Path(__file__).resolve().parent.parent / "chat_service" / "data" / "logs"
)


def _setup_file_sink():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = LOG_DIR / f"astrabot_{timestamp}.log"
    logger.add(
        file_path,
        encoding="utf-8",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )


_setup_file_sink()
