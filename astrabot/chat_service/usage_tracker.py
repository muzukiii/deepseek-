"""API 用量统计：记录每次调用的 token 消耗，支持按天/按模型汇总。

原先代码把 response.usage 直接丢弃，只能靠日志字符数反推花费。
这里集中记录，便于随时查看实际消耗。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from astrabot.logmanager.logger import logger

DATA_DIR = Path(__file__).resolve().parent / "data"
USAGE_DB = DATA_DIR / "usage.jsonl"

# 活动文件超过该大小即轮转为时间戳归档，避免 usage.jsonl 无界增长、
# 并让 summary() 的读取成本保持在有界窗口内。
MAX_USAGE_FILE_BYTES = 1_000_000

# DeepSeek 峰时价格（美元 / 1M tokens），用于估算花费
_PRICE = {
    "input_cache_hit": 0.006,
    "input_cache_miss": 0.3,
    "output": 1.2,
}
# 峰谷时段（UTC）：01:00-04:00 与 06:00-10:00 为峰时，其余折半
_PEAK_HOURS = set(range(1, 4)) | set(range(6, 10))

_lock = threading.Lock()


def _estimate_cost(usage: dict) -> float:
    """按 DeepSeek 计费规则估算本次花费（美元）。"""
    hit = usage.get("cache_hit_tokens") or 0
    miss = usage.get("cache_miss_tokens")
    prompt = usage.get("prompt_tokens") or 0
    if miss is None:
        # 未单独给出时，把总量当作 cache miss 计（保守上限）
        miss = max(0, prompt - hit)
    completion = usage.get("completion_tokens") or 0

    hour = datetime.now().hour
    factor = 1.0 if hour in _PEAK_HOURS else 0.5

    return factor * (
        hit / 1_000_000 * _PRICE["input_cache_hit"]
        + miss / 1_000_000 * _PRICE["input_cache_miss"]
        + completion / 1_000_000 * _PRICE["output"]
    )


def _rotate_if_needed() -> None:
    """活动文件超限时轮转：usage.jsonl -> usage-<ts>.jsonl。

    调用方需持有 _lock。os.replace 原子，跨进程也不会写出半个文件。
    """
    try:
        if USAGE_DB.is_file() and USAGE_DB.stat().st_size > MAX_USAGE_FILE_BYTES:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive = DATA_DIR / f"usage-{stamp}.jsonl"
            n = 1
            while archive.exists():
                archive = DATA_DIR / f"usage-{stamp}-{n}.jsonl"
                n += 1
            os.replace(USAGE_DB, archive)
            logger.debug(f"Rotated usage log to {archive.name}")
    except Exception as e:
        logger.debug(f"Usage log rotation skipped: {e}")


def record(
    *,
    kind: str,
    model: str,
    usage: dict | None,
) -> None:
    """记录一次 API 调用。usage 为 None 时表示该响应未返回用量。"""
    if not usage:
        return

    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    details = usage.get("prompt_tokens_details") or {}
    hit = details.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0

    entry = {
        "ts": int(time.time()),
        "kind": kind,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": usage.get("total_tokens") or (prompt + completion),
        "cache_hit_tokens": hit,
        "cache_miss_tokens": max(0, prompt - hit),
    }
    entry["cost_usd"] = round(_estimate_cost(entry), 8)

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            _rotate_if_needed()
            with USAGE_DB.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # 统计失败不应影响主流程
        logger.debug(f"Failed to record usage: {e}")
        return

    logger.info(
        f"API 用量[{kind}] {model}: "
        f"in={prompt}(cache {hit}) out={completion} "
        f"≈${entry['cost_usd']:.6f}"
    )


def _load(days: int | None = None) -> list[dict]:
    if not USAGE_DB.is_file():
        return []
    since = None
    if days:
        since = time.time() - days * 86400

    rows: list[dict] = []
    with USAGE_DB.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and row.get("ts", 0) < since:
                continue
            rows.append(row)
    return rows


def summary(days: int | None = None) -> str:
    """生成用量汇总文本。days=None 表示全部。"""
    rows = _load(days)
    if not rows:
        return "暂无 API 用量记录"

    scope = f"最近 {days} 天" if days else "全部时间"
    total_calls = len(rows)
    t_in = sum(r.get("prompt_tokens", 0) for r in rows)
    t_out = sum(r.get("completion_tokens", 0) for r in rows)
    t_hit = sum(r.get("cache_hit_tokens", 0) for r in rows)
    cost = sum(r.get("cost_usd", 0.0) for r in rows)

    lines = [
        f"=== API 用量（{scope}）===",
        f"  调用次数   : {total_calls}",
        f"  输入 tokens: {t_in:,}",
        f"  输出 tokens: {t_out:,}",
        f"  缓存命中   : {t_hit:,} ({t_hit * 100 // max(1, t_in)}%)",
        f"  估算花费   : ${cost:.4f} (约 {cost * 7.2:.2f} 元)",
    ]

    # 按模型拆分
    by_model: dict[str, dict] = {}
    for r in rows:
        m = r.get("model", "?")
        agg = by_model.setdefault(m, {"calls": 0, "in": 0, "out": 0, "cost": 0.0})
        agg["calls"] += 1
        agg["in"] += r.get("prompt_tokens", 0)
        agg["out"] += r.get("completion_tokens", 0)
        agg["cost"] += r.get("cost_usd", 0.0)

    if len(by_model) > 1:
        lines.append("")
        lines.append("  按模型:")
        for m, a in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
            lines.append(
                f"    {m}: {a['calls']} 次  "
                f"in={a['in']:,} out={a['out']:,}  ${a['cost']:.4f}"
            )

    return "\n".join(lines)
