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


def _get_httpx_client(timeout: int = IMAGE_TIMEOUT):
    """兼容旧调用点的包装，统一走 http_client。"""
    return get_http_client(timeout=timeout)


async def download_image_base64(url: str, timeout: int = IMAGE_TIMEOUT) -> str | None:
    try:
        resp = await request_with_retry(
            "GET",
            url,
            timeout=timeout,
            follow_redirects=True,
        )
        b64 = base64.b64encode(resp.content).decode("utf-8")
        return b64
    except Exception as e:
        # 区分超时与连接失败，便于快速定位是网络问题还是 URL 失效
        kind = "timeout" if "Timeout" in type(e).__name__ else type(e).__name__
        logger.error(f"Failed to download image ({kind}) {url}: {e}")
        return None
