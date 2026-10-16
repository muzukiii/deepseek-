"""表情图来源：本地自建库优先，远程 GitHub 图库作为补充。

本地库参见 sticker_store（把群里出现过的图片存下来并附描述）。
远程库没有描述，只作为兜底，避免本地还没攒够图时无图可用。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from astrabot.chat_service import sticker_store
from astrabot.chat_service.text_tokenizer import term_list
from astrabot.logmanager.logger import logger

GALLERY_INDEX_URL = "https://raw.githubusercontent.com/muzukiii/-/main/index.json"

# 策展描述库：随仓库提交的 images/<hash> -> 中文语境描述，由 deepseek-harness
# 用视觉模型批量生成后同步过来。远程 index.json 本身不带描述，故以此为准。
_CURATED_PATH = Path(__file__).resolve().parent / "sticker_desc.json"
_curated_cache: tuple[float, dict[str, str]] = (0.0, {})

# 远程图库索引缓存：内存新鲜窗口内直接返回，过期则「先给旧数据 + 后台刷新」，
# 并落盘以便重启后不必冷取、断网时回退到上次成功结果。
_DATA_DIR = Path(__file__).resolve().parent / "data"
_CACHE_FILE = _DATA_DIR / "gallery_cache.json"
FRESH_TTL = 3600.0  # 内存视为新鲜的时长（秒）
STALE_MAX_AGE = 7 * 86400.0  # 可回退提供陈旧数据的最大年龄（墙钟秒）

_cached: tuple[float, list[dict]] = (0.0, [])  # (monotonic 抓取时刻, stickers)
_fetched_wall: float = 0.0  # 上次成功抓取的墙钟时间
_have_data = False  # 是否已成功抓到过（含空列表）
_disk_loaded = False
_refresh_task: asyncio.Task | None = None


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


def _build_stickers(data: list) -> list[dict]:
    """把远程 index.json 原始条目转为 {id,url,description}，并用策展描述补全空描述。"""
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
    curated = curated_descriptions()
    for s in stickers:
        if not s["description"]:
            s["description"] = curated.get(s["id"], "")
    return stickers


def _load_disk() -> None:
    """进程内首次调用时从磁盘缓存恢复，避免重启后冷取。"""
    global _cached, _fetched_wall, _have_data, _disk_loaded
    if _disk_loaded:
        return
    _disk_loaded = True
    try:
        obj = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        stickers = obj.get("stickers")
        ts = float(obj.get("fetched_at", 0))
        if isinstance(stickers, list) and ts and time.time() - ts <= STALE_MAX_AGE:
            # 把单调时钟基准回拨到「age 秒前」，让新鲜度判定保持一致
            _cached = (time.monotonic() - (time.time() - ts), stickers)
            _fetched_wall = ts
            _have_data = True
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"Gallery disk cache load skipped: {e}")


def _save_disk(stickers: list[dict], wall: float) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"fetched_at": wall, "stickers": stickers}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(_CACHE_FILE)
    except Exception as e:
        logger.debug(f"Gallery disk cache save failed: {e}")


async def _fetch_remote() -> list[dict] | None:
    """抓取远程索引并合并策展描述；失败返回 None（区别于成功但为空的列表）。"""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(GALLERY_INDEX_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.debug(f"Remote sticker gallery unavailable: {e}")
        return None
    return _build_stickers(data)


async def _refresh() -> None:
    """后台刷新：成功则更新内存 + 落盘；失败保留旧数据下次再试。"""
    global _cached, _fetched_wall, _have_data
    stickers = await _fetch_remote()
    if stickers is None:
        return
    _cached = (time.monotonic(), stickers)
    _fetched_wall = time.time()
    _have_data = True
    _save_disk(stickers, _fetched_wall)


def _schedule_refresh() -> None:
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    try:
        _refresh_task = asyncio.create_task(_refresh())
    except RuntimeError:
        # 无运行中的事件循环（不应发生，list_stickers 在 async 路径）：跳过后台刷新
        pass


async def list_stickers() -> list[dict]:
    """远程图库条目（本地库请用 sticker_store.all_stickers）。

    新鲜期内存直返；过期先返回旧数据并触发后台 revalidate（stale-while-revalidate），
    仅在从未抓到、或旧数据已超陈旧窗口时才阻塞抓取。
    """
    global _cached, _fetched_wall, _have_data
    _load_disk()

    if _have_data and time.monotonic() - _cached[0] < FRESH_TTL:
        return _cached[1]

    if _have_data and time.time() - _fetched_wall <= STALE_MAX_AGE:
        _schedule_refresh()  # 陈旧可容忍：立即返回旧数据，后台刷新
        return _cached[1]

    # 无数据或已超陈旧窗口：阻塞抓取
    stickers = await _fetch_remote()
    if stickers is None:
        return _cached[1] if _have_data else []
    _cached = (time.monotonic(), stickers)
    _fetched_wall = time.time()
    _have_data = True
    _save_disk(stickers, _fetched_wall)
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
