"""D-3 回归：长期记忆单一真源（SQLite）+ 删除按本轮展示 id。
独立运行：python tests/test_d3_single_source.py（无需 pytest）
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from astrabot.chat_service import enhanced_memory as enh
from astrabot.chat_service import memory_manager as mm

failures = []


def check(cond, msg):
    print(("  ok   - " if cond else "  FAIL - ") + msg)
    if not cond:
        failures.append(msg)


def test_long_term_ids_aligned():
    print("[build_memory_context.long_term_ids 对齐展示编号]")
    o1, o2 = enh.get_long_term_memories, enh.get_recent_facts
    try:
        enh.get_long_term_memories = lambda *a, **k: [
            {"id": 101, "created_at": 1700000000, "content": "甲"},
            {"id": 102, "created_at": 1700000001, "content": "乙"},
            {"id": 103, "created_at": 1700000002, "content": "丙"},
        ]
        enh.get_recent_facts = lambda *a, **k: []
        ctx = enh.build_memory_context(group_id=1, current_message="hi")
        check(ctx["long_term_ids"] == [101, 102, 103], "ids 顺序正确")
        lines = ctx["long_term"].splitlines()
        check(lines[0].startswith("1.") and "甲" in lines[0], "编号1 → 甲")
        check(lines[1].startswith("2.") and "乙" in lines[1], "编号2 → 乙")
    finally:
        enh.get_long_term_memories, enh.get_recent_facts = o1, o2


def test_process_memorize():
    print("[process_memorize 只写SQLite + 删除按展示id]")
    adds, dels = [], []
    r_add, r_del, m_add = enh.add_long_term_memory, enh.delete_long_term_memory, mm.add_memory
    try:
        enh.add_long_term_memory = lambda gid, c, created_by=None: (adds.append((gid, c)), True)[1]
        enh.delete_long_term_memory = lambda gid, mid: (dels.append((gid, mid)), True)[1]
        mm.add_memory = lambda *a, **k: (_ for _ in ()).throw(AssertionError("add_memory 不应被调用"))

        mm.process_memorize(7, ["新记忆", 2], shown_ltm_ids=[101, 102, 103])
        check(adds == [(7, "新记忆")], "string → 仅写 SQLite 长期记忆")
        check(dels == [(7, 102)], "int 2 → 删除本轮展示第2条 id=102")

        adds.clear(); dels.clear()
        mm.process_memorize(7, [5], shown_ltm_ids=[101, 102])
        check(dels == [], "越界序号不删除")

        adds.clear(); dels.clear()
        mm.process_memorize(7, ["x"])
        check(adds == [(7, "x")], "缺 shown_ltm_ids 时新增仍生效")
    finally:
        enh.add_long_term_memory, enh.delete_long_term_memory, mm.add_memory = r_add, r_del, m_add


def test_migrate_idempotent():
    print("[migrate_from_old_memory_system 幂等去重]")
    import os

    # 迁移现在默认关（JSON 只是回滚快照），本用例测的正是开启后的接管与幂等
    os.environ["ASTRA_ENABLE_LEGACY_MIGRATION"] = "true"
    tmp = Path(tempfile.mkdtemp(prefix="d3mem_"))
    enh.DATA_DIR = tmp
    enh.MEMORY_DB = tmp / "memories.db"
    if hasattr(enh._local, "conn"):
        try:
            enh._local.conn.close()
        except Exception:
            pass
        del enh._local.conn

    fake = {"group_42": {"memories": ["A", "B", "B", "C"], "users": {"111": "喜欢猫"}}}
    r_load = mm._load_json
    try:
        mm._load_json = lambda path: dict(fake)
        enh.migrate_from_old_memory_system()
        c = sqlite3.connect(str(enh.MEMORY_DB))
        n1 = c.execute("SELECT COUNT(*) FROM long_term_memories WHERE group_id=42").fetchone()[0]
        c.close()
        enh.migrate_from_old_memory_system()
        c = sqlite3.connect(str(enh.MEMORY_DB))
        n2 = c.execute("SELECT COUNT(*) FROM long_term_memories WHERE group_id=42").fetchone()[0]
        c.close()
        check(n1 == 3, f"迁移去重后 3 条（A/B/C），实得 {n1}")
        check(n2 == 3, f"重复迁移幂等，仍 3 条，实得 {n2}")
    finally:
        mm._load_json = r_load
        os.environ.pop("ASTRA_ENABLE_LEGACY_MIGRATION", None)


if __name__ == "__main__":
    test_long_term_ids_aligned()
    test_process_memorize()
    test_migrate_idempotent()
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        sys.exit(1)
    print("ALL D3 REGRESSION TESTS PASSED")
