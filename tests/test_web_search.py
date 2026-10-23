"""智谱 web_search 封装回归测试：请求形状 / 结果排版 / 截断 / 失败降级。
全程打桩 request_with_retry，不发真实付费请求。
无 pytest 时可 `python tests/test_web_search.py` 直跑。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import search_engine as se

_CAPTURED: list[dict] = []


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch_request(payload=None, exc=None):
    se.get_config = lambda: type("C", (), {
        "API_KEY": "k",
        "API_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
        "web_search_engine": "",
    })()

    async def _req(method, url, **kwargs):
        _CAPTURED.append({"method": method, "url": url, **kwargs})
        if exc is not None:
            raise exc
        return FakeResp(payload)

    se.request_with_retry = _req


def _run():
    _CAPTURED.clear()
    return asyncio.run(se.web_search("班味 是什么梗"))


def test_request_shape():
    _patch_request({"search_result": []})
    _run()
    call = _CAPTURED[0]
    assert call["method"] == "POST"
    # 斜杠结尾的 base_url 不能拼出 //web_search
    assert call["url"] == "https://open.bigmodel.cn/api/paas/v4/web_search", call["url"]
    assert call["headers"]["Authorization"] == "Bearer k"
    # 参数名必须是 search_query（写错会 400 / code 1214）
    assert call["json"] == {"search_engine": "search_std", "search_query": "班味 是什么梗", "count": 5}, call["json"]


def test_results_formatted_with_source():
    _patch_request({
        "search_result": [
            {"title": "「班味」到底是什么", "content": "打工人身上的疲惫气质。", "link": "https://a.com/1",
             "media": "某报", "publish_date": "2026-09-01"},
            {"title": "无元数据的命中", "content": "另一段解释。", "link": "https://b.com/2"},
        ]
    })
    out = _run()
    assert "[1] 「班味」到底是什么（某报｜2026-09-01）" in out, out
    assert "打工人身上的疲惫气质。" in out
    assert "来源: https://a.com/1" in out
    assert "[2] 无元数据的命中" in out
    assert "来源: https://b.com/2" in out
    assert "搜索结果 2 条" in out


def test_long_content_truncated():
    _patch_request({"search_result": [{"title": "t", "content": "长" * 5000, "link": "https://a.com"}]})
    out = _run()
    assert "长" * se._PER_RESULT_CAP in out
    assert "长" * (se._PER_RESULT_CAP + 1) not in out


def test_http_error_returns_text_not_exception():
    _patch_request(exc=RuntimeError("boom"))
    out = _run()
    assert out.startswith("web_search failed:"), out
    assert "boom" in out


def test_business_error_degrades_to_retry_hint():
    # 智谱参数/鉴权错误也是 200，只是没有 search_result
    _patch_request({"code": 1214, "msg": "invalid param"})
    out = _run()
    assert "没有返回结果" in out, out


if __name__ == "__main__":
    test_request_shape()
    test_results_formatted_with_source()
    test_long_content_truncated()
    test_http_error_returns_text_not_exception()
    test_business_error_degrades_to_retry_hint()
    print("ALL WEB_SEARCH TESTS PASSED")
