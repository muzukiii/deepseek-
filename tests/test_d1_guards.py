"""D-1 回归：/memory 权限识别、纯文本记住可达、记忆截断、去重归一。
独立运行：python tests/test_d1_guards.py（无需 pytest）
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from astrabot.chat_service import enhanced_memory as enh
from astrabot.chat_service import memory_commands as mc
from astrabot.chat_service import prompt_builder as pb

failures = []


def check(cond, msg):
    if cond:
        print(f"  ok   - {msg}")
    else:
        print(f"  FAIL - {msg}")
        failures.append(msg)


def test_destructive_detection():
    print("[is_destructive_command]")
    check(mc.is_destructive_command("/memory delete 12"), "/memory delete 判定破坏性")
    check(mc.is_destructive_command("/删除记忆 3"), "/删除记忆 判定破坏性")
    check(mc.is_destructive_command("/memory clear confirm"), "clear confirm 判定破坏性")
    check(not mc.is_destructive_command("/memory clear"), "clear（未 confirm）非破坏性")
    check(not mc.is_destructive_command("/memory list"), "list 非破坏性")
    check(not mc.is_destructive_command("/用量"), "用量 非破坏性")
    check(not mc.is_destructive_command("记住：喜欢猫"), "记住 非破坏性")


def test_implicit_reachability():
    print("[check_implicit_memory_request]")
    check(mc.check_implicit_memory_request("记住：我喜欢吃辣") == "我喜欢吃辣", "记住： 提取")
    check(mc.check_implicit_memory_request("帮我记住，周三开会") == "周三开会", "帮我记住， 提取")
    check(mc.check_implicit_memory_request("请记住 密码是123") == "密码是123", "请记住+空格 提取")
    check(mc.check_implicit_memory_request("今天天气不错") is None, "普通消息不误判")


def test_char_cap():
    print("[prompt_builder 截断]")
    check(pb._cap("abc", 10) == "abc", "未超限原样返回")
    check(pb._cap("", 100) == "", "空串不变")
    capped = pb._cap("字" * 3000, 2000)
    check(len(capped) < 3000 and capped.endswith("已截断）"), "超限截断并加标记")

    class _Cfg:
        name = "Astra"
        name_cn = "阿斯特拉"
        person_setting = "测试"
        output_style = "o"
        reply_style = "r"
        extra_style = "e"

    huge = "L" * 5000
    out = pb.build_main_prompt(
        _Cfg(), plugin_section="", formatted_history="H", trigger_marker="T",
        long_term_memory=huge, relevant_facts=huge, personal_memory_section=huge,
    )
    check("L" * 2001 not in out, "长记忆块被截断到上限内")
    check(out.count("已截断）") >= 3, "三个长记忆块都截断")


def test_normalize_dedup():
    print("[归一化去重]")
    check(enh._normalize("喜欢  吃\t辣\n辣") == "喜欢 吃 辣 辣", "连续空白压成单空格")

    tmp = Path(tempfile.mkdtemp(prefix="d1mem_"))
    enh.DATA_DIR = tmp
    enh.MEMORY_DB = tmp / "memories.db"
    if hasattr(enh._local, "conn"):
        try:
            enh._local.conn.close()
        except Exception:
            pass
        del enh._local.conn

    check(enh.add_fact(1, "A  B C") is True, "首条事实入库")
    check(enh.add_fact(1, "A B  C") is False, "仅空白差异的事实去重")
    check(enh.add_fact(1, "A B C D") is True, "内容不同的事实独立入库")

    conn = sqlite3.connect(str(enh.MEMORY_DB))
    rows = {r[0] for r in conn.execute("SELECT fact FROM facts WHERE group_id=1")}
    conn.close()
    check(rows == {"A B C", "A B C D"}, f"facts 仅保留归一后的 2 条，实得 {rows}")
    check(
        enh.add_long_term_memory(1, "X   Y") is True
        and enh.add_long_term_memory(1, "X Y") is False,
        "长期记忆仅空白差异也去重",
    )


if __name__ == "__main__":
    test_destructive_detection()
    test_implicit_reachability()
    test_char_cap()
    test_normalize_dedup()
    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        sys.exit(1)
    print("ALL D1 REGRESSION TESTS PASSED")
