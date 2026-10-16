"""思维链档位回归：默认值、.env 校验、chat() 传参、三档映射。
独立运行：python tests/test_reasoning_effort.py（无需 pytest）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from astrabot.config import Config
from astrabot.chat_service import ai_client

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok   - {msg}")
    else:
        print(f"  FAIL - {msg}")
        failures.append(msg)


# Config.load() 会先校验这几项，缺任意一项直接抛 ValueError
REQUIRED_TEXT = {
    "name": "testbot", "name_cn": "测试", "person_setting": "s",
    "output_style": "o", "reply_style": "r", "extra_style": "e",
    "image_analyzer": "i", "API_PROTOCOL": "OPENAI",
    "API_BASE_URL": "https://x/v1", "API_KEY": "k", "API_MODEL": "m",
}


def test_defaults():
    print("[Config 默认值]")
    c = Config()
    check(c.reasoning_effort == "low", "第一档默认 low（智谱不传就会思考）")
    check(c.back_reasoning_effort == "low", "第二档默认 low")
    check(c.third_reasoning_effort == "", "第三档默认留空（DeepSeek 传了反而开启思考）")


def test_env_validation():
    print("[.env 档位校验]")
    saved = dict(os.environ)
    try:
        os.environ.update(REQUIRED_TEXT)
        os.environ["reasoning_effort"] = "MAX"
        check(Config.load().reasoning_effort == "max", "大小写归一 MAX -> max")
        os.environ["reasoning_effort"] = "ultra"
        check(Config.load().reasoning_effort == "low", "非法档位回退默认 low")
        os.environ["reasoning_effort"] = ""
        check(Config.load().reasoning_effort == "", "显式空值 = 不传该参数")
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_chat_kwargs():
    print("[chat() 只在有档位时才传参]")
    captured = {}

    class _Msg:
        content = '{"reply":"hi"}'

    class _Resp:
        choices = [type("C", (), {"message": _Msg()})()]
        usage = None

    class _Client:
        class _ChatNS:
            class _Comp:
                async def create(self, **kwargs):
                    captured.update(kwargs)
                    return _Resp()
            completions = _Comp()
        chat = _ChatNS()

    orig_client = ai_client._create_openai_client
    orig_record = ai_client.usage_tracker.record
    ai_client._create_openai_client = lambda **kw: _Client()
    ai_client.usage_tracker.record = lambda **kw: None
    try:
        async def call(effort):
            captured.clear()
            await ai_client.chat(
                protocol="OPENAI", base_url="https://x/v1", api_key="k",
                model="m", messages=[{"role": "user", "content": "x"}],
                reasoning_effort=effort,
            )
            return dict(captured)

        check(asyncio.run(call("low")).get("reasoning_effort") == "low", "low 时透传")
        check("reasoning_effort" not in asyncio.run(call(None)), "None 时完全不传")
        check("reasoning_effort" not in asyncio.run(call("")), "空字符串也不传")
    finally:
        ai_client._create_openai_client = orig_client
        ai_client.usage_tracker.record = orig_record


def test_tier_mapping():
    print("[generate_reply 分档映射]")
    calls = []

    async def fake_chat(**kw):
        calls.append((kw["model"], kw.get("reasoning_effort")))
        if kw["model"] == "glm-5.3-flash":
            return {"content": "", "reasoning": None}
        return {"content": '{"reply":"兜底"}', "reasoning": None}

    cfg = Config()
    cfg.API_PROTOCOL = cfg.BACK_API_PROTOCOL = cfg.THIRD_API_PROTOCOL = "OPENAI"
    cfg.API_BASE_URL, cfg.API_KEY, cfg.API_MODEL = "https://a/v1", "k", "glm-5.3-flash"
    cfg.BACK_API_BASE_URL, cfg.BACK_API_KEY, cfg.BACK_API_MODEL = "https://b/v1", "k", "glm-5.3-flashx"
    cfg.THIRD_API_BASE_URL, cfg.THIRD_API_KEY, cfg.THIRD_API_MODEL = "https://c/v1", "k", "deepseek-chat"

    orig_chat, orig_cfg = ai_client.chat, ai_client.get_config
    ai_client.chat, ai_client.get_config = fake_chat, (lambda: cfg)
    try:
        reply, _ = asyncio.run(ai_client.generate_reply("prompt"))
        efforts = dict(calls)
        check(reply == "兜底", "首档空回复时逐档下探")
        check(efforts.get("glm-5.3-flash") == "low", "第一档带 low")
        check(efforts.get("glm-5.3-flashx") == "low", "第二档带 low")
        check(efforts.get("deepseek-chat") is None, "第三档不带 reasoning_effort")
    finally:
        ai_client.chat, ai_client.get_config = orig_chat, orig_cfg


if __name__ == "__main__":
    test_defaults()
    test_env_validation()
    test_chat_kwargs()
    test_tier_mapping()
    print(f"\n{'全部通过' if not failures else f'{len(failures)} 项失败'}")
    sys.exit(1 if failures else 0)
