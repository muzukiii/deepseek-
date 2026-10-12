"""全局配置：从 .env 文件加载机器人人设、API 密钥、回复概率等参数"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# .env 位于项目根目录（本文件的上上级目录）。
# 必须使用绝对路径：find_dotenv() 依赖调用栈推断位置，在 Windows 上
# 经 nonebot 加载插件后可能解析为空字符串，导致配置全部读不到。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


def _normalize_no_proxy() -> None:
    """清理 NO_PROXY 里的非法条目。

    Clash 等代理工具会写入 ``[::1]``。httpx 会把 NO_PROXY 的每一项
    当作 URL 解析，遇到方括号会抛 ``InvalidURL: Invalid port: ':1]'``，
    导致所有 httpx 客户端创建失败。这里统一去掉括号。
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        parts = []
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
            parts.append(item)
        os.environ[key] = ",".join(parts)


def _load_env() -> None:
    """按绝对路径加载 .env；若不存在则回退到 dotenv 的默认查找。"""
    if _ENV_FILE.is_file():
        load_dotenv(_ENV_FILE, override=False)
    else:
        load_dotenv(override=False)
    _normalize_no_proxy()


@dataclass
class Config:
    """机器人运行配置，所有字段均从环境变量读取"""
    name: str = ""
    name_cn: str = ""
    person_setting: str = ""
    enabled_groups: list[int] = field(default_factory=list)
    output_style: str = ""
    reply_style: str = ""
    extra_style: str = ""
    image_analyzer: str = ""

    keyword: list[str] = field(default_factory=list)

    # 四种回复概率，分别对应：普通消息 / @触发 / 回复中继续搭话 / 回复中被@插入
    reply_rate: float = 0.1
    reply_rate_at: float = 1.0
    reply_rate_in_reply: float = 0.0
    reply_rate_at_in_reply: float = 1.0

    MINIMAX_API_KEY: str = ""

    API_PROTOCOL: str = ""
    API_BASE_URL: str = ""
    API_KEY: str = ""
    API_MODEL: str = ""

    BACK_API_PROTOCOL: str = ""
    BACK_API_BASE_URL: str = ""
    BACK_API_KEY: str = ""
    BACK_API_MODEL: str = ""

    VISUAL_API_KEY: str = ""
    VISUAL_API_PROVIDER: str = "SILICONFLOW"
    VISUAL_API_MODEL: str = ""

    MINIMAX_API_HOST: str = "https://api.minimaxi.com"
    system_prompt: str = ""

    @classmethod
    def load(cls) -> Config:
        _load_env()

        # Windows 的环境变量名大小写不敏感，但保留首次写入时的大小写。
        # 这里统一做一次小写归一化，避免 .env 写的 name 与
        # 系统里已存在的 NAME 冲突时读不到值。
        raw: dict[str, str] = {}
        for key, value in os.environ.items():
            if not key:
                continue
            raw.setdefault(key, value)
            raw.setdefault(key.lower(), value)
        required_str = ["name", "name_cn", "person_setting", "output_style", "reply_style", "extra_style", "image_analyzer"]
        for key in required_str:
            if not raw.get(key):
                raise ValueError(f"Missing required config: {key}")

        required_api = ["API_KEY"]
        for key in required_api:
            if not raw.get(key):
                raise ValueError(f"Missing required config: {key}")

        if not raw.get("API_MODEL"):
            raise ValueError("Missing required config: API_MODEL")

        required_chat_keys = [
            "API_PROTOCOL",
            "API_BASE_URL",
            "API_KEY",
        ]
        for key in required_chat_keys:
            if not raw.get(key):
                raise ValueError(f"Missing required config: {key}")

        enabled_groups_raw = raw.get("enabled_groups", "[]")
        if isinstance(enabled_groups_raw, str):
            enabled_groups = [int(x) for x in ast.literal_eval(enabled_groups_raw)]
        else:
            enabled_groups = [int(x) for x in enabled_groups_raw]

        float_keys = ["reply_rate", "reply_rate_at", "reply_rate_in_reply", "reply_rate_at_in_reply"]
        floats = {}
        for key in float_keys:
            val = raw.get(key, "0.1")
            try:
                floats[key] = float(val)
            except (ValueError, TypeError):
                floats[key] = 0.1

        keyword_raw = raw.get("keyword", "[]")
        if isinstance(keyword_raw, str):
            keyword = ast.literal_eval(keyword_raw)
        else:
            keyword = list(keyword_raw)

        return cls(
            name=raw["name"],
            name_cn=raw["name_cn"],
            person_setting=raw["person_setting"],
            enabled_groups=enabled_groups,
            keyword=keyword,
            output_style=raw["output_style"],
            reply_style=raw["reply_style"],
            extra_style=raw["extra_style"],
            image_analyzer=raw.get("image_analyzer", "请结合上下文描述图片内容。"),
            reply_rate=floats["reply_rate"],
            reply_rate_at=floats["reply_rate_at"],
            reply_rate_in_reply=floats["reply_rate_in_reply"],
            reply_rate_at_in_reply=floats["reply_rate_at_in_reply"],
            MINIMAX_API_KEY=raw.get("MINIMAX_API_KEY", ""),
            API_PROTOCOL=raw.get("API_PROTOCOL", ""),
            API_BASE_URL=raw.get("API_BASE_URL", ""),
            API_KEY=raw.get("API_KEY", ""),
            API_MODEL=raw.get("API_MODEL", ""),
            BACK_API_PROTOCOL=raw.get("BACK_API_PROTOCOL", ""),
            BACK_API_BASE_URL=raw.get("BACK_API_BASE_URL", ""),
            BACK_API_KEY=raw.get("BACK_API_KEY", ""),
            BACK_API_MODEL=raw.get("BACK_API_MODEL", ""),
            VISUAL_API_KEY=raw.get("VISUAL_API_KEY", ""),
            VISUAL_API_PROVIDER=raw.get("VISUAL_API_PROVIDER", "SILICONFLOW"),
            VISUAL_API_MODEL=raw.get("VISUAL_API_MODEL", ""),
            MINIMAX_API_HOST=raw.get("MINIMAX_API_HOST", "https://api.minimaxi.com"),
            system_prompt=raw.get("system_prompt", ""),
        )


_config_instance: Config | None = None


def get_config() -> Config:
    global _config_instance
    if _config_instance is None:
        _config_instance = Config.load()
    return _config_instance
