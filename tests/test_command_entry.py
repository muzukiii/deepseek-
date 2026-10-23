"""指令入口回归测试：@机器人 + 指令必须能真正落到命令处理器。

起因：`event.get_plaintext()` 走的是 `extract_plain_text()`，它会整段剥掉 at
但保留后面的空格，于是 "@bot /用量" 的 plain_text 是 " /用量"，
`startswith("/")` 判定为假 → 指令掉进 AI 流程变成一句闲聊，从未生效。

纯本地，不发消息、不联网、不碰数据库。
无 pytest 时可 `python tests/test_command_entry.py` 直跑。"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from astrabot.chat_service.memory_commands import (
    handle_memory_command,
    is_destructive_command,
)

_HERE = Path(__file__).resolve().parent
SOURCE = _HERE.parent / "astrabot" / "chat_service" / "message_handler.py"


def _plaintext_of_at_command(cmd: str) -> str:
    """复刻 nonebot 对 "@机器人 <cmd>" 的纯文本提取结果。"""
    return Message([MessageSegment.at("12345"), MessageSegment.text(cmd)]).extract_plain_text()


def test_at_prefix_leaves_leading_space():
    # 钉住成因本身：剥掉 at 段后确实剩下前导空格，未 strip 的判定必然为假。
    raw = _plaintext_of_at_command(" /用量")
    assert raw == " /用量", repr(raw)
    assert not raw.startswith("/")
    assert raw.strip().startswith("/")


def test_gate_uses_stripped_text():
    # 入口必须先 strip 再判定，并且把 strip 后的文本传给处理器。
    src = SOURCE.read_text(encoding="utf-8")
    assert "cmd_text = plain_text.strip()" in src, "命令入口未做 strip"
    assert 'if cmd_text.startswith("/"):' in src, "命令入口仍用未 strip 的文本判定"
    assert "_run_command(bot, group_id, user_id, cmd_text, config)" in src, "传给处理器的仍是原文"


def test_command_recognized_after_strip():
    # 用 strip 后的文本调用，确实命中已知指令（/memory help 不碰 DB）。
    handled, reply = handle_memory_command(
        1, "2", _plaintext_of_at_command(" /memory help").strip()
    )
    assert handled and "记忆管理命令" in reply, reply

    handled, reply = handle_memory_command(1, "2", "/记忆帮助")
    assert handled and "记忆管理命令" in reply, reply


def test_destructive_still_detected_after_strip():
    # fail-safe 判定同样依赖 strip 后的规范文本。
    assert is_destructive_command(_plaintext_of_at_command(" /memory clear confirm").strip())
    assert not is_destructive_command("/memory clear")


if __name__ == "__main__":
    test_at_prefix_leaves_leading_space()
    test_gate_uses_stripped_text()
    test_command_recognized_after_strip()
    test_destructive_still_detected_after_strip()
    print("ALL COMMAND ENTRY TESTS PASSED")
