"""回复控制器：管理各群的回复状态、概率判断、排队消息和撤回标记"""

from __future__ import annotations

import asyncio
import random
import time

from astrabot.config import Config


class ReplyController:
    _locks: dict[int, asyncio.Lock] = {}
    _reply_status: dict[int, bool] = {}
    _pending_at: dict[int, list[dict]] = {}  # 回复期间被 @ 的排队消息
    _recalled_messages: set[tuple[int, int]] = set()  # 已撤回的 (group_id, message_id)
    # 连续互回计数: group_id -> {"user_id": str, "streak": int, "blocked_at": float}
    # 用于防止两个机器人（或两个话痨）无限互相回复。
    # 达到上限后进入冷却，冷却期满自动恢复；期间若有别人插话也立即恢复。
    _streak: dict[int, dict] = {}

    @classmethod
    def get_lock(cls, group_id: int) -> asyncio.Lock:
        if group_id not in cls._locks:
            cls._locks[group_id] = asyncio.Lock()
        return cls._locks[group_id]

    @classmethod
    def is_replying(cls, group_id: int) -> bool:
        return cls._reply_status.get(group_id, False)

    @classmethod
    def set_replying(cls, group_id: int, status: bool):
        cls._reply_status[group_id] = status

    @classmethod
    def bump_streak(cls, group_id: int, user_id: str) -> int:
        """记录一次回复，返回该用户的连续被回次数。

        同一个用户连续发言则累加；换了人（例如真人插话）立即归零重计，
        这样只要有人插话就能打破僵局。
        """
        state = cls._streak.get(group_id)
        if state and state.get("user_id") == str(user_id):
            state["streak"] = state.get("streak", 0) + 1
        else:
            state = {"user_id": str(user_id), "streak": 1}
            cls._streak[group_id] = state
        return state["streak"]

    @classmethod
    def peek_streak(cls, group_id: int, user_id: str) -> int:
        """查看当前连续次数（不修改）。

        若已达到上限且冷却时间已过，则视为 0 并顺手清除状态，
        让同一个人沉默一阵后仍能重新开始对话，而不是永久被卡住。
        """
        state = cls._streak.get(group_id)
        if not state or state.get("user_id") != str(user_id):
            return 0
        return state.get("streak", 0)

    @classmethod
    def set_blocked(cls, group_id: int):
        """标记该群刚因连续互回而上限，开始计时冷却。"""
        state = cls._streak.get(group_id)
        if state:
            state["blocked_at"] = time.time()

    @classmethod
    def is_cooling_down(cls, group_id: int, user_id: str, cooldown: float) -> bool:
        """是否仍在上限冷却中。

        cooldown <= 0 表示不启用冷却，返回 False。
        """
        if cooldown <= 0:
            return False
        state = cls._streak.get(group_id)
        if not state or state.get("user_id") != str(user_id):
            return False
        blocked_at = state.get("blocked_at")
        if not blocked_at:
            return False
        if time.time() - blocked_at < cooldown:
            return True
        # 冷却结束：清除计数，允许重新开始
        cls._streak.pop(group_id, None)
        return False

    @classmethod
    def cooldown_left(cls, group_id: int, cooldown: float) -> int:
        """剩余冷却秒数，0 表示不在冷却中。"""
        if cooldown <= 0:
            return 0
        state = cls._streak.get(group_id)
        if not state:
            return 0
        blocked_at = state.get("blocked_at")
        if not blocked_at:
            return 0
        left = int(cooldown - (time.time() - blocked_at))
        return max(0, left)

    @classmethod
    def reset_streak(cls, group_id: int):
        cls._streak.pop(group_id, None)

    @classmethod
    def add_pending_at(cls, group_id: int, info: dict):
        if group_id not in cls._pending_at:
            cls._pending_at[group_id] = []
        cls._pending_at[group_id].append(info)

    @classmethod
    def add_recalled(cls, group_id: int, message_id: int):
        cls._recalled_messages.add((group_id, message_id))

    @classmethod
    def is_recalled(cls, group_id: int, message_id: int) -> bool:
        return (group_id, message_id) in cls._recalled_messages

    @classmethod
    def pop_pending_at(cls, group_id: int) -> list[dict]:
        return cls._pending_at.pop(group_id, [])


def should_reply(
    is_at: bool,
    is_replying: bool,
    config: Config,
    is_keyword: bool = False,
) -> bool:
    """根据触发方式和回复状态，按不同概率决定是否回复"""
    triggered = is_at or is_keyword
    # 四种概率：被@时回复中 / 被@时空闲 / 普通回复中 / 普通空闲
    if triggered and is_replying:
        rate = config.reply_rate_at_in_reply
    elif triggered:
        rate = config.reply_rate_at
    elif is_replying:
        rate = config.reply_rate_in_reply
    else:
        rate = config.reply_rate
    return random.random() < rate
