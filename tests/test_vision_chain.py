"""识图档位链回归测试：主视觉→辅助视觉的顺序、按档思维链、逐级回退。

背景：analyze_images 以前只有一个端点、没有任何回退，识图失败就静默返回 {}。
现在改成按档尝试，所以必须钉住三件事：档位顺序、每档只试一次、
每档透传自己的 reasoning_effort（智谱不传就白烧思维链，DeepSeek 传了反而开始思考）。
纯打桩，不联网、不调 LLM。无 pytest 时可 `python tests/test_vision_chain.py` 直跑。"""

import asyncio
import base64
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import ai_client as ac

ZHIPU = "https://open.bigmodel.cn/api/paas/v4"
DS = "https://api.deepseek.com/v1"
SF = "https://api.siliconflow.cn/v1"
GIF_B64 = base64.b64encode(b"GIF89a" + b"\x00" * 40).decode()


def cfg(**over) -> SimpleNamespace:
    """默认值对齐 config.py：主力智谱 glm，辅助 DeepSeek。"""
    base = dict(
        API_PROTOCOL="OPENAI",
        API_BASE_URL=ZHIPU,
        API_KEY="k-chat",
        API_MODEL="glm-5.3-flash",
        VISUAL_API_PROVIDER="MAIN",
        VISUAL_API_BASE_URL=ZHIPU,
        VISUAL_API_KEY="",
        VISUAL_API_MODEL="glm-5.3-flash",
        VISUAL_BACK_API_BASE_URL=DS,
        VISUAL_BACK_API_KEY="k-ds",
        VISUAL_BACK_API_MODEL="deepseek-chat",
        vision_reasoning_effort="low",
        vision_back_reasoning_effort="",
        vision_detail="low",
        vision_max_images=3,
    )
    base.update(over)
    return SimpleNamespace(**base)


def patch(config, replies):
    """把 get_config 与 chat 换成桩；replies 是每档的返回值或异常。"""
    calls: list[dict] = []
    saved = (ac.get_config, ac.chat)

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        out = replies[len(calls) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    ac.get_config = lambda: config
    ac.chat = fake_chat

    def restore():
        ac.get_config, ac.chat = saved

    return calls, restore


def test_chain_order_and_dedup():
    tiers = ac._vision_chain(cfg())
    assert [t[3] for t in tiers] == ["glm-5.3-flash", "deepseek-chat"], tiers
    assert [t[1] for t in tiers] == [ZHIPU, DS], tiers

    # 未配辅助档：不能把主聊天 API 再排一遍，否则同端点同模型白打两次
    one = ac._vision_chain(cfg(VISUAL_BACK_API_BASE_URL="", VISUAL_BACK_API_KEY="", VISUAL_BACK_API_MODEL=""))
    assert len(one) == 1, one
    # 端点相同但模型不同时仍要保留两档（换模型正是回退的意义）
    two = ac._vision_chain(cfg(VISUAL_BACK_API_MODEL="glm-5.3-flashx"))
    assert [t[3] for t in two] == ["glm-5.3-flash", "glm-5.3-flashx"], two


def test_external_provider_keeps_its_own_key():
    tiers = ac._vision_chain(cfg(VISUAL_API_PROVIDER="SILICONFLOW", VISUAL_API_KEY="k-sf"))
    assert tiers[0][1] == SF, tiers
    assert tiers[0][2] == "k-sf", tiers

    # 独立服务没配 Key 时退回主聊天 API（旧行为），不能拿聊天 Key 去敲第三方
    back = ac._vision_chain(cfg(VISUAL_API_PROVIDER="SILICONFLOW", VISUAL_API_KEY=""))
    assert back[0][1] == ZHIPU, back
    assert back[0][2] == "k-chat", back


def test_no_vision_api_configured():
    bare = cfg(
        API_KEY="",
        VISUAL_API_BASE_URL="",
        VISUAL_API_MODEL="",
        VISUAL_BACK_API_BASE_URL="",
        VISUAL_BACK_API_KEY="",
        VISUAL_BACK_API_MODEL="",
    )
    assert ac._vision_chain(bare) == []
    # 主视觉档缺 Key 时，自带 Key 的辅助档仍然可用——识图不该整条链一起哑掉
    only_back = ac._vision_chain(cfg(API_KEY=""))
    assert [t[3] for t in only_back] == ["deepseek-chat"], only_back


def test_per_tier_reasoning_effort():
    calls, restore = patch(
        cfg(), [{"content": '{"image_1": "ok"}'}, {"content": '{"image_1": "fallback"}'}]
    )
    try:
        out = asyncio.run(ac.analyze_images([GIF_B64], "描述这张图"))
        assert out == {"image_1": "ok"}, out
    finally:
        restore()
    assert len(calls) == 1, calls
    assert calls[0]["reasoning_effort"] == "low", calls[0]
    assert calls[0]["model"] == "glm-5.3-flash", calls[0]
    assert calls[0]["usage_kind"] == "vision", calls[0]
    assert calls[0]["response_format"] == "json_object", calls[0]
    assert calls[0]["timeout"] == 60, calls[0]


def test_failover_on_exception():
    calls, restore = patch(
        cfg(),
        [RuntimeError("429 rate limited"), {"content": '{"image_1": "来自 DeepSeek"}'}],
    )
    try:
        out = asyncio.run(ac.analyze_images([GIF_B64], "描述这张图"))
    finally:
        restore()
    assert out == {"image_1": "来自 DeepSeek"}, out
    assert [c["model"] for c in calls] == ["glm-5.3-flash", "deepseek-chat"], calls
    # 辅助档不传该参数：deepseek-chat 一旦收到 reasoning_effort 反而开启思考
    assert calls[1]["reasoning_effort"] is None, calls[1]


def test_failover_on_empty_and_non_json():
    for bad in ({"content": ""}, {"content": "这张图是一只猫。"}):
        calls, restore = patch(cfg(), [bad, {"content": '{"image_1": "ok"}'}])
        try:
            out = asyncio.run(ac.analyze_images([GIF_B64], "描述这张图"))
        finally:
            restore()
        assert out == {"image_1": "ok"}, (bad, out)
        assert len(calls) == 2, (bad, calls)


def test_all_tiers_fail_returns_empty_not_raises():
    calls, restore = patch(cfg(), [RuntimeError("boom"), RuntimeError("boom2")])
    try:
        assert asyncio.run(ac.analyze_images([GIF_B64], "x")) == {}
    finally:
        restore()
    assert len(calls) == 2, calls


def test_markdown_wrapped_json_still_parsed():
    calls, restore = patch(cfg(), [{"content": "```json\n{\"image_1\": \"ok\"}\n```"}])
    try:
        assert asyncio.run(ac.analyze_images([GIF_B64], "x")) == {"image_1": "ok"}
    finally:
        restore()


def test_images_and_detail_are_built_once_for_all_tiers():
    calls, restore = patch(
        cfg(vision_max_images=2, vision_detail="auto"),
        [RuntimeError("x"), {"content": "{}"}],
    )
    imgs = [GIF_B64] * 5
    try:
        asyncio.run(ac.analyze_images(imgs, "描述"))
    finally:
        restore()
    assert len(calls) == 2, calls
    for c in calls:
        parts = c["messages"][0]["content"]
        # vision_max_images=2 时只带前两张 + 1 条文本
        assert len(parts) == 3, parts
        assert parts[0] == {"type": "text", "text": "描述"}, parts[0]
        assert parts[1]["image_url"]["url"].startswith("data:image/gif;base64,"), parts[1]
        # GIF89a + 全零：长宽 0，_needs_high_detail 判 False
        assert parts[1]["image_url"]["detail"] == "low", parts[1]


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            import traceback

            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
