"""连回上限白名单回归测试：QQ 集合解析 + 两处门控都认豁免名单。

背景：max_reply_streak 不分对象地把同一个人掐在 3 轮，主人自己想长聊也会被断，
而且触发的是防互刷冷却（默认 300s 不回话）。新增 streak_allowed_users 豁免名单后，
三个 QQ 集合配置（命令白名单 / 忽略名单 / 连回豁免）共用同一个解析器，
所以这里同时钉住重构后的解析行为，避免顺手改坏 command_allowed_users。
纯打桩，不联网、不调 LLM。无 pytest 时可 `python tests/test_streak_whitelist.py` 直跑。"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.chat_service import message_handler as mh
from astrabot.config import Config

OWNER = "2041718160"
REQUIRED = {
    "name": "Astra",
    "name_cn": "阿斯特拉",
    "person_setting": "测试人设",
    "output_style": "简洁",
    "reply_style": "自然",
    "extra_style": "无",
    "image_analyzer": "描述图片内容",
    "API_KEY": "sk-test",
    "API_MODEL": "glm-5.3-flash",
    "API_PROTOCOL": "OPENAI",
    "API_BASE_URL": "https://example.invalid/v1",
}


class env:
    """临时注入一组环境变量跑 Config.load()，退出时还原。"""

    def __init__(self, **over):
        self.over = dict(REQUIRED)
        self.over.update({k: str(v) for k, v in over.items()})
        self.saved = {}

    def __enter__(self):
        for k, v in self.over.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_streak_whitelist_bracket_form():
    with env(streak_allowed_users=f"[{OWNER}]"):
        cfg = Config.load()
    assert cfg.streak_allowed_users == {OWNER}, cfg.streak_allowed_users


def test_streak_whitelist_comma_form_and_bare_value():
    with env(streak_allowed_users=f"{OWNER},123456"):
        assert Config.load().streak_allowed_users == {OWNER, "123456"}
    with env(streak_allowed_users=OWNER):
        assert Config.load().streak_allowed_users == {OWNER}


def test_default_is_no_exemption():
    # 必须显式清空：跑在真仓库里时 .env 会把这些键带进 os.environ，
    # load_dotenv(override=False) 只保证不覆盖已有值，不会替我们抹掉它们。
    with env(streak_allowed_users="", command_allowed_users="", ignore_users=""):
        cfg = Config.load()
    assert cfg.streak_allowed_users == set(), cfg.streak_allowed_users
    # 留空 = 旧行为：谁都受 max_reply_streak 约束
    assert not mh._streak_exempt(cfg, OWNER)


def test_malformed_value_does_not_crash():
    with env(streak_allowed_users="[123"):
        cfg = Config.load()
    assert isinstance(cfg.streak_allowed_users, set)


def test_shared_parser_keeps_other_sets():
    with env(
        command_allowed_users=f"[{OWNER}, 123456]",
        ignore_users="777,888",
        streak_allowed_users=OWNER,
    ):
        cfg = Config.load()
    assert cfg.command_allowed_users == {OWNER, "123456"}, cfg.command_allowed_users
    assert cfg.ignore_users == {"777", "888"}, cfg.ignore_users
    assert cfg.streak_allowed_users == {OWNER}, cfg.streak_allowed_users


def test_predicate_coerces_int_user_id():
    cfg = SimpleNamespace(streak_allowed_users={OWNER})
    assert mh._streak_exempt(cfg, int(OWNER)), "OneBot 的 user_id 是 int，必须能命中"
    assert mh._streak_exempt(cfg, OWNER)
    assert not mh._streak_exempt(cfg, "10000")
    assert not mh._streak_exempt(SimpleNamespace(streak_allowed_users=set()), OWNER)


def test_both_gate_sites_honour_the_whitelist():
    src = open(mh.__file__, encoding="utf-8").read()
    guarded = src.count("config.max_reply_streak > 0 and not _streak_exempt(config, user_id)")
    bare = src.count("if config.max_reply_streak > 0:")
    assert guarded == 2, f"预检查与计数两处都要豁免，实得 {guarded}"
    assert bare == 0, f"不该留下没豁免判断的门控，实得 {bare}"


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
