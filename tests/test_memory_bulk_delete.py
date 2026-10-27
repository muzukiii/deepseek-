"""批量删除长期记忆的回归测试：条件筛选、两步确认、删前备份、破坏性判定。

背景：388 条叙事记忆只能逐条删不现实，所以加了"按日期/关键词/全清"三种批量删除，
但都是不可逆操作——必须预览条数、同一条件加 confirm 才执行，且删前先落备份文件。
纯打桩 + 临时 SQLite，不联网、不碰真实 data/。
无 pytest 时 `python tests/test_memory_bulk_delete.py` 直跑。
"""

from __future__ import annotations

import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrabot.chat_service import enhanced_memory as em
from astrabot.chat_service import memory_commands as mc

GROUP = 999


def fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="astrabot_bulk_del_"))
    em.DATA_DIR = tmp
    em.MEMORY_DB = tmp / "memories.db"
    em._local = threading.local()
    mc._EXPORT_DIR = tmp / "exports"
    return tmp


def ts(day: str, hour: int = 12) -> int:
    return int(datetime.strptime(f"{day} {hour:02d}:00", "%Y-%m-%d %H:%M").timestamp())


def seed() -> None:
    conn = em._get_db()
    rows = [
        (GROUP, "旧记忆：点叔喜欢被叫点哥", ts("2026-10-10")),
        (GROUP, "旧记忆：表情包是恶俗包", ts("2026-10-12")),
        (GROUP, "新记忆：小池是 AI 助手", ts("2026-10-26")),
        (GROUP, "新记忆：兰山要各叫各的", ts("2026-10-27")),
    ]
    for gid, content, t in rows:
        conn.execute(
            "INSERT INTO long_term_memories (group_id, content, created_at) VALUES (?, ?, ?)",
            (gid, content, t),
        )
    conn.commit()


def count() -> int:
    cur = em._get_db().execute(
        "SELECT COUNT(*) FROM long_term_memories WHERE group_id = ?", (GROUP,)
    )
    return int(cur.fetchone()[0])


def run() -> None:
    fresh_db()
    seed()

    # 条件筛选：按时间
    old = em.find_long_term_memories(GROUP, before_ts=ts("2026-10-20", 0))
    assert len(old) == 2 and all("旧记忆" in r["content"] for r in old), old
    # 关键词用 instr：% 不是通配符
    assert em.find_long_term_memories(GROUP, keyword="%") == [], "instr 不应把 % 当通配符"
    hit = em.find_long_term_memories(GROUP, keyword="表情包")
    assert len(hit) == 1 and "恶俗包" in hit[0]["content"], hit
    print("PASS 条件筛选：按时间截断、关键词用 instr 不受通配符影响")

    # 预览不删
    handled, text, spec = mc.handle_memory_command_with_export(
        GROUP, "1", "/memory delete before 2026-10-20"
    )
    assert handled and spec is None, (handled, spec)
    assert "将删除 2 条" in text and "confirm" in text, text
    assert count() == 4, "预览阶段不允许删任何东西"
    print("PASS 预览只报数、不删除")

    # 确认才删，并且先落备份
    handled, text, spec = mc.handle_memory_command_with_export(
        GROUP, "1", "/memory delete before 2026-10-20 confirm"
    )
    assert handled and spec and spec["path"].exists(), (handled, spec)
    assert count() == 2, f"应删掉 2 条旧记忆，实剩 {count()}"
    assert "已删除 2 条" in text and spec["name"] in text, text
    backup = spec["path"].read_text(encoding="utf-8")
    assert "旧记忆：点叔喜欢被叫点哥" in backup and "旧记忆：表情包是恶俗包" in backup, backup
    assert "小池是 AI 助手" not in backup, "备份里不该出现未被删除的条目"
    print("PASS 确认后删除并生成只含被删条目的备份")

    # 关键词批量：预览 + 确认
    handled, text, spec = mc.handle_memory_command_with_export(
        GROUP, "1", "/memory delete keyword 不存在的词"
    )
    assert handled and spec is None and "没有符合条件的长期记忆" in text, text
    handled, text, spec = mc.handle_memory_command_with_export(
        GROUP, "1", "/memory delete keyword 兰山 confirm"
    )
    assert handled and "已删除 1 条" in text and count() == 1, (text, count())
    print("PASS 关键词批量：无匹配时不动，匹配后按确认删除")

    # 清空全部长期记忆
    handled, text, spec = mc.handle_memory_command_with_export(GROUP, "1", "/memory clear long")
    assert handled and spec is None and "将删除 1 条" in text, text
    assert count() == 1
    handled, text, spec = mc.handle_memory_command_with_export(GROUP, "1", "/memory clear long confirm")
    assert handled and spec and spec["path"].exists(), (handled, spec)
    assert count() == 0, f"应清空，实剩 {count()}"
    print("PASS 清空全部长期记忆：两步确认 + 备份")

    # 破坏性判定要覆盖三种批量形式，否则会绕过白名单
    for cmd in (
        "/memory delete before 2026-10-20",
        "/memory delete before 10-20 confirm",
        "/memory delete keyword 表情包",
        "/memory delete keyword 表情包 confirm",
        "/memory clear long",
        "/memory clear long confirm",
    ):
        assert mc.is_destructive_command(cmd), cmd
    assert not mc.is_destructive_command("/memory list")
    print("PASS 三种批量形式都被判定为破坏性命令")


def main() -> int:
    failures = 0
    try:
        run()
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
