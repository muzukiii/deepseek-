"""数据目录的唯一来源。

原先 9 个模块各自写 `Path(__file__).parent / "data"`，等于没有"数据根目录"这个概念，
带来两个真实问题：

1. 换部署搬不走 —— 接进 AstrBot 时没法一句话把记忆/历史/导出一整体换到新目录；
2. **回灌源的坑**：`memories.json` 的路径与 `memories.db` 不同源。2026-10-27 实测，
   只把 enhanced_memory 的 DATA_DIR/MEMORY_DB 指到别处，启动迁移仍会从**原目录**
   读走 `memories.json`，把已删的 349 条灌回新库。

优先级：环境变量 `ASTRA_DATA_DIR` > 默认（本包下的 `data/`，与历史行为一致）。
面板与启动器读的就是 `astrabot/chat_service/data`，不设环境变量时路径不变。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"

DATA_DIR = Path(os.environ.get("ASTRA_DATA_DIR") or DEFAULT_DATA_DIR)

MEMORY_DB = DATA_DIR / "memories.db"
HISTORY_DB = DATA_DIR / "history.db"
MEMORIES_PATH = DATA_DIR / "memories.json"
IGNORE_PATH = DATA_DIR / "ignores.json"
EXPORT_DIR = DATA_DIR / "exports"
