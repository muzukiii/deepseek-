"""图片工具：下载图片并转换为 base64 编码"""

from __future__ import annotations

import base64

from astrabot.chat_service.http_client import (
    DEFAULT_TIMEOUT,
    get_http_client,
    request_with_retry,
)
from astrabot.logmanager.logger import logger

# 腾讯图片 CDN 的 URL 带 rkey 鉴权且体积可能较大，超时给足；
# 网络抖动时靠重试兜底，而不是直接放弃。
IMAGE_TIMEOUT = DEFAULT_TIMEOUT

# 常见图片格式的文件头。用于推断真实 MIME —— 曾经这里一律按
# image/jpeg 发送，而群里实际传的常是 GIF/PNG，格式声明错误会让
# 模型解析出问题或降质。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def detect_mime(raw: bytes) -> str:
    """按文件头推断图片 MIME，识别不出时退回 jpeg。"""
    for sig, mime in _MAGIC:
        if raw.startswith(sig):
            return mime
    # WEBP 是 RIFF 容器，需要看第 8-12 字节
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def to_data_uri(raw: bytes) -> str:
    """把图片字节转成带正确 MIME 的 data URI。"""
    mime = detect_mime(raw)
    return f"data:{mime};base64,{base64.b64encode(raw).decode('utf-8')}"


def _get_httpx_client(timeout: int = IMAGE_TIMEOUT):
    """兼容旧调用点的包装，统一走 http_client。"""
    return get_http_client(timeout=timeout)


async def download_image(url: str, timeout: int = IMAGE_TIMEOUT) -> bytes | None:
    """下载图片原始字节，失败返回 None。"""
    try:
        resp = await request_with_retry(
            "GET",
            url,
            timeout=timeout,
            follow_redirects=True,
        )
        return resp.content
    except Exception as e:
        # 区分超时与连接失败，便于快速定位是网络问题还是 URL 失效
        kind = "timeout" if "Timeout" in type(e).__name__ else type(e).__name__
        logger.error(f"Failed to download image ({kind}) {url}: {e}")
        return None


async def download_image_base64(url: str, timeout: int = IMAGE_TIMEOUT) -> str | None:
    """下载图片并返回 base64（不含 data URI 前缀，保持旧调用点兼容）。"""
    raw = await download_image(url, timeout=timeout)
    if raw is None:
        return None
    return base64.b64encode(raw).decode("utf-8")
