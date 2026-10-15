"""本地表情包库：把群里出现过的图片落盘保存，供之后按需发送。

与 sticker_gallery（远程 GitHub 图库）互补：
- 本模块负责「本地自建库」：识别群图 -> 存盘 -> 带描述索引
- 远程库仍作为补充来源保留

设计要点：
- 用图片内容的 SHA-256 前 12 位作为 id，同一张图重复发送不会重复入库
- 索引与图片文件都放在 data/stickers/ 下（已被 .gitignore 覆盖）
- 任何异常都不阻塞主聊天流程，失败只记日志
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from pathlib import Path

from astrabot.logmanager.logger import logger

DATA_DIR = Path(__file__).resolve().parent / "data"
STICKER_DIR = DATA_DIR / "stickers"
INDEX_FILE = STICKER_DIR / "index.json"

# 单张图片上限，超过则不入库（避免把磁盘塞满）
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 默认库容量上限
DEFAULT_MAX_COUNT = 200

_lock = threading.RLock()


def _load_index() -> dict:
    """读取索引，损坏时安全回退为空索引。"""
    if not INDEX_FILE.is_file():
        return {}
    try:
        with INDEX_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        logger.warning(f"sticker index corrupted, rebuilding: {e}")
        return {}
    except Exception as e:
        logger.debug(f"Failed to load sticker index: {e}")
        return {}


def _save_index(index: dict) -> None:
    with _lock:
        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        tmp.replace(INDEX_FILE)


def _sha_id(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:12]


def save_sticker(
    image_bytes: bytes,
    description: str,
    *,
    max_count: int = DEFAULT_MAX_COUNT,
) -> str | None:
    """保存一张表情图，返回其 id；未保存则返回 None。"""
    if not image_bytes:
        return None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        logger.debug(
            f"Sticker skipped: {len(image_bytes)} bytes exceeds {MAX_IMAGE_BYTES}"
        )
        return None

    description = (description or "").strip()
    if not description:
        # 没有描述就无法让模型按语境挑选，存了也用不上
        return None

    sid = _sha_id(image_bytes)

    with _lock:
        index = _load_index()

        if sid in index:
            index[sid]["uses"] = index[sid].get("uses", 0) + 1
            _save_index(index)
            return sid

        # 容量控制：按使用次数升序、时间升序淘汰，保证常用图留存
        if len(index) >= max_count:
            victims = sorted(
                index.items(),
                key=lambda kv: (kv[1].get("uses", 0), kv[1].get("created_at", 0)),
            )
            for old_id, meta in victims[: max(1, len(index) - max_count + 1)]:
                try:
                    (STICKER_DIR / meta.get("file", "")).unlink(missing_ok=True)
                except Exception as e:
                    logger.debug(f"Failed to remove evicted sticker {old_id}: {e}")
                index.pop(old_id, None)

        STICKER_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{sid}.jpg"
        try:
            (STICKER_DIR / filename).write_bytes(image_bytes)
        except Exception as e:
            logger.error(f"Failed to write sticker file: {e}")
            return None

        index[sid] = {
            "file": filename,
            "description": description,
            "created_at": int(time.time()),
            "uses": 0,
        }
        _save_index(index)

    logger.info(f"Sticker saved: {sid} ({description[:30]})")
    return sid


def get_sticker(sticker_id: str) -> dict | None:
    index = _load_index()
    meta = index.get(str(sticker_id))
    if not meta:
        return None
    path = STICKER_DIR / meta.get("file", "")
    if not path.is_file():
        return None
    return {**meta, "id": str(sticker_id), "path": path}


def sticker_path(sticker_id: str) -> Path | None:
    meta = get_sticker(sticker_id)
    return meta["path"] if meta else None


def mark_used(sticker_id: str) -> None:
    with _lock:
        index = _load_index()
        if str(sticker_id) in index:
            index[str(sticker_id)]["uses"] = index[str(sticker_id)].get("uses", 0) + 1
            _save_index(index)


def all_stickers() -> list[dict]:
    index = _load_index()
    out = []
    for sid, meta in index.items():
        path = STICKER_DIR / meta.get("file", "")
        if path.is_file():
            out.append({**meta, "id": sid, "path": path})
    return out


def catalog_for_prompt(limit: int = 20) -> str:
    """生成给模型挑选用的目录文本，按使用次数降序取前 limit 条。"""
    items = all_stickers()
    if not items:
        return ""
    items.sort(key=lambda x: (-x.get("uses", 0), -x.get("created_at", 0)))
    return "\n".join(
        f"{it['id']}: {it['description'][:60]}" for it in items[:limit]
    )


def stats() -> dict:
    items = all_stickers()
    total_bytes = sum(it["path"].stat().st_size for it in items)
    return {
        "count": len(items),
        "bytes": total_bytes,
        "used": sum(1 for it in items if it.get("uses", 0) > 0),
    }


def image_b64_to_bytes(b64: str) -> bytes | None:
    """把 analyze_images 用的 base64 还原成字节。"""
    try:
        return base64.b64decode(b64, validate=False)
    except Exception as e:
        logger.debug(f"Failed to decode image base64: {e}")
        return None
