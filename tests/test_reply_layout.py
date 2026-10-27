"""回复排版回归测试：只在句末换行、过长自动分行、行数上限、提示词规矩。

背景：模型用 [/] 标换行，但常在句中乱断，一条回复被劈成几行碎句（
"我觉得这个方案 / 不太行"）。format_reply_lines 做确定性兜底，提示词里
另立规矩。纯打桩，不联网。
无 pytest 时 `python tests/test_reply_layout.py` 直跑。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from astrabot.chat_service.prompt_builder import build_main_prompt, format_reply_lines


def run() -> None:
    # 1) 句中折断 → 合并回一行
    out = format_reply_lines("我觉得这个方案[/]不太行，因为成本太高")
    assert out == "我觉得这个方案不太行，因为成本太高", repr(out)
    print("PASS 句中折断被合并")

    # 2) 句末换行 → 保留
    out = format_reply_lines("今天天气不错。[/]我们去公园吧")
    assert out.split("\n") == ["今天天气不错。", "我们去公园吧"], repr(out)
    print("PASS 句末换行被保留")

    # 3) 颜文字结尾也算结句
    out = format_reply_lines("好耶(/ω＼)[/]那我们出发吧")
    assert len(out.split("\n")) == 2, repr(out)
    print("PASS 颜文字结尾不误合并")

    # 3b) 语气词收尾也算结句（闲聊里很少打标点）
    out = format_reply_lines("兰山，是兰山才对嘛[/]不过说真的图太糊了。")
    assert len(out.split("\n")) == 2, repr(out)
    # 3c) 真正的句中折断（上一行停在名词上）仍要合并
    out = format_reply_lines("这个方案的成本太高了，我建议[/]先做小规模试验。")
    assert out.count("\n") == 0, repr(out)
    print("PASS 语气词结尾保留换行、名词处折断仍合并")

    # 4) 空段丢弃
    out = format_reply_lines("第一句。[/][/]\n\n第二句。")
    assert out.split("\n") == ["第一句。", "第二句。"], repr(out)
    print("PASS 空段被丢弃")

    # 5) 行数上限：多余的行并到最后一行
    many = "。[/]".join(f"第{i}句" for i in range(1, 7)) + "。"
    out = format_reply_lines(many, max_lines=3)
    lines = out.split("\n")
    assert len(lines) == 3, lines
    assert lines[-1].endswith("第6句。") and "第3句" in lines[-1], lines
    print("PASS 超过行数上限时并到末行")

    # 6) 长单行自动按句号分行（阈值 80 字，这里 4 句共 104 字）
    long_one = "第一句话讲的是这件事的来龙去脉比较长需要拆开才行。" * 4
    assert len(long_one) > 80
    out = format_reply_lines(long_one, max_lines=3)
    lines = out.split("\n")
    assert 1 < len(lines) <= 3, lines
    assert "".join(lines) == long_one, "分行不能丢字"
    print("PASS 过长单行按句末标点拆成多行且不丢字")

    # 7) 短单行不拆
    short = "对呀对呀，就是这个意思嘛～"
    assert format_reply_lines(short) == short, "短句不该被拆"
    print("PASS 短句保持一行")

    # 8) 真实换行与 [/] 等价
    assert format_reply_lines("第一句。\n第二句。").split("\n") == ["第一句。", "第二句。"]
    print("PASS 真实换行与 [/] 行为一致")

    # 9) 空输入
    assert format_reply_lines("") == ""
    assert format_reply_lines("[/][/]") == ""
    print("PASS 空输入返回空串")

    # 10) 提示词里带着排版规矩
    cfg = types.SimpleNamespace(
        name="Astra", name_cn="阿斯特拉", person_setting="测试人设",
        output_style="输出 JSON", reply_style="自然", extra_style="无",
    )
    prompt = build_main_prompt(cfg, "", "聊天记录", "触发行")
    assert "【排版】分行只分在句子之间" in prompt, "排版规矩应进 prompt"
    assert "绝对不要在句子中间" in prompt
    print("PASS 排版规矩已注入 prompt")


def main() -> int:
    failures = 0
    try:
        run()
    except AssertionError as e:
        failures += 1
        print(f"FAIL {e}")
    except Exception as e:
        failures += 1
        import traceback

        print(f"FAIL unexpected: {type(e).__name__}: {e}")
        traceback.print_exc()
    print("1/1 passed" if not failures else "0/1 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
