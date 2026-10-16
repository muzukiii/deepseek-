"""统一的 HTTP 客户端构造与重试工具。

背景：项目里原先在 ai_client / image_utils / search_engine 各写了一份
几乎相同的 `_get_httpx_client()`，超时与代理策略不一致，且都依赖 httpx
默认读取环境变量（HTTP_PROXY / HTTPS_PROXY）。

当本机开着 Clash 之类的代理时，所有请求都会被塞进那一跳：代理一重启或
切节点，连国内可直连的服务（DeepSeek、腾讯图片 CDN）也会一起失败。
实测这两者直连均可用，因此默认改为直连，并允许通过 .env 切回代理。
"""

from __future__ import annotations

import asyncio
import os

import httpx

from astrabot.logmanager.logger import logger

DEFAULT_TIMEOUT = 30.0

# 连接池上限：单进程共享一个 AsyncClient，复用 keep-alive 连接，
# 避免每次 AI/图片/搜索调用都重新做 TCP+TLS 握手。
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=32)

# 进程级共享客户端。httpx 客户端绑定到首次使用它的事件循环，因此这里
# 按 running loop 惰性创建并记录其 id；NoneBot 热重载若切换了循环会重建。
_shared_client: httpx.AsyncClient | None = None
_shared_loop_id: int | None = None

# 连接类异常：重试可能有效；HTTP 4xx 之类则不该重试
RETRYABLE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def use_proxy() -> bool:
    """是否让 httpx 读取环境里的代理设置。

    优先级：ALL_PROXY 显式配置 > network_use_proxy 开关 > 默认直连。
    """
    explicit = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    if explicit:
        return True
    flag = str(os.environ.get("network_use_proxy", "false")).strip().lower()
    return flag in ("1", "true", "yes", "on")


def _explicit_proxy() -> str | None:
    proxy = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    if proxy and proxy.startswith("socks://"):
        proxy = proxy.replace("socks://", "socks5://", 1)
    return proxy


def _new_client(timeout: float) -> httpx.AsyncClient:
    explicit = _explicit_proxy()
    if explicit:
        return httpx.AsyncClient(
            proxy=explicit, timeout=timeout, limits=_LIMITS, trust_env=False
        )
    return httpx.AsyncClient(
        timeout=timeout, limits=_LIMITS, trust_env=use_proxy()
    )


def get_http_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """返回进程内共享的 httpx 客户端（连接池复用）。

    调用方**不要**关闭返回的客户端——进程退出时由 aclose_http_client()
    统一释放。这里的 timeout 仅作为客户端级默认；各调用点通过 SDK 或
    request 的 timeout 参数做请求级覆盖，因此不同调用点互不影响。

    不在事件循环内（如启动自检）时退回一次性客户端，交由调用方 async-with 关闭。
    """
    global _shared_client, _shared_loop_id
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        return _new_client(timeout)

    if (
        _shared_client is None
        or _shared_loop_id != loop_id
        or _shared_client.is_closed
    ):
        _shared_client = _new_client(timeout)
        _shared_loop_id = loop_id
    return _shared_client


async def aclose_http_client() -> None:
    """关闭共享客户端，释放连接池。在应用 shutdown 钩子里调用。"""
    global _shared_client, _shared_loop_id
    client = _shared_client
    _shared_client = None
    _shared_loop_id = None
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as e:
            logger.debug(f"Failed to close shared http client: {e}")


async def request_with_retry(
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    backoff: tuple[float, ...] = (0.5, 1.5),
    **kwargs,
) -> httpx.Response:
    """带退避重试的请求。

    只对连接类异常重试；HTTP 状态码错误（4xx/5xx）直接抛出，避免
    在鉴权失败等确定性错误上浪费时间。复用共享连接池，故不在此关闭客户端；
    timeout 作为请求级参数传入，覆盖客户端默认值。
    """
    client = get_http_client()
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            response = await client.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except RETRYABLE as e:
            last_exc = e
            if attempt >= retries:
                break
            delay = backoff[min(attempt, len(backoff) - 1)]
            logger.debug(
                f"{method} {url} failed ({type(e).__name__}), "
                f"retry {attempt + 1}/{retries} in {delay}s"
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


async def probe_connectivity() -> dict[str, bool]:
    """启动自检：分别探测直连与代理是否可用，结果写入日志。

    仅用于诊断，失败不影响启动。
    """
    results: dict[str, bool] = {}
    for label, trust in (("direct", False), ("proxy", True)):
        try:
            async with httpx.AsyncClient(timeout=8.0, trust_env=trust) as c:
                r = await c.get("https://api.deepseek.com")
            results[label] = r.status_code < 500
        except Exception:
            results[label] = False
    return results
