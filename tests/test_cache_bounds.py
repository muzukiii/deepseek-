"""C-5 缓存有界化回归测试：TTL 淘汰 + 撤回集合上限。纯内存、无网络。
无 pytest 时可 `python tests/test_cache_bounds.py` 直跑。"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import message_handler as mh
from astrabot.chat_service.reply_controller import ReplyController


def test_prune_noop_under_cap():
    now = time.time()
    d = {"a": (1, now + 100)}
    mh._prune_ttl_cache(d, now, 3)
    assert d == {"a": (1, now + 100)}


def test_prune_drops_expired_then_oldest_expiring():
    now = time.time()
    d = {
        "e1": (1, now - 10),
        "e2": (2, now - 5),
        "f1": (3, now + 50),
        "f2": (4, now + 100),
        "f3": (5, now + 20),
    }
    mh._prune_ttl_cache(d, now, 2)
    assert "e1" not in d and "e2" not in d
    assert set(d) == {"f1", "f2"}, d


def test_recalled_set_bounded_and_fifo():
    ReplyController._recalled_messages.clear()
    ReplyController._RECALLED_MAX = 3
    for i in range(5):
        ReplyController.add_recalled(1, i)
    assert len(ReplyController._recalled_messages) <= 3
    assert ReplyController.is_recalled(1, 4)
    assert not ReplyController.is_recalled(1, 0)
    assert ReplyController.is_recalled(1, 2)
    ReplyController._recalled_messages.clear()


def test_recalled_ttl_prune():
    ReplyController._recalled_messages.clear()
    old_max = ReplyController._RECALLED_MAX
    ReplyController._RECALLED_MAX = 100000
    ReplyController.add_recalled(7, 99)
    assert ReplyController.is_recalled(7, 99)
    ReplyController._recalled_messages[(7, 99)] = time.time() - ReplyController._RECALLED_TTL - 1
    ReplyController._prune_recalled(time.time())
    assert not ReplyController.is_recalled(7, 99)
    ReplyController._RECALLED_MAX = old_max


if __name__ == "__main__":
    test_prune_noop_under_cap()
    test_prune_drops_expired_then_oldest_expiring()
    test_recalled_set_bounded_and_fifo()
    test_recalled_ttl_prune()
    print("ALL CACHE-BOUND TESTS PASSED")
