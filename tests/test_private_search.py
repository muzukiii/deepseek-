"""私聊联网搜索回归测试。

线上现象（2026-10-27 17:20:57）：私聊发一张动画表情，模型回了
"…等我查一下再告诉千亿大哥哥！" 并带上 {"search": "碧蓝档案 …"}，
但 private_handler 只发 reply_text、把 search 字段整个丢掉，于是那句承诺
发出去就再也没有下文。这里把搜索循环的每条闸门都钉住。

纯打桩：不联网、不调真实 LLM、不碰 history.db。
无 pytest 时可 `python tests/test_private_search.py` 直跑。
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import private_handler as ph
from astrabot.chat_service.search_engine import MAX_SEARCH_ROUNDS, promises_search


class FakeConfig:
    name_cn = "小池"
    extra_style = "__STYLE__"
    history_messages = 25
    search_enabled = True
    image_analyzer = "analyzer"


class FakeSender:
    card = ""
    nickname = "千亿"


class FakeEvent:
    def __init__(self, text="这个表情包是谁", user_id=2041718160, message_id=1795695694):
        self.user_id = user_id
        self.message_id = message_id
        self.sender = FakeSender()
        self._text = text

    def get_plaintext(self):
        return self._text

    def get_message(self):
        return []


class FakeBot:
    self_id = "3540205153"

    def __init__(self):
        self.sent = []

    async def send_private_msg(self, user_id=None, message=None):
        self.sent.append(message)


def _sent_text(bot):
    """把发出的所有气泡拼成一段文本，便于断言。"""
    out = []
    for message in bot.sent:
        for seg in message:
            if seg.type == "text":
                out.append(seg.data.get("text", ""))
    return " ".join(out)


class PrivateSearchTest(unittest.TestCase):
    def setUp(self):
        self._orig = {
            name: getattr(ph, name, None)
            for name in (
                "generate_reply",
                "run_search",
                "select_context",
                "append_message",
                "trim_history",
                "get_history_since",
                "schedule_summarize",
                "sticker_prompt_catalog",
                "sticker_url",
                "build_main_prompt",
                "get_config",
            )
        }
        self.prompts = []          # 每次 generate_reply 收到的 prompt
        self.search_calls = []     # 每次 run_search 的 (query, context)
        self.history_rows = []     # 记进历史的 bot 回复
        self.config = FakeConfig()

        async def _select_context(*a, **kw):
            return []

        async def _append(conversation_id, row):
            if row.get("msg_type") == "bot":
                self.history_rows.append(row["message"])

        async def _trim(*a, **kw):
            return None

        async def _since(*a, **kw):
            return []

        def _schedule(*a, **kw):
            return None

        async def _catalog(query=""):
            return ""

        async def _sticker_url(sid):
            return None

        ph.select_context = _select_context
        ph.append_message = _append
        ph.trim_history = _trim
        ph.get_history_since = _since
        ph.schedule_summarize = _schedule
        ph.sticker_prompt_catalog = _catalog
        ph.sticker_url = _sticker_url
        ph.build_main_prompt = lambda **kw: "PROMPT __STYLE__"
        ph.get_config = lambda: self.config

    def tearDown(self):
        for name, fn in self._orig.items():
            if fn is None:
                # 旧版 private_handler 根本没有这个属性（没接搜索），
                # 测试期间是硬注入的，收尾要删掉，别留下 None 冒充函数。
                if hasattr(ph, name):
                    delattr(ph, name)
            else:
                setattr(ph, name, fn)

    def _stub_replies(self, replies, summaries=None):
        """replies: 依次返回的 (text, json)；用完就重复最后一条。"""
        state = {"n": 0}

        async def _generate(prompt):
            self.prompts.append(prompt)
            idx = min(state["n"], len(replies) - 1)
            state["n"] += 1
            return replies[idx]

        async def _search(query, context):
            self.search_calls.append((query, context))
            summary = ""
            if summaries:
                summary = summaries[min(len(self.search_calls) - 1, len(summaries) - 1)]
            return {"summary": summary, "sources": []}

        ph.generate_reply = _generate
        ph.run_search = _search
        return state

    def _run(self, event=None):
        bot = FakeBot()
        asyncio.run(ph.handle_private_message(bot, event or FakeEvent()))
        return bot

    def test_search_field_runs_search_and_replies_again(self):
        """模型给了 search 就必须真去搜，并把带搜索结果的第二次回复发出去。"""
        self._stub_replies(
            [
                ("等我查一下再告诉你！", {"search": "碧蓝档案 紫发光环 角色"}),
                ("是《碧蓝档案》的角瞳哦。", {}),
            ],
            summaries=["搜索摘要：这个角色叫角瞳。"],
        )
        bot = self._run()
        self.assertEqual(len(self.search_calls), 1, self.search_calls)
        self.assertEqual(self.search_calls[0][0], "碧蓝档案 紫发光环 角色")
        self.assertIn("私聊中 千亿(2041718160) 说：", self.search_calls[0][1])
        text = _sent_text(bot)
        self.assertIn("等我查一下", text)          # 搜索前先把已有回复发出去
        self.assertIn("角瞳", text)                # 搜索后的补充回复也要发
        # 第二次 generate_reply 的 prompt 里必须带上搜索结果
        self.assertEqual(len(self.prompts), 2, self.prompts)
        self.assertIn("角瞳", self.prompts[1])
        self.assertIn("PROMPT", self.prompts[1])   # 基于原 prompt 追加，不是从零重建

    def test_no_search_single_call(self):
        """没有 search 字段时行为和以前一模一样：一次调用、一次发送。"""
        self._stub_replies([("普通回复。", {})])
        bot = self._run()
        self.assertEqual(self.search_calls, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("普通回复", _sent_text(bot))

    def test_blank_search_ignored(self):
        """空白 search 不该触发付费路径。"""
        self._stub_replies([("好哦。", {"search": "   "})])
        bot = self._run()
        self.assertEqual(self.search_calls, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("好哦", _sent_text(bot))

    def test_search_disabled_skips_paid_path(self):
        """search_enabled=false 时整条花钱路径都不走，只发已有回复。"""
        self.config.search_enabled = False
        self._stub_replies([("等我查一下。", {"search": "碧蓝档案 角色"})])
        bot = self._run()
        self.assertEqual(self.search_calls, [])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("等我查一下", _sent_text(bot))

    def test_rounds_capped_no_infinite_paid_loop(self):
        """模型每轮都继续要搜索时必须在 MAX_SEARCH_ROUNDS 收手。"""
        self._stub_replies([("还在查。", {"search": "再搜一次"})], summaries=["摘要"])
        self._run()
        self.assertEqual(len(self.search_calls), MAX_SEARCH_ROUNDS, self.search_calls)
        self.assertEqual(len(self.prompts), MAX_SEARCH_ROUNDS + 1)

    def test_interim_reply_recorded_in_history(self):
        """搜索前发出的那句要进历史，否则上下文里只剩用户的问、没有机器人的答。"""
        self._stub_replies(
            [("等我查一下。", {"search": "q"}), ("查到了。", {})],
            summaries=["摘要"],
        )
        self._run()
        self.assertEqual(len(self.history_rows), 2, self.history_rows)
        self.assertIn("等我查一下", self.history_rows[0])
        self.assertIn("查到了", self.history_rows[1])

    def test_empty_search_summary_still_replies(self):
        """搜索失败（空摘要）也要走注入重生成，不能把用户晾着。"""
        self._stub_replies(
            [("等我查一下。", {"search": "q"}), ("没搜到，但我猜是角瞳。", {})],
            summaries=[""],
        )
        bot = self._run()
        self.assertEqual(len(self.search_calls), 1)
        self.assertIn("没搜到", _sent_text(bot))
        self.assertIn("联网搜索失败", self.prompts[1])

    def test_empty_reply_after_search_sends_nothing_extra(self):
        """搜索后模型给了空回复：不该再发一条空消息，也不该抛异常。"""
        self._stub_replies(
            [("等我查一下。", {"search": "q"}), ("", {})],
            summaries=["摘要"],
        )
        bot = self._run()
        self.assertIn("等我查一下", _sent_text(bot))
        self.assertEqual(len(self.history_rows), 1, self.history_rows)

    def test_promise_without_search_field_retries_once(self):
        """说了"等我查一下"却漏了 search 字段：必须纠正一次，不能把用户晾着。"""
        self._stub_replies(
            [
                ("来啦来啦，重新搜一轮～等我几分钟，有结果马上汇报给千亿大哥哥哦", {}),
                ("这次真的查，给我几秒！", {"search": "碧蓝档案 紫发 角耳 光环"}),
                ("查到了，是《碧蓝档案》的角瞳。", {}),
            ],
            summaries=["摘要"],
        )
        bot = self._run()
        self.assertEqual(len(self.search_calls), 1, self.search_calls)
        self.assertEqual(self.search_calls[0][0], "碧蓝档案 紫发 角耳 光环")
        self.assertEqual(len(self.prompts), 3, self.prompts)
        self.assertIn("没有给出 search 字段", self.prompts[1])
        self.assertIn("角瞳", _sent_text(bot))

    def test_promise_retry_gives_up_after_one(self):
        """纠正一次仍然不搜：只多发一次生成，不循环、不搜、只发一条。"""
        self._stub_replies([("等我查一下，马上汇报。", {})])
        bot = self._run()
        self.assertEqual(self.search_calls, [])
        self.assertEqual(len(self.prompts), 2, self.prompts)
        self.assertEqual(len(self.history_rows), 1, self.history_rows)
        self.assertIn("等我查一下", _sent_text(bot))


class SearchPromiseHeuristicTest(unittest.TestCase):
    """promises_search() 的判定：线上真实文本必须被抓住，已完成/普通回复不能误伤。"""

    def test_real_failure_text_is_promise(self):
        text = (
            "来啦来啦，重新搜一轮～角状耳朵加光环，这次换个思路，看看能不能锁定到具体角色！"
            "\n等我几分钟，有结果马上汇报给千亿大哥哥哦 (๑•̀ㅂ•́)و✧"
        )
        self.assertTrue(promises_search(text))

    def test_completed_search_is_not_promise(self):
        self.assertFalse(
            promises_search("查到啦！光环在碧蓝档案里是每个学生独有的标记，颜色造型各不相同～")
        )

    def test_plain_reply_is_not_promise(self):
        self.assertFalse(promises_search("今天天气不错，出去走走吧。"))

    def test_explicit_plain_search_request_is_promise(self):
        self.assertTrue(promises_search("等我查一下。"))
        self.assertTrue(promises_search("还没搜到，我再搜一次。"))

    def test_group_path_uses_the_guard(self):
        """群聊和私聊共用同一道兜底（函数体依赖太多，只做源码级断言）。"""
        import inspect

        from astrabot.chat_service import message_handler as mh

        self.assertIn("promises_search", inspect.getsource(mh._ai_call_with_search))


if __name__ == "__main__":
    unittest.main(verbosity=2)
