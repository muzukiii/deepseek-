"""回归：写入失败后不能把写事务挂着（会永久占用 memories.db 的写锁）。

真实事故（2026-10-27）：add_long_term_memory 命中 UNIQUE 约束走 IntegrityError
分支时只 return False，没有 rollback；Python sqlite3 在 INSERT 前已隐式 BEGIN，
于是 bot 的连接一直持有写事务——它自己还能写，但任何外部进程 DELETE 都是
"database is locked"，WAL 也无法 checkpoint（实测 -wal 积压 4.1 MB）。
"""
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import astrabot.chat_service.enhanced_memory as em  # noqa: E402


class WriteLockTest(unittest.TestCase):
    def setUp(self):
        self._saved = (em.DATA_DIR, em.MEMORY_DB, em._local)
        self.tmp = Path(tempfile.mkdtemp(prefix="memlock-"))
        em.DATA_DIR = self.tmp
        em.MEMORY_DB = self.tmp / "memories.db"
        em._local = threading.local()

    def tearDown(self):
        em.DATA_DIR, em.MEMORY_DB, em._local = self._saved

    def _external_write_ok(self) -> bool:
        """另一个连接能否拿到写锁（模拟运维脚本 / 第二个进程）。"""
        conn = sqlite3.connect(str(em.MEMORY_DB), timeout=0.5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
            return True
        except sqlite3.OperationalError:
            return False
        finally:
            conn.close()

    def test_duplicate_long_term_memory_releases_write_lock(self):
        self.assertTrue(em.add_long_term_memory(1, "小池喜欢用颜文字"))
        self.assertTrue(self._external_write_ok(), "首次写入后不该占着写锁")

        self.assertFalse(em.add_long_term_memory(1, "小池喜欢用颜文字"))  # UNIQUE 命中
        self.assertTrue(
            self._external_write_ok(),
            "重复记忆被拒后必须释放写锁，否则外部进程永远 database is locked",
        )

    def test_blank_long_term_memory_releases_write_lock(self):
        self.assertFalse(em.add_long_term_memory(1, "   "))
        self.assertTrue(self._external_write_ok())

    def test_duplicate_fact_releases_write_lock(self):
        em.add_fact(1, "千亿喜欢喝奶茶", user_id="111")
        em.add_fact(1, "千亿喜欢喝奶茶", user_id="111")  # 走 UPDATE access_count 分支
        self.assertTrue(self._external_write_ok())

    def test_delete_after_duplicate_still_works(self):
        """挂起的事务还会让后续 DELETE 把无关写入一起提交，这里验证删除本身正常。"""
        em.add_long_term_memory(1, "小池喜欢用颜文字")
        em.add_long_term_memory(1, "小池喜欢用颜文字")
        rows = em.find_long_term_memories(1, keyword="颜文字")
        self.assertEqual(len(rows), 1)
        self.assertEqual(em.delete_long_term_memories_by_ids(1, [r["id"] for r in rows]), 1)
        self.assertEqual(em.find_long_term_memories(1, keyword="颜文字"), [])
        self.assertTrue(self._external_write_ok())


if __name__ == "__main__":
    unittest.main(verbosity=2)
