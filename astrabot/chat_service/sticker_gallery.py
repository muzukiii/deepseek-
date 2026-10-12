"""从 GitHub 图库读取远程图片索引，不在本地持久化图片。"""

from __future__ import annotations

import time

import httpx

GALLERY_INDEX_URL = "https://raw.githubusercontent.com/muzukiii/-/main/index.json"
_cached: tuple[float, list[dict]] = (0.0, [])


async def list_stickers() -> list[dict]:
    global _cached
    if _cached[1] and time.monotonic() - _cached[0] < 3600:
        return _cached[1]
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        response = await client.get(GALLERY_INDEX_URL)
        response.raise_for_status()
        data = response.json()
    stickers = [
        {"id": item["path"].rsplit("/", 1)[-1].split(".", 1)[0][:12], "url": item["url"], "description": item.get("description", "")}
        for item in data
        if isinstance(item, dict)
        and item.get("path", "").startswith("images/")
        and item.get("url", "").startswith("https://raw.githubusercontent.com/muzukiii/-/")
    ]
    _cached = (time.monotonic(), stickers)
    return stickers


async def sticker_url(sticker_id: str | int | None) -> str | None:
    if sticker_id is None or str(sticker_id).strip().lower() in {"", "null", "none"}:
        return None
    wanted = str(sticker_id).strip()
    for sticker in await list_stickers():
        if wanted == sticker["id"] and sticker["description"]:
            return sticker["url"]
    return None


async def sticker_prompt_catalog() -> str:
    stickers = await list_stickers()
    return "\n".join(
        f"{item['id']}: {item['description']}"
        for item in stickers
        if item["description"]
    )
