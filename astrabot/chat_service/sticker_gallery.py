"""表情图来源：本地自建库优先，远程 GitHub 图库作为补充。

本地库参见 sticker_store（把群里出现过的图片存下来并附描述）。
远程库没有描述，只作为兜底，避免本地还没攒够图时无图可用。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from astrabot.chat_service import sticker_store
from astrabot.logmanager.logger import logger

GALLERY_INDEX_URL = "https://raw.githubusercontent.com/muzukiii/-/main/index.json"
_cached: tuple[float, list[dict]] = (0.0, [])


async def list_stickers() -> list[dict]:
    """远程图库条目（本地库请用 sticker_store.all_stickers）。"""
    global _cached
    if _cached[1] and time.monotonic() - _cached[0] < 3600:
        return _cached[1]
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(GALLERY_INDEX_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.debug(f"Remote sticker gallery unavailable: {e}")
        return _cached[1] if _cached[1] else []

    stickers = [
        {
            "id": item["path"].rsplit("/", 1)[-1].split(".", 1)[0][:12],
            "url": item["url"],
            "description": item.get("description", ""),
        }
        for item in data
        if isinstance(item, dict)
        and item.get("path", "").startswith("images/")
        and item.get("url", "").startswith("https://raw.githubusercontent.com/muzukiii/-/")
    ]
    _cached = (time.monotonic(), stickers)
    return stickers


async def resolve_sticker(sticker_id: str | int | None) -> str | Path | None:
    """按 id 取图：本地库优先，未命中再查远程库。

    返回本地 Path 时，MessageSegment.image 会转成 file:// URI；
    返回 str 时是远程 http(s) 地址。
    """
    if sticker_id is None or str(sticker_id).strip().lower() in {"", "null", "none"}:
        return None
    wanted = str(sticker_id).strip()

    local = sticker_store.sticker_path(wanted)
    if local is not None:
        sticker_store.mark_used(wanted)
        return local

    for sticker in await list_stickers():
        if wanted == sticker["id"]:
            return sticker["url"]
    return None


# 兼容旧调用点
async def sticker_url(sticker_id: str | int | None) -> str | Path | None:
    return await resolve_sticker(sticker_id)


async def sticker_prompt_catalog(limit: int = 20) -> str:
    """合并本地与远程的可选表情目录，供模型挑选。

    本地条目带描述（可被理解），远程条目大多无描述，因此本地优先，
    只有本地为空时才给出远程 id，避免模型在无描述的情况下瞎猜。
    """
    local = sticker_store.catalog_for_prompt(limit=limit)
    if local:
        return local

    stickers = await list_stickers()
    described = [s for s in stickers if s["description"]]
    if not described:
        return ""
    return "\n".join(
        f"{s['id']}: {s['description'][:60]}" for s in described[:limit]
    )
