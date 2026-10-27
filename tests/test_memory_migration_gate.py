"""回归：旧 memories.json 的启动迁移必须是一次性的。

真实事故（2026-10-27）：按日期删掉 490 条长期记忆后重启，
bot.py 的 _reconcile_legacy_memory 又把 memories.json 里的 349 条灌了回来，
删除全部失效，而且 created_at 变成迁移当天，按日期再也筛不出来。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astrabot.chat_service.enhanced_memory as em  # noqa: E402
from astrabot.chat_service import memory_manager as mm  # noqa: E402


class MigrationGateTest(unittest.TestCase):
    def setUp(self):
        self._saved = (em.DATA_DIR, em.MEMORY_DB, em._local, mm.MEMORIES_PATH)
        self.tmp = Path(tempfile.mkdtemp(prefix="miggate-"))
        em.DATA_DIR = self.tmp
        em.MEMORY_DB = self.tmp / "memories.db"
        em._local = threading.local()
        mm.MEMORIES_PATH = self.tmp / "memories.json"

    def tearDown(self):
        em.DATA_DIR, em.MEMORY_DB, em._local, mm.MEMORIES_PATH = self._saved

    def _write_json(self, entries):
        payload = {"group_1": {"memories": entries, "users": {}}}
        mm.MEMORIES_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_first_run_migrates_and_marks(self):
        self._write_json(["小池喜欢用颜文字", "千亿喜欢喝奶茶"])
        self.assertEqual(em.migrate_from_old_memory_system(), 2)
        self.assertTrue(em._migration_marker().exists(), "首次迁移后必须落标记")
        self.assertEqual(len(em.find_long_term_memories(1)), 2)

    def test_deleted_memory_is_not_resurrected(self):
        self._write_json(["小池喜欢用颜文字", "千亿喜欢喝奶茶"])
        em.migrate_from_old_memory_system()

        rows = em.find_long_term_memories(1)
        em.delete_long_term_memories_by_ids(1, [r["id"] for r in rows])
        self.assertEqual(em.find_long_term_memories(1), [])

        self.assertEqual(em.migrate_from_old_memory_system(), 0, "第二次启动不该再迁移")
        self.assertEqual(em.find_long_term_memories(1), [], "删掉的记忆被 JSON 灌回来了")

    def test_marker_blocks_later_json_entries(self):
        self._write_json(["小池喜欢用颜文字"])
        em.migrate_from_old_memory_system()
        self._write_json(["小池喜欢用颜文字", "后来才写进 JSON 的条目"])
        self.assertEqual(em.migrate_from_old_memory_system(), 0)
        self.assertEqual(len(em.find_long_term_memories(1)), 1)

    def test_missing_json_still_marks_done(self):
        self.assertEqual(em.migrate_from_old_memory_system(), 0)
        self.assertTrue(em._migration_marker().exists(), "没有 JSON 也算迁移完成，避免每次启动重试")

    def test_no_marker_when_json_write_path_recreated(self):
        """标记存在时即使 JSON 被别的代码重新写出来，也不再回灌。"""
        self._write_json(["会被回灌的条目"])
        em.migrate_from_old_memory_system()
        em.delete_long_term_memories_by_ids(1, [r["id"] for r in em.find_long_term_memories(1)])
        self._write_json(["会被回灌的条目"])  # 个人印象写回 JSON 时会带上 memories 数组
        em.migrate_from_old_memory_system()
        self.assertEqual(em.find_long_term_memories(1), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
