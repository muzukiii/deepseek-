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

    # 引用回复：回复时引用触发的那条消息（QQ 气泡上方显示"回复 XXX"）
    quote_reply: bool = False

    # 上下文规模：聊天记录条数 / 中期记忆条数
    # 聊天记录是"原文"，记忆是"摘要"。摘要过长会挤占模型对当前对话的注意力，
    # 导致答非所问、上下文割裂，因此记忆条数应显著少于聊天记录。
    history_messages: int = 25
    memory_facts: int = 6
    long_term_memories: int = 12

    # 识图：默认复用主 API（DeepSeek 多模态），无需额外 Key。
    # 单张图 token 有硬上限（≤1024），成本很低，但仍限制单次图片数量，
    # 避免群内刷图时一次性放大开销。
    vision_provider: str = "MAIN"
    vision_detail: str = "low"
    vision_max_images: int = 3

    # 戳一戳：被戳时是否回应，以及是否回戳对方
    poke_reply: bool = True
    poke_back: bool = True

    # 命令白名单：只有这些 QQ 可以使用 /用量、/memory 等斜杠命令。
    # 留空表示不限制（任何人都能用）。
    command_allowed_users: set[str] = field(default_factory=set)

    # 本地表情包库：把群里出现过的图片识别后存到本地，供之后按需发送
    sticker_enabled: bool = True
    sticker_autosave: bool = True
    sticker_max_count: int = 200
    sticker_catalog_size: int = 20

    # 防互刷：
    # ignore_users 里的 QQ 完全不回复（即使被 @）——用于排除其它机器人
    ignore_users: set[str] = field(default_factory=set)
    # 同一人连续被回复的上限，超过则停止；0 表示不限制。
    # 用于兜底：即便出现未知的循环，也会在若干轮后自动停下。
    max_reply_streak: int = 3

    # 生气冷落：模型判定被某人惹到时，短暂无视对方（默认 10 分钟）
    mood_enabled: bool = True
    mood_angry_seconds: int = 600
    # 冷落期间是否连 @ 都不理。True 更"真性情"，False 则只在非 @ 时冷落。
    mood_ignore_at: bool = True

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

        quote_reply = str(raw.get("quote_reply", "false")).strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        def _int_env(key: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int(str(raw.get(key, default)).strip())))
            except (ValueError, TypeError):
                return default

        history_messages = _int_env("history_messages", 25, 5, 100)
        memory_facts = _int_env("memory_facts", 6, 0, 30)
        long_term_memories = _int_env("long_term_memories", 12, 0, 100)
        vision_max_images = _int_env("vision_max_images", 3, 1, 20)

        vision_provider = str(raw.get("vision_provider", "MAIN")).strip().upper() or "MAIN"
        vision_detail = str(raw.get("vision_detail", "low")).strip().lower()
        if vision_detail not in ("low", "high", "original", "auto"):
            vision_detail = "low"

        def _bool_env(key: str, default: bool) -> bool:
            val = str(raw.get(key, default)).strip().lower()
            return val in ("1", "true", "yes", "on")

        poke_reply = _bool_env("poke_reply", True)
        poke_back = _bool_env("poke_back", True)

        # 命令白名单：支持 command_allowed_users=[123,456] 或 123,456
        allowed_raw = raw.get("command_allowed_users", "")
        allowed_users: set[str] = set()
        if allowed_raw:
            try:
                parsed = (
                    ast.literal_eval(allowed_raw)
                    if isinstance(allowed_raw, str) and allowed_raw.strip().startswith("[")
                    else str(allowed_raw).split(",")
                )
                if isinstance(parsed, (list, tuple, set)):
                    allowed_users = {str(x).strip() for x in parsed if str(x).strip()}
                else:
                    allowed_users = {str(parsed).strip()}
            except (ValueError, SyntaxError):
                allowed_users = {
                    x.strip() for x in str(allowed_raw).split(",") if x.strip()
                }

        sticker_enabled = _bool_env("sticker_enabled", True)
        sticker_autosave = _bool_env("sticker_autosave", True)
        sticker_max_count = _int_env("sticker_max_count", 200, 0, 5000)
        sticker_catalog_size = _int_env("sticker_catalog_size", 20, 0, 100)

        max_reply_streak = _int_env("max_reply_streak", 3, 0, 50)
        mood_enabled = _bool_env("mood_enabled", True)
        mood_angry_seconds = _int_env("mood_angry_seconds", 600, 30, 86400)
        mood_ignore_at = _bool_env("mood_ignore_at", True)

        # 忽略名单：支持 ignore_users=[123,456] 或 123,456
        ignore_raw = raw.get("ignore_users", "")
        ignore_users: set[str] = set()
        if ignore_raw:
            try:
                parsed_ignore = (
                    ast.literal_eval(ignore_raw)
                    if isinstance(ignore_raw, str) and ignore_raw.strip().startswith("[")
                    else str(ignore_raw).split(",")
                )
                if isinstance(parsed_ignore, (list, tuple, set)):
                    ignore_users = {
                        str(x).strip() for x in parsed_ignore if str(x).strip()
                    }
                else:
                    ignore_users = {str(parsed_ignore).strip()}
            except (ValueError, SyntaxError):
                ignore_users = {
                    x.strip() for x in str(ignore_raw).split(",") if x.strip()
                }

        return cls(
            name=raw["name"],
            name_cn=raw["name_cn"],
            person_setting=raw["person_setting"],
            enabled_groups=enabled_groups,
            keyword=keyword,
            command_allowed_users=allowed_users,
            ignore_users=ignore_users,
            max_reply_streak=max_reply_streak,
            mood_enabled=mood_enabled,
            mood_angry_seconds=mood_angry_seconds,
            mood_ignore_at=mood_ignore_at,
            sticker_enabled=sticker_enabled,
            sticker_autosave=sticker_autosave,
            sticker_max_count=sticker_max_count,
            sticker_catalog_size=sticker_catalog_size,
            quote_reply=quote_reply,
            history_messages=history_messages,
            memory_facts=memory_facts,
            long_term_memories=long_term_memories,
            poke_reply=poke_reply,
            poke_back=poke_back,
            vision_provider=vision_provider,
            vision_detail=vision_detail,
            vision_max_images=vision_max_images,
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
