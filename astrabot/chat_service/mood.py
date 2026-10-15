"""情绪系统：让机器人生气时能短暂冷落对方。

与常驻的 ignore_users（配置写死的黑名单）不同，这里是**临时**的：
被惹火的用户会在若干分钟内被无视，到期自动恢复，不会永久影响关系。

状态以 JSON 落盘，重启后依然有效（否则一生气就被重启洗掉了）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from astrabot.logmanager.logger import logger

DATA_DIR = Path(__file__).resolve().parent / "data"
MOOD_PATH = DATA_DIR / "mood.json"

# 默认冷落时长（秒）
DEFAULT_ANGRY_SECONDS = 600

_lock = threading.RLock()


def _load() -> dict:
    if not MOOD_PATH.is_file():
        return {}
    try:
        with MOOD_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        logger.warning(f"mood.json corrupted, resetting: {e}")
        return {}
    except Exception as e:
        logger.debug(f"Failed to load mood: {e}")
        return {}


def _save(data: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MOOD_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(MOOD_PATH)


def _key(group_id: int, user_id: str) -> str:
    return f"{group_id}:{user_id}"


def set_angry(
    group_id: int,
    user_id: str,
    seconds: int = DEFAULT_ANGRY_SECONDS,
    reason: str = "",
) -> int:
    """把某人加入临时冷落名单，返回解禁时间戳。"""
    seconds = max(30, min(int(seconds), 24 * 3600))
    until = int(time.time()) + seconds

    with _lock:
        data = _load()
        data[_key(group_id, user_id)] = {
            "until": until,
            "reason": reason[:100],
            "at": int(time.time()),
        }
        _save(data)

    logger.info(
        f"Mood: {user_id} in group {group_id} is ignored for {seconds}s"
        + (f" ({reason})" if reason else "")
    )
    return until


def is_angry(group_id: int, user_id: str) -> bool:
    """当前是否正在冷落此人。到期会自动清理。"""
    data = _load()
    entry = data.get(_key(group_id, user_id))
    if not entry:
        return False

    until = entry.get("until", 0)
    if until <= time.time():
        # 过期即清理，避免文件无限增长
        with _lock:
            data = _load()
            data.pop(_key(group_id, user_id), None)
            _save(data)
        return False
    return True


def remaining_seconds(group_id: int, user_id: str) -> int:
    data = _load()
    entry = data.get(_key(group_id, user_id))
    if not entry:
        return 0
    left = int(entry.get("until", 0) - time.time())
    return max(0, left)


def clear(group_id: int, user_id: str | None = None) -> int:
    """解除冷落。user_id 为 None 时清空该群全部。"""
    with _lock:
        data = _load()
        prefix = f"{group_id}:"
        before = len(data)
        if user_id is None:
            data = {k: v for k, v in data.items() if not k.startswith(prefix)}
        else:
            data.pop(_key(group_id, user_id), None)
        _save(data)
        return before - len(data)


def purge_expired() -> int:
    """清理所有已过期条目，返回清理数量。"""
    now = time.time()
    with _lock:
        data = _load()
        before = len(data)
        data = {k: v for k, v in data.items() if v.get("until", 0) > now}
        if len(data) != before:
            _save(data)
        return before - len(data)


def list_active(group_id: int) -> list[dict]:
    """列出该群当前仍在冷落中的用户。"""
    now = time.time()
    out = []
    for k, v in _load().items():
        if not k.startswith(f"{group_id}:"):
            continue
        if v.get("until", 0) <= now:
            continue
        out.append(
            {
                "user_id": k.split(":", 1)[1],
                "left": int(v["until"] - now),
                "reason": v.get("reason", ""),
            }
        )
    return out
