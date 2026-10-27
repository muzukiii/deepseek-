"""上下文相关性选取的回归测试：引用锚点（ID/文本兜底）、回场说话人、护栏、统计。

背景：A4 之后只补"窗口外确实缺的前情"，所以必须钉住触发条件、去重、上限、
字符预算护栏，以及给观测用的统计字段——否则线上既不知道有没有触发，也不知道
补进来多少。纯打桩 + 临时 SQLite，不联网。
无 pytest 时 `python tests/test_context_focus.py` 直跑。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrabot.chat_service import history_manager as hm

GROUP = 777
G_TRIM = 778
G_ANCHOR_TRIM = 779
KEEP = 5
A, B, C, D = "2001", "2002", "2003", "2004"
SUMMARY_FLAG = "【摘要】更早的对话"


def fresh_db() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="astrabot_ctx_focus_"))
    hm.DATA_DIR = tmp
    hm.DB_PATH = tmp / "history.db"
    hm._local = threading.local()
    hm._db_initialized = False


async def add(group: int, time: int, message_id: int, uid: str, text: str, msg_type: str = "user") -> None:
    await hm.append_message(group, {
        "time": time, "message_id": message_id, "user_id": uid,
        "user_name": uid, "message": text, "msg_type": msg_type,
    })


async def seed() -> dict[str, int]:
    """A 只在很早出现两次，之后窗口里全是 B/C/D。"""
    await add(GROUP, 900, 1, "bot", SUMMARY_FLAG, "mid_term_memory")
    await add(GROUP, 1000, 10, A, "A的旧问题1")
    await add(GROUP, 1010, 11, B, "B1")
    await add(GROUP, 1020, 12, A, "A的旧问题2")
    await add(GROUP, 1030, 13, C, "C1被引用的那句")
    await add(GROUP, 1040, 14, "bot", "机器人插话", "bot")
    plan = [B, C, D, B, C, D, B, D]
    for i, uid in enumerate(plan):
        await add(GROUP, 1050 + i * 10, 100 + i, uid, f"窗口内{i}")
    return {"anchor_c": 13, "anchor_in_window": 100 + len(plan) - 1, "last_user": plan[-1]}


async def seed_trim() -> None:
    """G_TRIM：基础上下文极短，A 的旧消息很长 → 说话人补量必须被护栏丢弃。"""
    for i in range(KEEP):
        await add(G_TRIM, 1050 + i * 10, 200 + i, B, f"短{i}")
    for i in range(3):
        await add(G_TRIM, 500 + i, 300 + i, A, "长" * 300)


async def seed_anchor_trim() -> None:
    """G_ANCHOR_TRIM：被引用那条本身超预算 → 只剩锚点，前后文全丢。"""
    for i in range(KEEP):
        await add(G_ANCHOR_TRIM, 1050 + i * 10, 400 + i, B, f"短{i}")
    await add(G_ANCHOR_TRIM, 600, 410, C, "锚" * 900)


def ids(rows: list[dict]) -> list[int]:
    return [r["message_id"] for r in rows]


async def run() -> None:
    fresh_db()
    info = await seed()

    # --- 基础窗口 ---
    base, st = await hm.select_context_with_stats(GROUP, max_messages=KEEP)
    base_ids = ids(base)
    assert base_ids[0] == 1, f"pinned 摘要必须一直在最前，实得 {base_ids[:3]}"
    assert 10 not in base_ids and 12 not in base_ids and 13 not in base_ids, base_ids
    assert base == sorted(base, key=lambda r: (r["time"], r["id"])), "必须按时间升序"
    assert st["extra_rows"] == 0 and st["quote_state"] == "none", st
    assert st["base_rows"] == len(base), st
    print("PASS 基础窗口不含窗口外的 A/C1，摘要 pinned 在最前，统计为 0 补充")

    # --- 回场说话人 ---
    ret = await hm.select_context(GROUP, max_messages=KEEP, focus_user_id=A)
    ret_ids = ids(ret)
    assert 10 in ret_ids and 12 in ret_ids, f"回场说话人 A 的旧消息应补进来: {ret_ids}"
    same = await hm.select_context(GROUP, max_messages=KEEP, focus_user_id=info["last_user"])
    assert ids(same) == base_ids, f"窗口内有发言的人不该补旧账: {ids(same)}"
    _, st_spk = await hm.select_context_with_stats(GROUP, max_messages=KEEP, focus_user_id=A)
    assert st_spk["extra_speaker"] == 2 and st_spk["extra_quote"] == 0, st_spk
    assert not st_spk["trimmed"], st_spk
    print("PASS 回场说话人补入、活跃说话人不补，统计区分 quote/speaker")

    # --- 引用锚点（ID 命中） ---
    quoted, st_q = await hm.select_context_with_stats(
        GROUP, max_messages=KEEP, focus_message_id=info["anchor_c"]
    )
    quoted_ids = ids(quoted)
    assert info["anchor_c"] in quoted_ids and 11 in quoted_ids and 14 in quoted_ids, quoted_ids
    assert st_q["quote_state"] == "hit_id" and st_q["extra_quote"] >= 1, st_q
    assert quoted == sorted(quoted, key=lambda r: (r["time"], r["id"])), "锚点补充后要按时间升序"
    print("PASS 引用锚点按 ID 命中，带出被引用那一句及前后文")

    dup = await hm.select_context(GROUP, max_messages=KEEP, focus_message_id=info["anchor_in_window"])
    dup_ids = ids(dup)
    assert len(dup_ids) == len(set(dup_ids)), f"锚点在窗口内时不能重复: {dup_ids}"
    print("PASS 锚点落在窗口内时不产生重复行")

    excl = await hm.select_context(
        GROUP, max_messages=KEEP, exclude_message_id=info["anchor_in_window"],
        focus_message_id=info["anchor_in_window"],
    )
    assert info["anchor_in_window"] not in ids(excl), f"exclude_message_id 必须仍然生效: {ids(excl)}"
    print("PASS exclude_message_id 依然生效")

    # --- 文本兜底 ---
    text_hit, st_t = await hm.select_context_with_stats(
        GROUP, max_messages=KEEP, focus_message_id=999999,
        focus_text="C1被引用", focus_author_id=C,
    )
    assert st_t["quote_state"] == "hit_text", st_t
    assert info["anchor_c"] in ids(text_hit), f"文本兜底应定位到 C1: {ids(text_hit)}"
    miss, st_m = await hm.select_context_with_stats(
        GROUP, max_messages=KEEP, focus_message_id=999999,
        focus_text="根本不存在的句子", focus_author_id=C,
    )
    assert st_m["quote_state"] == "miss" and st_m["extra_rows"] == 0, st_m
    assert ids(miss) == base_ids, ids(miss)
    print("PASS 引用文本兜底命中；兜底失败时记 miss 且不改变上下文")

    # --- 护栏：说话人补量被丢弃 ---
    await seed_trim()
    _, st_g = await hm.select_context_with_stats(G_TRIM, max_messages=KEEP, focus_user_id=A)
    assert st_g["trimmed"] and st_g["extra_speaker"] == 0 and st_g["extra_rows"] == 0, st_g
    print("PASS 超预算时丢弃说话人补量（trimmed=True）")

    # --- 护栏：锚点超预算时只留锚点 ---
    await seed_anchor_trim()
    anchor_only, st_a = await hm.select_context_with_stats(
        G_ANCHOR_TRIM, max_messages=KEEP, focus_message_id=410
    )
    assert st_a["trimmed"] and st_a["extra_rows"] == 1 and 410 in ids(anchor_only), st_a
    print("PASS 锚点本身超预算时只保留锚点一句")

    # --- 行数硬上限 ---
    fresh_db()
    info2 = await seed()
    for i in range(20):
        await add(GROUP, 500 + i, 500 + i, A, f"A的历史{i}")
    capped, st_c = await hm.select_context_with_stats(GROUP, max_messages=KEEP, focus_user_id=A)
    assert st_c["extra_rows"] <= hm.FOCUS_MAX_ROWS, st_c
    assert len(ids(capped)) == len(set(ids(capped))), "不允许重复行"
    print(f"PASS 补充量受 FOCUS_MAX_ROWS 限制（本次补了 {st_c['extra_rows']} 条）")


def main() -> int:
    failures = 0
    try:
        asyncio.run(run())
    except AssertionError as e:
        failures += 1
        print(f"FAIL {e}")
    except Exception as e:
        failures += 1
        import traceback

        print(f"FAIL unexpected: {type(e).__name__}: {e}")
        traceback.print_exc()
    print("1/1 passed" if not failures else "0/1 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
