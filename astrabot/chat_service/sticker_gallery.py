"""表情图来源：本地自建库优先，远程 GitHub 图库作为补充。

本地库参见 sticker_store（把群里出现过的图片存下来并附描述）。
远程库没有描述，只作为兜底，避免本地还没攒够图时无图可用。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from astrabot.chat_service import sticker_store
from astrabot.chat_service.text_tokenizer import term_list
from astrabot.logmanager.logger import logger

GALLERY_INDEX_URL = "https://raw.githubusercontent.com/muzukiii/-/main/index.json"
_cached: tuple[float, list[dict]] = (0.0, [])

# 策展描述库：随仓库提交的 images/<hash> -> 中文语境描述，由 deepseek-harness
# 用视觉模型批量生成后同步过来。远程 index.json 本身不带描述，故以此为准。
_CURATED_PATH = Path(__file__).resolve().parent / "sticker_desc.json"
_curated_cache: tuple[float, dict[str, str]] = (0.0, {})


def _remote_id(path: str) -> str:
    """与 list_stickers 一致：取 images/<hash>.<ext> 的 hash 前 12 位。"""
    return path.rsplit("/", 1)[-1].split(".", 1)[0][:12]


def curated_descriptions(max_age: float = 300.0) -> dict[str, str]:
    """加载策展描述 -> {remote_id: description}，过滤 __aifail__/__dlfail__ 失败项。

    以 5 分钟为周期按需重载，失败项不进入候选，避免把错误标记当描述发给模型。
    """
    global _curated_cache
    now = time.monotonic()
    if _curated_cache[1] and now - _curated_cache[0] < max_age:
        return _curated_cache[1]
    out: dict[str, str] = {}
    try:
        data = json.loads(_CURATED_PATH.read_text(encoding="utf-8"))
        for path, desc in data.items():
            if not isinstance(desc, str):
                continue
            desc = desc.strip()
            if not desc or desc.startswith("__"):
                continue
            out[_remote_id(path)] = desc
    except Exception as e:
        logger.debug(f"Curated sticker descriptions unavailable: {e}")
        return _curated_cache[1]  # 回退到上次成功结果
    _curated_cache = (now, out)
    return out


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
    # 远程 index.json 不带描述，用策展描述库按 id 对齐补全
    curated = curated_descriptions()
    for s in stickers:
        if not s["description"]:
            s["description"] = curated.get(s["id"], "")
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


def _match_score(query_tokens: set[str], desc_tokens: set[str]) -> float:
    """查询与描述的大义重叠度：交集 / sqrt(描述词数)。

    除以描述长度的平方根，避免长描述仅因词多而天然更易命中。
    """
    if not query_tokens or not desc_tokens:
        return 0.0
    inter = len(query_tokens & desc_tokens)
    if inter == 0:
        return 0.0
    return inter / (len(desc_tokens) ** 0.5)


async def sticker_prompt_catalog(limit: int = 20, query: str = "") -> str:
    """合并本地自建库与远程策展图库，按「与当前语境的语义相关度」挑选表情目录。

    - 候选来自两处：本地库（含识图描述）+ 远程策展描述（sticker_desc.json）；
    - 给了 query 时按 bigram 重叠度排序，让机器人发「贴合当前话题」的表情，
      而不是无脑复用发得最多的那张；query 为空时退化为按使用次数排序（旧行为）；
    - 任何一条都没描述时返回空串，模型便不会收到表情目录。
    """
    candidates: dict[str, dict] = {}

    for it in sticker_store.all_stickers():
        desc = (it.get("description") or "").strip()
        if not desc:
            continue
        candidates[str(it["id"])] = {
            "id": str(it["id"]),
            "description": desc,
            "uses": it.get("uses", 0),
            "created_at": it.get("created_at", 0),
        }

    for s in await list_stickers():
        desc = (s.get("description") or "").strip()
        if not desc:
            continue
        candidates.setdefault(
            s["id"],
            {"id": s["id"], "description": desc, "uses": 0, "created_at": 0},
        )

    if not candidates:
        return ""

    query_tokens = set(term_list(query, limit=16)) if query else set()
    ranked = []
    for c in candidates.values():
        score = _match_score(query_tokens, set(term_list(c["description"], limit=24)))
        ranked.append((score, c["uses"], c["created_at"], c))
    ranked.sort(key=lambda x: (-x[0], -x[1], -x[2]))

    return "\n".join(
        f"{c['id']}: {c['description'][:100]}"
        for _, _, _, c in ranked[:limit]
    )
