"""AstraBot 入口文件。

用于 `python bot.py` 或 `nb run` 启动。

关键点：
1. 必须在 `nonebot.init()` 之前调用 `load_dotenv()`，否则 .env 中的
   DRIVER / HOST / PORT 等配置不会被 nonebot 读取，驱动会退化为
   默认的 ~fastapi（不支持 WebSocket 客户端连接）。
2. 需要吸收反向 WebSocket 的断连异常，否则 SnowLuma 重启时整个
   机器人进程会直接退出。
"""

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows 控制台默认 GBK，日志里的 emoji 会触发 UnicodeEncodeError。
# 这里统一切到 UTF-8 并忽略无法编码的字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 必须以绝对路径加载：find_dotenv() 会依据调用栈推断位置，
# 在部分环境下（尤其 Windows + 插件动态加载）会解析为空，导致
# DRIVER / HOST / PORT 等全部读不到，驱动退化为 ~fastapi。
load_dotenv(Path(__file__).resolve().parent / ".env")

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.log import logger

# 这些异常只表示"连接断了"。OneBot 适配器内部有 while True 重连循环，
# 不该因为一次断连就让整个进程退出。
_TRANSIENT = frozenset(
    {
        "WebSocketClosed",
        "ConnectionClosed",
        "ConnectionClosedError",
        "ConnectionClosedOK",
        "ConnectionResetError",
        "ClientConnectorError",
        "ServerDisconnectedError",
    }
)


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """把未被回收的连接类异常降级为警告，其余仍照常报错。"""
    exc = context.get("exception")
    message = context.get("message", "")

    if isinstance(exc, BaseException) and type(exc).__name__ in _TRANSIENT:
        logger.warning(f"连接中断（{type(exc).__name__}），等待适配器自动重连...")
        return

    if isinstance(exc, OSError):
        logger.warning(f"网络异常（{type(exc).__name__}: {exc}），已忽略")
        return

    if exc is not None:
        logger.opt(exception=exc).error(f"未处理的任务异常: {message}")
    elif message:
        logger.debug(f"asyncio: {message}")


def _patch_uvicorn_server() -> None:
    """让 uvicorn 创建的事件循环也带上我们的异常处理。

    `nonebot.run()` 最终走的是同步的 `uvicorn.run()`，它会在内部新建
    事件循环。这里包装 `Server.run`，在进入 `asyncio.run` 之前给当前
    循环挂上 handler。
    """
    try:
        from uvicorn.server import Server
    except Exception:  # pragma: no cover
        return

    original_run = Server.run

    def patched_run(self, sockets=None):
        # uvicorn 在 asyncio.run 内部才建 loop，这里通过 startup 钩子挂载
        original_serve = self.serve

        async def serve_with_handler(*args, **kwargs):
            asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
            return await original_serve(*args, **kwargs)

        self.serve = serve_with_handler
        return original_run(self, sockets=sockets)

    Server.run = patched_run


nonebot.init()

_driver = nonebot.get_driver()
_driver.register_adapter(Adapter)

nonebot.load_plugins("astrabot")


@_driver.on_shutdown
async def _close_shared_http_client() -> None:
    """进程退出时关闭共享 httpx 连接池，优雅释放 keep-alive 连接。"""
    from astrabot.chat_service.http_client import aclose_http_client

    await aclose_http_client()


_patch_uvicorn_server()


def _network_selfcheck() -> None:
    """启动时探测直连与代理，把结果写进日志。

    之前排查"图片下载失败/AI 调用失败"时，无法快速判断是代理挂了
    还是服务本身不可达，只能事后翻日志。这里提前给出结论。
    """
    try:
        from astrabot.chat_service.http_client import probe_connectivity, use_proxy

        results = asyncio.run(probe_connectivity())
        mode = "代理" if use_proxy() else "直连"
        logger.info(
            f"网络自检: 当前模式={mode} "
            f"直连={'可用' if results.get('direct') else '不可用'} "
            f"代理={'可用' if results.get('proxy') else '不可用'}"
        )
        if not results.get("direct") and not results.get("proxy"):
            logger.warning("网络自检: 直连与代理均不可用，请检查网络或代理设置")
    except Exception as e:  # 自检失败不应影响启动
        logger.debug(f"网络自检跳过: {e}")


if __name__ == "__main__":
    _network_selfcheck()
    nonebot.run()
