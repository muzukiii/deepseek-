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

# 每个模型的单价（美元 / 1M tokens）。按模型名精确匹配取价；
# 也可用环境变量 MODEL_PRICING_JSON 覆盖或扩展（见 _load_pricing）。
# 未配置价格的模型不会被错误地套用别的模型的价，而是标记为「未配置」。
# 智谱价取自 Z.ai 国际站公开牌价（USD/1M，含缓存输入），无峰谷折扣。
_DEFAULT_PRICING: dict[str, dict] = {
    "deepseek-chat": {"input": 0.3, "output": 1.2, "cache_hit": 0.006, "peak": True},
    "glm-5.3-flash": {"input": 0.15, "output": 0.50, "cache_hit": 0.03},
    "glm-5.3-flashx": {"input": 0.37, "output": 1.25, "cache_hit": 0.075},
}
# 模型名前缀 → 默认价，用于未精确命中时的兜底（目前仅 DeepSeek）。
_PREFIX_PRICING: dict[str, str] = {"deepseek": "deepseek-chat"}
# 峰谷时段（UTC）：01:00-04:00 与 06:00-10:00 为峰时，其余折半（仅对 peak=True 生效）
_PEAK_HOURS = set(range(1, 4)) | set(range(6, 10))

_pricing_cache: dict[str, dict] | None = None

_lock = threading.Lock()


def _load_pricing() -> dict[str, dict]:
    """合并内置默认价与 MODEL_PRICING_JSON（进程启动后 env 固定，结果缓存一次）。

    MODEL_PRICING_JSON 形如：
      {"glm-5.3-flash": {"input": 0.0, "output": 0.0, "cache_hit": 0.0}, ...}
    单位美元/1M tokens；`peak`(bool) 可选，默认 False（智谱等按量计价的模型不分峰谷）。
    解析失败时安全回退到内置默认，绝不影响主流程。
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    table = {k: dict(v) for k, v in _DEFAULT_PRICING.items()}
    raw = os.environ.get("MODEL_PRICING_JSON", "").strip()
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                for model, price in override.items():
                    if isinstance(price, dict):
                        table[str(model)] = {**table.get(str(model), {}), **price}
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"MODEL_PRICING_JSON 解析失败，仅用内置默认价: {e}")
    _pricing_cache = table
    return table


def _price_for(model: str) -> dict | None:
    """按模型名取价：精确命中优先，否则按前缀兜底，都未命中返回 None（未配置）。"""
    table = _load_pricing()
    price = table.get(model)
    if price is None:
        for prefix, fallback in _PREFIX_PRICING.items():
            if model.lower().startswith(prefix):
                return table.get(fallback)
        return None
    return price


def _estimate_cost(usage: dict, model: str) -> float:
    """按该模型的单价估算本次花费（美元）。未配置单价时返回 0.0。"""
    price = _price_for(model)
    if price is None:
        return 0.0

    hit = usage.get("cache_hit_tokens") or 0
    miss = usage.get("cache_miss_tokens")
    prompt = usage.get("prompt_tokens") or 0
    if miss is None:
        # 未单独给出时，把总量当作 cache miss 计（保守上限）
        miss = max(0, prompt - hit)
    completion = usage.get("completion_tokens") or 0

    factor = 1.0
    if price.get("peak"):
        # 仅 DeepSeek 这类峰谷计价模型：峰时原价、平时折半
        factor = 1.0 if datetime.now().hour in _PEAK_HOURS else 0.5

    return factor * (
        hit / 1_000_000 * price.get("cache_hit", 0.0)
        + miss / 1_000_000 * price.get("input", 0.0)
        + completion / 1_000_000 * price.get("output", 0.0)
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
    priced = _price_for(model) is not None
    entry["priced"] = priced
    entry["cost_usd"] = round(_estimate_cost(entry, model), 8) if priced else 0.0

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _lock:
            _rotate_if_needed()
            with USAGE_DB.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # 统计失败不应影响主流程
        logger.debug(f"Failed to record usage: {e}")
        return

    if priced:
        logger.info(
            f"API 用量[{kind}] {model}: "
            f"in={prompt}(cache {hit}) out={completion} "
            f"≈${entry['cost_usd']:.6f}"
        )
    else:
        logger.info(
            f"API 用量[{kind}] {model}: "
            f"in={prompt}(cache {hit}) out={completion} "
            f"(价格未配置→花费计 0；在 .env 的 MODEL_PRICING_JSON 补该模型单价)"
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
        agg = by_model.setdefault(
            m, {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "priced": r.get("priced", True)}
        )
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

    # 未配置单价的模型：其花费恒为 0，汇总总额会偏低，需显式提示。
    unpriced = sorted(m for m, a in by_model.items() if not a.get("priced", True))
    if unpriced:
        lines.append("")
        lines.append(
            f"  ⚠ 以下模型未配置单价，花费按 0 计（总花费被低估）："
            f"{', '.join(unpriced)}"
        )
        lines.append("    在 .env 的 MODEL_PRICING_JSON 补上其单价后重新统计。")

    return "\n".join(lines)
