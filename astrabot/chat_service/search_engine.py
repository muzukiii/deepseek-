"""联网搜索：智谱原生 web_search 接口 + GLM tool calling 实现多轮搜索、网页抓取和结果总结"""

from __future__ import annotations

import asyncio
import json
from html.parser import HTMLParser

import httpx
from openai import AsyncOpenAI

from astrabot.chat_service import usage_tracker
from astrabot.chat_service.http_client import get_http_client, request_with_retry
from astrabot.config import get_config
from astrabot.logmanager.logger import logger

MAX_TOOL_CALLS = 10  # 搜索代理最大工具调用次数

# 一轮对话里最多让搜索代理跑几遍（群聊/私聊共用）：模型拿到结果后可能再要一次
# 搜索，没有上限就会一路循环烧钱。
MAX_SEARCH_ROUNDS = 3

# 整个搜索代理的墙钟预算（秒）：单次 LLM 调用已有 20s 超时，但最多 10 轮
# (每轮 LLM+可能的抓取) 仍可累加成分钟级，故加一道总时限，超时即返回 best-effort。
# 可由 config.search_wallclock_timeout 覆盖。
_DEFAULT_WALLCLOCK = 120
# 连续多少轮模型「只回文本、不调任何工具」即判定其已答完/卡住并早停，
# 修复原先该分支不计数导致的无限循环 + 每轮白烧一次付费调用。
# 可由 config.search_no_tool_turns_limit 覆盖。
_DEFAULT_NO_TOOL_LIMIT = 2
# 搜索代理单轮 LLM 调用的超时。与主聊天路径一致取 60s：主模型是推理模型，
# 且注入搜索结果后 prompt 可达 5k+ token，20s 会稳定超时（实测第三轮必超时）。
_LLM_TIMEOUT = 60
# 智谱联网搜索档位：search_std ¥0.01/次，search_pro ¥0.03，sogou_/quark_ ¥0.05。
# 可由 config.web_search_engine 覆盖。
_DEFAULT_SEARCH_ENGINE = "search_std"
# 单条命中结果注入 prompt 的正文上限：智谱 content 可达数千字，5 条 × 多轮
# 会把上下文撑到不可控，故每条截断（来源链接与标题不截）。
_PER_RESULT_CAP = 900

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for real-time information. Use 3-5 keywords for best results. Include current date for time-sensitive topics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query, 3-5 keywords"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and read the content of a webpage to get detailed information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the webpage to fetch"}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "respond",
            "description": "Call this when you have enough information to give a final answer to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Final response to the user"}
                },
                "required": ["message"],
            },
        },
    },
]

def _build_system_prompt() -> str:
    from datetime import datetime
    now = datetime.now().strftime("%Y年%m月%d日")
    return f"""你是一个智能搜索助手。你的任务是：
1. 分析用户的问题，理解他们需要什么信息
2. 使用 web_search 工具搜索相关关键词
3. 如果搜索结果不够详细，使用 fetch_url 获取具体网页内容
4. 如果搜索结果不理想，尝试不同的关键词重新搜索
5. 当你收集到足够信息后，使用 respond 工具给出完整的回答

搜索时注意：
- 对于有时效性的问题，在搜索关键词中包含当前日期
- 优先查看权威来源的内容
- 综合多个来源的信息给出答案

当前日期：{now}"""


def _extract_text(html: str) -> str:
    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.result = []
            self.skip = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.skip = False

        def handle_data(self, data):
            if not self.skip:
                stripped = data.strip()
                if stripped:
                    self.result.append(stripped)

    extractor = TextExtractor()
    extractor.feed(html)
    return "\n".join(extractor.result)


def _get_httpx_client(timeout: int = 20) -> httpx.AsyncClient:
    """创建统一的 httpx 客户端（默认直连，可用 .env 切换代理）。"""
    return get_http_client(timeout=timeout)


def _record_usage(response, model: str) -> None:
    """把搜索代理这一轮的 token 记入 usage_tracker。

    以前 search_engine 从不记账，联网开销对账本完全不可见。
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    try:
        usage_tracker.record(kind="search", model=model, usage=usage.model_dump())
    except Exception as e:
        logger.warning(f"usage record failed: {e}")


async def web_search(query: str) -> str:
    """调用智谱原生联网搜索，把命中结果格式化成纯文本交给模型阅读。

    不抛异常：失败以文本形式返回，搜索代理会据此换关键词重试。
    """
    config = get_config()
    engine = getattr(config, "web_search_engine", "") or _DEFAULT_SEARCH_ENGINE
    url = config.API_BASE_URL.rstrip("/") + "/web_search"
    try:
        resp = await request_with_retry(
            "POST",
            url,
            timeout=20,
            headers={"Authorization": f"Bearer {config.API_KEY}"},
            json={"search_engine": engine, "search_query": query, "count": 5},
        )
        data = resp.json()
    except Exception as e:
        logger.error(f"web_search failed: {e}")
        return f"web_search failed: {e}"

    results = data.get("search_result") or []
    if not results:
        # 鉴权/参数错误的业务码也走 200，统一降级为「无结果」让模型重试
        logger.warning(f"web_search no results ({engine}): {str(data)[:200]}")
        return "web_search 没有返回结果，请换一组关键词重试。"

    blocks = []
    for idx, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        media = (item.get("media") or "").strip()
        date = (item.get("publish_date") or "").strip()
        meta = "｜".join(x for x in (media, date) if x)
        content = (item.get("content") or "").strip()[:_PER_RESULT_CAP]
        head = f"[{idx}] {title}"
        if meta:
            head += f"（{meta}）"
        blocks.append(f"{head}\n{content}\n来源: {link}")

    logger.info(f"web_search({query}) -> {len(results)} results via {engine}")
    return f"「{query}」搜索结果 {len(results)} 条：\n\n" + "\n\n".join(blocks)


async def fetch_url(url: str) -> str:
    try:
        resp = await request_with_retry(
            "GET",
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.encoding = "utf-8"
        content = _extract_text(resp.text)
        return content[:8000]
    except Exception as e:
        logger.error(f"fetch_url failed {url}: {e}")
        return f"Failed to fetch URL: {e}"


async def run_search(initial_query: str, context: str) -> dict:
    """启动搜索代理：用 GLM 多轮 tool calling，联网结果来自智谱 web_search 接口"""
    config = get_config()
    summary_result: dict = {"summary": "", "sources": []}

    http_client = _get_httpx_client(timeout=_LLM_TIMEOUT)
    client = AsyncOpenAI(
        api_key=config.API_KEY,
        base_url=config.API_BASE_URL,
        http_client=http_client,
    )
    # 推理档位沿用主聊天 tier1 的设置；搜索代理用的就是同一个 API_MODEL。
    effort = getattr(config, "reasoning_effort", "") or None

    try:
        messages = [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": f"用户的问题是：{initial_query}\n\n对话背景：{context}"},
        ]

        tool_calls_count = 0
        no_tool_turns = 0

        # 墙钟预算与早停阈值可经 config/.env 调；缺省走安全值。
        wallclock = getattr(config, "search_wallclock_timeout", _DEFAULT_WALLCLOCK)
        no_tool_limit = max(1, getattr(config, "search_no_tool_turns_limit", _DEFAULT_NO_TOOL_LIMIT))

        try:
            async with asyncio.timeout(wallclock):
                while tool_calls_count < MAX_TOOL_CALLS:
                    response = await client.chat.completions.create(
                        model=config.API_MODEL,
                        messages=messages,
                        tools=TOOLS,
                        reasoning_effort=effort,
                        timeout=_LLM_TIMEOUT,
                    )
                    _record_usage(response, config.API_MODEL)

                    msg = response.choices[0].message

                    if not msg.tool_calls:
                        # 模型只回文本、不调 respond 工具时，原先直接 continue 且不计数，
                        # 会无限循环、每轮白烧一次付费调用 → 连续若干轮即判定其已答完并早停。
                        messages.append({"role": "assistant", "content": msg.content or ""})
                        no_tool_turns += 1
                        if no_tool_turns >= no_tool_limit:
                            logger.info(
                                f"Search agent: {no_tool_turns} consecutive no-tool turns, "
                                "ending search early with best-effort summary"
                            )
                            break
                        continue
                    no_tool_turns = 0

                    assistant_msg = {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                            for tc in msg.tool_calls
                        ],
                    }
                    messages.append(assistant_msg)

                    answer_ready = False
                    for tc in msg.tool_calls:
                        tool_calls_count += 1
                        name = tc.function.name
                        args = json.loads(tc.function.arguments)

                        if name == "respond":
                            msg_text = args.get("message", "")
                            logger.info(f"Search agent: respond({len(msg_text)} chars)")
                            logger.debug(f"Respond content: {msg_text[:500]}")
                            summary_result["summary"] = msg_text
                            answer_ready = True
                            break

                        if name == "web_search":
                            result_text = await web_search(args["query"])
                        elif name == "fetch_url":
                            url = args["url"]
                            logger.info(f"Search agent: fetch_url({url})")
                            result_text = await fetch_url(url)
                        else:
                            result_text = f"Unknown tool: {name}"

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result_text,
                        })

                    if answer_ready:
                        break

                if not summary_result["summary"]:
                    # 轮数用尽 / 早停仍无 respond：强制一次总结兜底。
                    logger.warning(f"Search agent reached max tool calls ({MAX_TOOL_CALLS}), forcing summary")
                    force_prompt = "你已进行多轮搜索，请根据已获取的信息，用 respond 工具给出最终总结回答。"
                    messages.append({"role": "user", "content": force_prompt})
                    response = await client.chat.completions.create(
                        model=config.API_MODEL,
                        messages=messages,
                        tools=TOOLS,
                        reasoning_effort=effort,
                        timeout=_LLM_TIMEOUT,
                    )
                    _record_usage(response, config.API_MODEL)
                    msg = response.choices[0].message
                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            if tc.function.name == "respond":
                                forced = json.loads(tc.function.arguments).get("message", "")
                                logger.info(f"Search agent force respond ({len(forced)} chars)")
                                logger.debug(f"Forced respond content: {forced[:500]}")
                                summary_result["summary"] = forced
                    elif msg.content:
                        logger.info(f"Search agent force respond (text, {len(msg.content)} chars)")
                        summary_result["summary"] = msg.content
        except TimeoutError:
            # 超过墙钟预算：放弃后续轮次，返回当前 best-effort 结果（可能为空，调用方已能降级）。
            logger.warning(
                f"Search agent exceeded wall-clock budget {wallclock}s; "
                "returning best-effort summary"
            )

    except Exception:
        # 搜索代理整体失败：返回空 summary，调用方本来就会降级为不补充回复。
        # 带 traceback——这条路径不常走，出错时不留线索很难定位。
        logger.exception("Search agent failed")

    return summary_result
