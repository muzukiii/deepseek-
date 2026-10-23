"""搜索代理护栏回归测试：早停 / 墙钟超时 / respond / 轮数用尽 / token 记账。
纯打桩，不联网、不调真实 LLM。无 pytest 时可 `python tests/test_search_guards.py` 直跑。"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import search_engine as se


class ToolCall:
    def __init__(self, name, args, tid):
        self.id = tid
        self.function = type("F", (), {"name": name, "arguments": json.dumps(args)})()


class Msg:
    def __init__(self, tool_calls=None, content=None):
        self.tool_calls = tool_calls
        self.content = content


class _Usage:
    """模仿 openai 的 Usage 对象（search_engine 只用到 model_dump()）。"""

    def model_dump(self):
        return {"prompt_tokens": 100, "completion_tokens": 20}


class Resp:
    def __init__(self, msg, usage=None):
        self.choices = [type("C", (), {"message": msg})()]
        self.usage = usage if usage is not None else _Usage()


class FakeConfig:
    API_MODEL = "test-model"
    API_KEY = "k"
    API_BASE_URL = "http://x"
    web_search_engine = "search_std"

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class FakeClient:
    """chat.completions.create 依次吐出预置结果；('sleep', s, msg) 先等待再返回，用于触发超时。"""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0
        comp = type("Comp", (), {})()

        async def create(**kwargs):
            self.calls += 1
            idx = min(self.calls - 1, len(self._results) - 1)
            action = self._results[idx]
            if isinstance(action, tuple) and action and action[0] == "sleep":
                await asyncio.sleep(action[1])
                action = action[2]
            return Resp(action)

        comp.create = create
        self.chat = type("Chat", (), {"completions": comp})()


def _patch(client, config, search_log=None):
    se.AsyncOpenAI = lambda **kw: client
    se.get_config = lambda: config

    async def _web_search(query):
        if search_log is not None:
            search_log.append(query)
        return f"[搜索结果 {query}]"

    se.web_search = _web_search

    async def _fetch(url):
        return "fetched"

    se.fetch_url = _fetch


def test_respond_returns_immediately():
    client = FakeClient([Msg(content="thinking", tool_calls=[ToolCall("respond", {"message": "ANSWER"}, "t1")])])
    _patch(client, FakeConfig())
    res = asyncio.run(se.run_search("q", "ctx"))
    assert res["summary"] == "ANSWER", res
    assert client.calls == 1, client.calls  # 命中 respond 即返回，不多烧调用


def test_no_tool_early_stop():
    # 模型连续只回文本不调工具；limit=2 → 早停而非无限循环，随后走强制总结。
    client = FakeClient([
        Msg(content="hi", tool_calls=None),
        Msg(content="hi2", tool_calls=None),
        Msg(content=None, tool_calls=[ToolCall("respond", {"message": "FORCE"}, "f1")]),
    ])
    _patch(client, FakeConfig(search_no_tool_turns_limit=2, search_wallclock_timeout=10))
    res = asyncio.run(se.run_search("q", "ctx"))
    assert res["summary"] == "FORCE", res
    assert client.calls == 3, client.calls  # 2 次纯文本触发早停 + 1 次强制总结


def test_wallclock_timeout():
    # 墙钟预算 0.1s，首次调用 sleep 1s → 捕获 TimeoutError，快速返回空 best-effort。
    client = FakeClient([("sleep", 1.0, Msg(content="never", tool_calls=None))])
    _patch(client, FakeConfig(search_wallclock_timeout=0.1))
    res = asyncio.run(se.run_search("q", "ctx"))
    assert res["summary"] == "", res  # 未抛异常、返回空即调用方可降级


def test_max_tool_calls_forces_summary():
    n = se.MAX_TOOL_CALLS
    client = FakeClient(
        [Msg(content=None, tool_calls=[ToolCall("web_search", {"query": "x"}, f"t{i}")]) for i in range(n)]
        + [Msg(content=None, tool_calls=[ToolCall("respond", {"message": "DONE"}, "last")])]
    )
    seen: list[str] = []
    _patch(client, FakeConfig(search_wallclock_timeout=10), search_log=seen)
    res = asyncio.run(se.run_search("q", "ctx"))
    assert res["summary"] == "DONE", res
    assert client.calls == n + 1, client.calls  # n 轮工具用尽 + 1 次强制总结
    assert seen == ["x"] * n, seen  # web_search 工具名确实路由到了模块级搜索函数


def test_usage_recorded_as_search():
    # 搜索代理的 token 必须入账，否则联网开销对账本完全不可见。
    records: list[dict] = []
    client = FakeClient([Msg(content=None, tool_calls=[ToolCall("respond", {"message": "A"}, "t1")])])
    _patch(client, FakeConfig())

    def _record(**kw):
        records.append(kw)

    orig_record = se.usage_tracker.record
    se.usage_tracker.record = _record
    try:
        asyncio.run(se.run_search("q", "ctx"))
    finally:
        se.usage_tracker.record = orig_record
    assert len(records) == 1, records
    assert records[0]["kind"] == "search", records[0]
    assert records[0]["model"] == "test-model", records[0]
    assert records[0]["usage"]["prompt_tokens"] == 100, records[0]


if __name__ == "__main__":
    test_respond_returns_immediately()
    test_no_tool_early_stop()
    test_wallclock_timeout()
    test_max_tool_calls_forces_summary()
    test_usage_recorded_as_search()
    print("ALL SEARCH GUARD TESTS PASSED")
