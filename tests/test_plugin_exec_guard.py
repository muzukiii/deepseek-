"""Batch 5 插件 execute 加固的回归测试。

可在装有 pytest 的环境用 `pytest tests/`，也可在无 pytest 时直接
`python tests/test_plugin_exec_guard.py`（自带兜底 runner）。
"""
from __future__ import annotations

import inspect
import os
import sys
import types

# 让测试无论从何处运行都能 import astrabot
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrabot.config import Config  # noqa: E402
from astrabot.chat_service import message_handler, plugin_loader  # noqa: E402

PluginLoader = plugin_loader.PluginLoader
PluginInfo = plugin_loader.PluginInfo

_REQUIRED_ENV = {
    "name": "t", "name_cn": "t", "person_setting": "p",
    "output_style": "o", "reply_style": "r", "extra_style": "e",
    "image_analyzer": "a", "API_KEY": "k",
}


def _fake_plugin(name: str, desc: str = "描述") -> PluginInfo:
    return PluginInfo(name, types.ModuleType(name), {
        "function_format": "string",
        "function_desc": desc,
        "expose_to_ai": True,
    })


def test_config_defaults_fail_safe():
    cfg = Config()
    assert cfg.plugin_exec_enabled is False
    assert cfg.plugin_exec_max_rounds == 3
    assert cfg.plugin_exec_timeout == 30


def test_config_clamps_and_parses():
    saved = dict(os.environ)
    try:
        for key in list(os.environ):
            if key.lower() in {
                "plugin_exec_enabled", "plugin_exec_max_rounds",
                "plugin_exec_timeout", "name", "name_cn", "person_setting",
                "output_style", "reply_style", "extra_style",
                "image_analyzer", "api_key",
            }:
                del os.environ[key]
        os.environ.update(_REQUIRED_ENV)
        os.environ["plugin_exec_enabled"] = "true"
        os.environ["plugin_exec_max_rounds"] = "999"   # hi 上限 10
        os.environ["plugin_exec_timeout"] = "9999"     # hi 上限 300
        cfg = Config.load()
        assert cfg.plugin_exec_enabled is True
        assert cfg.plugin_exec_max_rounds == 10
        assert cfg.plugin_exec_timeout == 300

        os.environ["plugin_exec_enabled"] = "false"
        assert Config.load().plugin_exec_enabled is False
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_get_plugin_section_gating():
    saved = PluginLoader.plugins
    try:
        PluginLoader.plugins = []
        assert PluginLoader.get_plugin_section(enabled=True) == ""

        PluginLoader.plugins = [_fake_plugin("web_search", "联网搜索")]
        assert PluginLoader.get_plugin_section(enabled=False) == ""

        sec = PluginLoader.get_plugin_section(enabled=True)
        assert "web_search" in sec
        assert "docker_exec" not in sec
    finally:
        PluginLoader.plugins = saved


def test_execute_loop_guard_present():
    src = inspect.getsource(message_handler)
    assert "if config.plugin_exec_enabled:" in src
    assert "range(config.plugin_exec_max_rounds)" in src
    assert "asyncio.wait_for" in src
    assert "config.plugin_exec_timeout" in src
    assert "range(10)" not in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
