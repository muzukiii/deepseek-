"""AI 客户端：统一封装 chat 与图片分析调用"""

from __future__ import annotations

import json
import os

import anthropic
import httpx
from openai import AsyncOpenAI

from astrabot.chat_service import usage_tracker
from astrabot.chat_service.http_client import get_http_client
from astrabot.config import get_config
from astrabot.logmanager.logger import logger

API_BASE_URLS = {
    "SILICONFLOW": "https://api.siliconflow.cn/v1",
    "DEEPSEEK": "https://api.deepseek.com/v1",
    "MINIMAX": "https://api.minimaxi.com/anthropic",
}


def _get_httpx_client() -> httpx.AsyncClient:
    """创建统一的 httpx 客户端（默认直连，可用 .env 切换代理）。"""
    return get_http_client(timeout=30.0)


def _create_openai_client(base_url: str, api_key: str, http_client: httpx.AsyncClient) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)


def _create_anthropic_client(base_url: str, api_key: str, http_client: httpx.AsyncClient) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key, http_client=http_client)


async def chat(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    response_format: str | None = None,
    reasoning: bool = False,
    timeout: int = 30,
) -> dict:
    protocol_upper = protocol.upper()

    if protocol_upper == "OPENAI":
        http_client = _get_httpx_client()
        client = _create_openai_client(base_url=base_url, api_key=api_key, http_client=http_client)
        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "timeout": timeout,
            }
            if response_format == "json_object":
                kwargs["response_format"] = {"type": "json_object"}
            if reasoning:
                kwargs["reasoning_effort"] = "high"
            response = await client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            content = msg.content or ""
            reasoning_content = getattr(msg, "reasoning_content", None)
            usage = getattr(response, "usage", None)
            usage_tracker.record(
                kind="chat",
                model=model,
                usage=usage.model_dump() if usage is not None else None,
            )
            return {"content": content, "reasoning": reasoning_content}
        finally:
            await http_client.aclose()

    if protocol_upper == "ANTHROPIC":
        http_client = _get_httpx_client()
        client = _create_anthropic_client(base_url=base_url, api_key=api_key, http_client=http_client)
        try:
            anthropic_messages = [m for m in messages if m["role"] != "system"]
            system_content = next((m["content"] for m in messages if m["role"] == "system"), None)
            kwargs = {
                "model": model,
                "max_tokens": 4096,
                "messages": anthropic_messages,
            }
            if system_content:
                kwargs["system"] = system_content
            message = await client.messages.create(**kwargs)
            content = ""
            for block in message.content:
                if block.type == "text":
                    content += block.text
            anth_usage = getattr(message, "usage", None)
            if anth_usage is not None:
                usage_tracker.record(
                    kind="chat",
                    model=model,
                    usage={
                        "prompt_tokens": getattr(anth_usage, "input_tokens", 0),
                        "completion_tokens": getattr(anth_usage, "output_tokens", 0),
                    },
                )
            return {"content": content}
        finally:
            await http_client.aclose()

    raise ValueError(f"Unknown protocol: {protocol}")


async def analyze_images(
    images_base64: list[str],
    prompt: str,
) -> dict:
    """识图：默认复用主 API（DeepSeek 多模态），也可切到独立视觉服务。

    DeepSeek 的图片按 token 计费且有硬上限（每张 ≤1024 token），
    因此识图成本极低，无需单独申请视觉 API Key。
    """
    config = get_config()
    provider = (config.VISUAL_API_PROVIDER or "").strip().upper()

    # 解析使用哪个服务：主 API 还是独立视觉 API
    use_main_api = provider in ("", "MAIN", "DEEPSEEK") or not config.VISUAL_API_KEY
    if use_main_api:
        base_url = config.API_BASE_URL
        api_key = config.API_KEY
        model = config.VISUAL_API_MODEL or config.API_MODEL
    else:
        base_url = API_BASE_URLS.get(provider, API_BASE_URLS["SILICONFLOW"])
        api_key = config.VISUAL_API_KEY
        model = config.VISUAL_API_MODEL

    if not api_key or not model:
        logger.info("Image analysis skipped: no usable vision API configured")
        return {}

    # 单次请求的图片数量上限，避免群内刷图时一次性把成本放大
    max_images = max(1, config.vision_max_images)
    if len(images_base64) > max_images:
        logger.info(
            f"Image analysis: {len(images_base64)} images provided, "
            f"only analyzing the first {max_images}"
        )
        images_base64 = images_base64[:max_images]

    http_client = _get_httpx_client()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, http_client=http_client)

    content_parts = [{"type": "text", "text": prompt}]
    for b64 in images_base64:
        content_parts.append({
            "type": "image_url",
            # detail=low 会先把图缩到 512x512，token 更省；
            # 需要看清细节时可在 .env 里改成 high/original。
            "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": config.vision_detail,
            },
        })

    messages = [{"role": "user", "content": content_parts}]

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
            timeout=60,
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(_clean_json(content))
        usage = getattr(response, "usage", None)
        usage_tracker.record(
            kind="vision",
            model=model,
            usage=usage.model_dump() if usage is not None else None,
        )
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Image analysis returned non-JSON: {e}")
        return {}
    except Exception as e:
        logger.error(f"Image analysis failed: {e}")
        return {}
    finally:
        await http_client.aclose()


def _clean_json(raw: str) -> str:
    """去除 AI 输出中可能包裹的 markdown 代码块标记"""
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw[7:]
    elif raw.startswith("```"):
        raw = raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    return raw.strip()


REPLY_FIELD_CANDIDATES = (
    "reply",
    "content",
    "message",
    "response",
    "text",
    "answer",
    "回复",
    "内容",
)


def _extract_reply(parsed: dict) -> str:
    """从模型返回中尽力提取回复正文。

    模型有时会把回复放在 content / message / response 等同义字段里
    （即使提示词要求 reply）。此时不应直接判定为空并放弃，否则机器人
    会表现为"收到消息但不回复"。
    """
    if not isinstance(parsed, dict):
        return ""

    for key in REPLY_FIELD_CANDIDATES:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # 兜底：只有一个非空字符串字段时直接使用它
    strings = [
        v.strip()
        for k, v in parsed.items()
        if isinstance(v, str) and v.strip() and k not in {"type", "status"}
    ]
    if len(strings) == 1:
        return strings[0]

    return ""


RETRY_LIMIT = 3


async def generate_reply(
    prompt: str,
) -> tuple[str, dict]:
    """调用主/备 AI 生成 JSON 格式回复，自动重试与降级"""
    config = get_config()

    messages = []
    if config.system_prompt:
        messages.append({"role": "system", "content": config.system_prompt})
    messages.append({"role": "user", "content": prompt})

    providers = [(config.API_PROTOCOL, config.API_BASE_URL, config.API_KEY, config.API_MODEL)]
    if config.BACK_API_KEY and config.BACK_API_MODEL and config.BACK_API_BASE_URL:
        providers.append((config.BACK_API_PROTOCOL, config.BACK_API_BASE_URL, config.BACK_API_KEY, config.BACK_API_MODEL))

    for protocol, base_url, api_key, model in providers:
        for retry in range(RETRY_LIMIT):
            try:
                logger.debug(f"AI call attempt: protocol={protocol}, base_url={base_url}, model={model} (retry {retry + 1}/{RETRY_LIMIT})")
                # reasoning 会先输出大段思维链，延迟通常增加 3~6 倍。
                # 默认关闭以保证聊天响应速度，可用 AI_REASONING=true 开启。
                use_reasoning = (
                    protocol.upper() == "OPENAI"
                    and os.environ.get("AI_REASONING", "false").strip().lower()
                    in ("1", "true", "yes", "on")
                )
                result = await chat(
                    protocol=protocol,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    response_format="json_object",
                    reasoning=use_reasoning,
                    timeout=30,
                )
                content = result.get("content", "")
                if not content:
                    logger.warning(f"Empty response from {protocol}, retrying...")
                    continue

                reasoning = result.get("reasoning")
                if reasoning:
                    logger.debug(f"Reasoning from {protocol}:\n{reasoning}")

                logger.debug(f"Raw AI response from {protocol}:\n{content}")

                content = _clean_json(content)
                parsed = json.loads(content)
                reply_text = _extract_reply(parsed)
                if reply_text:
                    parsed["reply"] = reply_text
                    return reply_text, parsed
                if "reply" in parsed:
                    return "", parsed
                logger.warning(f"Empty reply field from {protocol}, retrying...")
                continue
            except json.JSONDecodeError as e:
                logger.warning(f"JSON decode error from {protocol}: {e}")
                logger.warning(f"Raw content that failed to parse:\n{content}")
                continue
            except Exception as e:
                logger.warning(f"AI call failed ({protocol}): {e}, retrying...")
                continue

    return "", {}
