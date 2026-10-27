"""私聊图片描述持久化回归测试。

线上现象（2026-10-27 18:0x）：同一张表情/立绘过一轮之后再问"这是谁"，
模型只剩自己上一句的"角耳+光环"，搜出来全是泛词（"碧蓝档案 光环 角色"），
永远锁不死角色。原因是 append_message 在识图之前就把消息落了库（只存文本
和 image 占位），分析出的描述只拼进本轮 text 就随请求结束消失了——群聊那边
有 set_image_descriptions 回写，私聊没有。

纯打桩：不联网、不调真实 LLM、不碰 history.db。
无 pytest 时可 `python tests/test_private_image_memory.py` 直跑。
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import private_handler as ph


class FakeConfig:
    name_cn = "小池"
    extra_style = "__STYLE__"
    history_messages = 25
    search_enabled = True
    image_analyzer = "analyzer"


class Seg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data or {}


class FakeSender:
    card = ""
    nickname = "千亿"


class FakeEvent:
    def __init__(self, text="这是谁", image_urls=(), user_id=2041718160, message_id=1795695694):
        self.user_id = user_id
        self.message_id = message_id
        self.sender = FakeSender()
        self._text = text
        self._urls = list(image_urls)

    def get_plaintext(self):
        return self._text

    def get_message(self):
        return [Seg("image", {"url": u}) for u in self._urls]


class FakeBot:
    self_id = "3540205153"

    def __init__(self):
        self.sent = []

    async def send_private_msg(self, user_id=None, message=None):
        self.sent.append(message)


class PrivateImageMemoryTest(unittest.TestCase):
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
                "download_image_base64",
                "analyze_images",
                "set_image_descriptions",
            )
        }
        self.prompt_kwargs = []
        self.saved: list[tuple] = []
        self.vision_calls = []
        self.config = FakeConfig()

        async def _noop(*a, **kw):
            return None

        async def _select_context(*a, **kw):
            return []

        async def _catalog(query=""):
            return ""

        async def _sticker_url(sid):
            return None

        def _build_main_prompt(**kw):
            self.prompt_kwargs.append(kw)
            return "PROMPT __STYLE__"

        async def _save(group_id, updates):
            self.saved.append((group_id, updates))
            return len(updates)

        ph.select_context = _select_context
        ph.append_message = _noop
        ph.trim_history = _noop
        ph.get_history_since = _select_context
        ph.schedule_summarize = _noop
        ph.sticker_prompt_catalog = _catalog
        ph.sticker_url = _sticker_url
        ph.build_main_prompt = _build_main_prompt
        ph.set_image_descriptions = _save
        ph.get_config = lambda: self.config

    def tearDown(self):
        for name, fn in self._orig.items():
            if fn is None:
                if hasattr(ph, name):
                    delattr(ph, name)
            else:
                setattr(ph, name, fn)

    def _stub_download(self, failed=()):
        async def _download(url):
            if url in failed:
                return None
            return f"b64:{url}"

        ph.download_image_base64 = _download

    def _stub_vision(self, result):
        async def _analyze(images, prompt):
            self.vision_calls.append(list(images))
            return result

        ph.analyze_images = _analyze

    def _stub_reply(self, text="好的。"):
        async def _generate(prompt):
            return text, {}

        ph.generate_reply = _generate

    def _run(self, event):
        asyncio.run(ph.handle_private_message(FakeBot(), event))

    def test_description_persisted_on_image_segment(self):
        """描述必须回写到那条消息的 image 段上（segments = [text] + image*N，下标 1 起）。"""
        self._stub_download()
        self._stub_vision({"image_1": "紫发、角状耳朵、头顶光环"})
        self._stub_reply()
        self._run(FakeEvent(text="这是谁", image_urls=["u1"]))
        self.assertEqual(
            self.saved,
            [(-2041718160, {1795695694: {1: "紫发、角状耳朵、头顶光环"}})],
            self.saved,
        )
        # 本轮 prompt 里也要能看到描述
        self.assertIn("紫发", self.prompt_kwargs[0]["trigger_marker"])

    def test_alignment_when_first_image_fails_to_download(self):
        """第一张下载失败时，描述不能挂到第一张的下标上。"""
        self._stub_download(failed=("bad",))
        self._stub_vision({"image_1": "第二张图的描述"})
        self._stub_reply()
        self._run(FakeEvent(text="这是谁", image_urls=["bad", "good"]))
        self.assertEqual(
            self.saved,
            [(-2041718160, {1795695694: {2: "第二张图的描述"}})],
            self.saved,
        )
        # 只把下载成功的那张送去识图
        self.assertEqual(self.vision_calls, [["b64:good"]])

    def test_positional_fallback_when_keys_are_not_image_n(self):
        """视觉模型没按 image_1 约定给键时，退化成按位置对齐，不能丢。"""
        self._stub_download()
        self._stub_vision({"角色A": "黑发、黑角、黑翅膀"})
        self._stub_reply()
        self._run(FakeEvent(text="这是谁", image_urls=["u1"]))
        self.assertEqual(
            self.saved,
            [(-2041718160, {1795695694: {1: "黑发、黑角、黑翅膀"}})],
            self.saved,
        )

    def test_empty_analysis_saves_nothing(self):
        """识图没结果时不写回、不崩溃，消息本身已落库。"""
        self._stub_download()
        self._stub_vision({})
        self._stub_reply()
        self._run(FakeEvent(text="这是谁", image_urls=["u1"]))
        self.assertEqual(self.saved, [])

    def test_multiple_images_map_to_their_own_slots(self):
        self._stub_download()
        self._stub_vision({"image_1": "图一描述", "image_2": "图二描述"})
        self._stub_reply()
        self._run(FakeEvent(text="这俩是谁", image_urls=["u1", "u2"]))
        self.assertEqual(
            self.saved,
            [(-2041718160, {1795695694: {1: "图一描述", 2: "图二描述"}})],
            self.saved,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
