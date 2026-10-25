"""AI 客户端：统一封装 chat 与图片分析调用"""

from __future__ import annotations

import base64
import json

import httpx
from openai import AsyncOpenAI

from astrabot.chat_service import usage_tracker
from astrabot.chat_service.http_client import get_http_client
from astrabot.chat_service.image_utils import detect_mime
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


def _create_anthropic_client(base_url: str, api_key: str, http_client: httpx.AsyncClient):
    """惰性导入 anthropic：它顶层依赖原生库 jiter，被 Windows 智能应用控制拦过一次。

    聊天三档都走 OPENAI 协议时，不该因为一个用不上的 SDK 让整个插件导入失败、
    bot 起不来；真要用 ANTHROPIC 档却导不进来，就抛能照做的错误，不静默退化。
    """
    try:
        import anthropic
    except Exception as e:
        raise RuntimeError(
            "ANTHROPIC protocol unavailable: `anthropic` failed to import "
            f"({e}). 若提示『应用程序控制策略已阻止此文件』，是被 Smart App Control 拦了 "
            "它依赖的 jiter 原生库——换一个 jiter 版本或关闭智能应用控制即可。"
        ) from e
    return anthropic.AsyncAnthropic(base_url=base_url, api_key=api_key, http_client=http_client)


async def chat(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    response_format: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    timeout: int = 30,
    usage_kind: str = "chat",
) -> dict:
    protocol_upper = protocol.upper()

    if protocol_upper == "OPENAI":
        client = _create_openai_client(
            base_url=base_url, api_key=api_key, http_client=_get_httpx_client()
        )
        kwargs = {
            "model": model,
            "messages": messages,
            "timeout": timeout,
        }
        if response_format == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        response = await client.chat.completions.create(**kwargs)
        msg = response.choices[0].message
        content = msg.content or ""
        reasoning_content = getattr(msg, "reasoning_content", None)
        usage = getattr(response, "usage", None)
        usage_tracker.record(
            kind=usage_kind,
            model=model,
            usage=usage.model_dump() if usage is not None else None,
        )
        return {"content": content, "reasoning": reasoning_content}

    if protocol_upper == "ANTHROPIC":
        client = _create_anthropic_client(
            base_url=base_url, api_key=api_key, http_client=_get_httpx_client()
        )
        anthropic_messages = [m for m in messages if m["role"] != "system"]
        system_content = next((m["content"] for m in messages if m["role"] == "system"), None)
        kwargs = {
            "model": model,
            "max_tokens": 4096,
            "messages": anthropic_messages,
            "timeout": timeout,
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
                kind=usage_kind,
                model=model,
                usage={
                    "prompt_tokens": getattr(anth_usage, "input_tokens", 0),
                    "completion_tokens": getattr(anth_usage, "output_tokens", 0),
                },
            )
        return {"content": content}

    raise ValueError(f"Unknown protocol: {protocol}")


def _read_dimensions(raw: bytes) -> tuple[int, int]:
    """从文件头读出宽高，失败返回 (0, 0)。

    只解析头部，不依赖 Pillow，避免再引入可能被系统策略拦截的原生库。
    """
    try:
        if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24:
            return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            # VP8X 扩展格式
            if raw[12:16] == b"VP8X" and len(raw) >= 30:
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return w, h
        if raw.startswith(b"GIF"):
            if len(raw) >= 10:
                return (
                    int.from_bytes(raw[6:8], "little"),
                    int.from_bytes(raw[8:10], "little"),
                )
        if raw.startswith(b"\xff\xd8"):
            # JPEG 需要逐段扫描 SOF 标记
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    return (
                        int.from_bytes(raw[i + 7 : i + 9], "big"),
                        int.from_bytes(raw[i + 5 : i + 7], "big"),
                    )
                if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(raw[i + 2 : i + 4], "big")
                i += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def _needs_high_detail(b64: str) -> bool:
    """判断这张图是否值得用 high 精度识别。

    经验规则：
    - 尺寸大（可能含大量细节/文字）-> high
    - 长宽比悬殊（长截图/聊天记录）-> high
    - 双轴都较小（表情包、头像）-> low，省 token
    """
    import base64 as _b64

    try:
        raw = _b64.b64decode(b64, validate=False)
    except Exception:
        return False
    if not raw:
        return False

    # 体积大通常是截图或高清图，值得看清
    if len(raw) > 400 * 1024:
        return True

    w, h = _read_dimensions(raw)
    if not w or not h:
        return False

    ratio = max(w, h) / max(1, min(w, h))
    long_side = max(w, h)

    # 长截图 / 聊天记录截图
    if ratio >= 2.0:
        return True

    # 高分辨率图：细节多，low 会被缩到 512 而丢信息
    if long_side >= 800:
        return True

    # 尺寸小且接近方形：表情包、头像，low 足够
    if long_side <= 400 and ratio <= 1.5:
        return False

    return False


def _vision_chain(config) -> list[tuple[str, str, str, str, str]]:
    """识图档位：主视觉 → 辅助视觉，返回 (protocol, base_url, api_key, model, reasoning_effort)。

    与聊天用的 _provider_chain 刻意分开：能不能识图取决于模型是否多模态，跟聊天档位的
    主/备/三顺序无关，共用一条链会把识图悄悄钉到一个不支持图片的模型上。
    """
    tiers: list[tuple[str, str, str, str, str]] = []

    def add(base_url: str, api_key: str, model: str, effort: str) -> None:
        key = api_key or config.API_KEY
        # 同端点同模型只留一档：未配辅助档时不能把主聊天 API 再打一遍
        if base_url and key and model and (base_url, key, model) not in [t[1:4] for t in tiers]:
            tiers.append(("OPENAI", base_url, key, model, effort))

    provider = (config.VISUAL_API_PROVIDER or "").strip().upper()
    if provider not in ("", "MAIN", "DEEPSEEK") and config.VISUAL_API_KEY:
        # 独立视觉服务只认它自带的 Key（旧行为）
        add(API_BASE_URLS.get(provider, API_BASE_URLS["SILICONFLOW"]), config.VISUAL_API_KEY,
            config.VISUAL_API_MODEL, config.vision_reasoning_effort)
    else:
        # 默认复用主聊天 API；单独配置 VISUAL_API_* 时优先，以便识图钉在多模态端点
        add(config.VISUAL_API_BASE_URL or config.API_BASE_URL, config.VISUAL_API_KEY,
            config.VISUAL_API_MODEL or config.API_MODEL, config.vision_reasoning_effort)

    # 辅助档必须显式给出端点和模型，否则会顺着聊天档位漂到不支持图片的模型上
    add(config.VISUAL_BACK_API_BASE_URL, config.VISUAL_BACK_API_KEY,
        config.VISUAL_BACK_API_MODEL, config.vision_back_reasoning_effort)
    return tiers


async def analyze_images(
    images_base64: list[str],
    prompt: str,
    force_detail: str | None = None,
) -> dict:
    """识图：按 主视觉档 → 辅助视觉档 依次尝试，失败返回空 dict。

    每档只试一次：识图是在回复链路上同步等待的，同一档重试只会把人晾在更长的超时里，
    不如直接换下一档（限流/超时/返回非 JSON 都算这一档失败）。思维链按档透传，
    所以主力换成智谱 glm 后不会每次识图都白烧 reasoning token。

    detail 默认沿用配置，但会按图片特征自动升级：截图、含文字、
    尺寸偏小或长宽比异常的图用 high，能显著提升 OCR 与细节准确度，
    普通表情包仍用 low 以省 token。
    """
    config = get_config()

    tiers = _vision_chain(config)
    if not tiers:
        logger.info("Image analysis skipped: no usable vision API configured")
        return {}

    # 按图片内容决定精度：配置为 auto 时启用自动判断
    detail = config.vision_detail
    if force_detail:
        detail = force_detail
    elif detail == "auto":
        detail = "high" if any(_needs_high_detail(b) for b in images_base64) else "low"

    # 单次请求的图片数量上限，避免群内刷图时一次性把成本放大
    max_images = max(1, config.vision_max_images)
    if len(images_base64) > max_images:
        logger.info(
            f"Image analysis: {len(images_base64)} images provided, "
            f"only analyzing the first {max_images}"
        )
        images_base64 = images_base64[:max_images]

    content_parts = [{"type": "text", "text": prompt}]
    for b64 in images_base64:
        try:
            raw = base64.b64decode(b64, validate=False)
        except Exception:
            raw = b""
        # 按真实格式构造 data URI：此前一律写 image/jpeg，
        # 而群里常见 GIF/PNG，格式声明错误会影响识图质量。
        mime = detect_mime(raw) if raw else "image/jpeg"
        content_parts.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{b64}",
                "detail": detail,
            },
        })

    messages = [{"role": "user", "content": content_parts}]

    for protocol, base_url, api_key, model, effort in tiers:
        try:
            result = await chat(
                protocol=protocol,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                response_format="json_object",
                reasoning_effort=effort or None,
                timeout=60,
                usage_kind="vision",
            )
        except Exception as e:
            logger.warning(f"Image analysis failed on {model}: {e}")
            continue

        content = (result.get("content") or "").strip()
        if not content:
            logger.warning(f"Empty vision response from {model}, trying next tier")
            continue
        try:
            return json.loads(_clean_json(content))
        except json.JSONDecodeError as e:
            logger.warning(f"Non-JSON vision response from {model}, trying next tier: {e}")
    return {}


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


def _is_retryable_error(exc: Exception) -> bool:
    """区分可重试与不可重试错误，避免对鉴权/请求错误反复浪费调用。

    带 HTTP 状态码的 API 错误：仅 429（限流）与 5xx（服务端）值得重试；
    4xx（400/401/403/404/422 等）重试没有意义。无状态码的多为网络/超时类，
    属于可重试。
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return True


def _provider_chain(config) -> list[tuple[str, str, str, str, str]]:
    """按 主→备→三 顺序返回可用档位：(protocol, base_url, api_key, model, reasoning_effort)。"""
    providers = [(config.API_PROTOCOL, config.API_BASE_URL, config.API_KEY, config.API_MODEL,
                  config.reasoning_effort)]
    if config.BACK_API_KEY and config.BACK_API_MODEL and config.BACK_API_BASE_URL:
        providers.append((config.BACK_API_PROTOCOL, config.BACK_API_BASE_URL, config.BACK_API_KEY, config.BACK_API_MODEL,
                          config.back_reasoning_effort))
    if config.THIRD_API_KEY and config.THIRD_API_MODEL and config.THIRD_API_BASE_URL:
        providers.append((config.THIRD_API_PROTOCOL, config.THIRD_API_BASE_URL, config.THIRD_API_KEY, config.THIRD_API_MODEL,
                          config.third_reasoning_effort))
    return providers


async def generate_summary(prompt: str, max_tokens: int) -> str:
    """生成前情摘要（纯文本）。失败返回空串，由调用方保持水位线不变、下轮再试。

    摘要走与回复相同的回退链，但它是后台任务，所以每档只试一次：某档失败就换
    下一档，全挂了就这轮不压缩，绝不拖慢或影响任何正在进行的回复。
    """
    config = get_config()
    messages = [{"role": "user", "content": prompt}]

    for protocol, base_url, api_key, model, effort in _provider_chain(config):
        try:
            result = await chat(
                protocol=protocol,
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                reasoning_effort=effort or None,
                max_tokens=max_tokens,
                timeout=60,
                usage_kind="summary",
            )
            content = (result.get("content") or "").strip()
            if content:
                return content
            logger.warning(f"Empty summary response from {model}, trying next provider")
        except Exception as e:
            logger.warning(f"Summary call failed on {model}: {e}")
    return ""


async def generate_reply(
    prompt: str,
) -> tuple[str, dict]:
    """调用主/备 AI 生成 JSON 格式回复，自动重试与降级"""
    config = get_config()

    messages = []
    if config.system_prompt:
        messages.append({"role": "system", "content": config.system_prompt})
    messages.append({"role": "user", "content": prompt})

    providers = _provider_chain(config)

    for protocol, base_url, api_key, model, effort in providers:
        for retry in range(RETRY_LIMIT):
            try:
                logger.debug(f"AI call attempt: protocol={protocol}, base_url={base_url}, model={model} (retry {retry + 1}/{RETRY_LIMIT})")
                # 按档透传思维链档位：智谱靠 low 把 reasoning 压到 0，DeepSeek 留空不传
                result = await chat(
                    protocol=protocol,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    response_format="json_object",
                    reasoning_effort=effort or None,
                    max_tokens=config.chat_max_tokens,
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
                if _is_retryable_error(e):
                    logger.warning(f"AI call failed ({protocol}): {e}, retrying...")
                    continue
                # 不可重试错误（如鉴权/请求非法）：本 provider 直接放弃，换下一个
                logger.error(f"AI call aborted ({protocol}, non-retryable): {e}")
                break

    return "", {}
