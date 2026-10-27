"""Prompt 构建器：拼接人设、记忆、历史、触发标记等各部分，生成完整的 AI 输入"""

from __future__ import annotations

import re
from typing import Any

from astrabot.config import Config


# 记忆注入字符上限：历史/记忆内容无截断会在记忆膨胀时把成本推高、
# 甚至挤爆上下文。这里给每类记忆块设一个足够宽裕但封顶的长度，
# 超限即截断，保证单条消息的 prompt 规模可控。
_LTM_CAP = 2000
_FACTS_CAP = 1500
_OLDMEM_CAP = 1000
_PERSONAL_CAP = 1500

# 记忆写法约束：output_style 只要求"必须包含 memorize 数组"，从没说该放什么，
# 结果模型把每轮发生的事都写成叙事句——一个群攒到 388 条，多是"谁说了什么"。
_MEMORIZE_GUIDE = (
    "【写记忆的规矩】memorize 数组只放以后还用得上、且不会很快过期的内容："
    "称呼与关系、偏好与禁忌、约定与规则、身份或长期设定。"
    "不要放：一次性的对话事件、玩笑与梗、谁先说了什么的过程叙述、"
    "以及从当前聊天一眼能看到的事。每条不超过 40 字、写成陈述句；"
    "已经记过的不要重复新增；没有值得记的就给空数组。"
)


def _cap(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（记忆内容过长，已截断）"


# 排版：模型的换行标记是 [/]，但它常在句中乱断，一条回复被劈成几行碎句。
# 提示词里先立规矩，发送前再用 format_reply_lines 做确定性兜底。
_LAYOUT_GUIDE = (
    "【排版】分行只分在句子之间：一行就是一句完整的话，句末带 。！？～ 之类；"
    "绝对不要在句子中间、词与词之间换行。多句话时每句一行；"
    "一句话就能说完时就别换行。整条回复最多 3 行。"
)

_SENT_END = "。！？!?…～"
_CLOSERS = "”\"’'）)」』】》〉"
_EMOJI_END = "✧☆♪★"
# 中文闲聊常以语气词收尾而没有标点（"是兰山才对嘛"），这些也算一句话说完，
# 否则会把正常的换行误判成"句中折断"合并回去，反而糊成一大坨。
_PARTICLES = "嘛啦吧呀呢哦啊哈呗咯喔噢诶嘿嗯呐哇唔咯嘞"


def _ends_sentence(line: str) -> bool:
    s = line.rstrip()
    if not s:
        return False
    return (
        s[-1] in _SENT_END
        or s[-1] in _CLOSERS
        or s[-1] in _EMOJI_END
        or s[-1] in _PARTICLES
    )


def _split_sentences(line: str) -> list[str]:
    """只在句末标点后切开，用于把过长的一行排成多行。"""
    parts = re.findall(r"[^。！？!?…]*[。！？!?…]+|[^。！？!?…]+$", line)
    return [p.strip() for p in parts if p.strip()]


def format_reply_lines(text: str, max_lines: int = 3, auto_split_over: int = 80) -> str:
    """把回复整理成"只在句末换行"的版式（模型排版失误的确定性兜底）。

    1) 模型用 [/]（或真换行）标断行；断在句中（上一行没有句末标点）就合并回去
    2) 一行里塞了多句且过长（> auto_split_over）时，按句末标点排成多行
    3) 超过 max_lines 的部分并到最后一行，避免一屏全是几个字的短行
    """
    if not text:
        return ""
    lines: list[str] = []
    for part in re.split(r"\[/\]|\r?\n", text):
        piece = part.strip()
        if not piece:
            continue
        if lines and not _ends_sentence(lines[-1]):
            lines[-1] += piece
        else:
            lines.append(piece)

    spread: list[str] = []
    for line in lines:
        spread.extend(_split_sentences(line) if len(line) > auto_split_over else [line])

    if len(spread) > max_lines:
        spread = spread[: max_lines - 1] + ["".join(spread[max_lines - 1 :])]
    return "\n".join(spread)


def build_main_prompt(
    config: Config,
    plugin_section: str,
    formatted_history: str,
    trigger_marker: str,
    memory_section: str = "",
    personal_memory_section: str = "",
    current_time: str = "",
    long_term_memory: str = "",
    relevant_facts: str = "",
    persona: str = "",
) -> str:
    parts = [
        f"你的名字是{config.name_cn}，英文名是{config.name}，你的设定是{config.person_setting}。",
        "",
        f"【当前真实时间】{current_time}，这是现在的真实时间，不是假设或设定。所有联网搜索结果都基于此时间。",
        "",
        "你需要按照如下的格式输出：",
        config.output_style,
        "",
        "你的说话风格如下：",
        config.reply_style,
        "",
        "除此之外，有一些额外的要求需要你注意：",
        config.extra_style,
    ]

    # 人设覆盖段紧跟 extra_style：基础设定里语气要求和协议规则是同一段文字，
    # 只有排在后面并显式声明"只覆盖性格语气"，才压得住上面那段而不带走机制规则。
    if persona:
        parts.append("")
        parts.append(persona)

    if plugin_section:
        parts.append("")
        parts.append(plugin_section)


    if long_term_memory:
        parts.append("")
        parts.append("【重要记忆】以下是你必须记住的内容：")
        parts.append(_cap(long_term_memory, _LTM_CAP))

    if relevant_facts:
        parts.append("")
        parts.append("【相关记忆】以下是与当前对话相关的记忆：")
        parts.append(_cap(relevant_facts, _FACTS_CAP))

    if long_term_memory or relevant_facts:
        parts.append(
            "（记忆只是背景参考，用来理解人物关系和前情，"
            "不要照抄或复述；回复请以【最近的聊天记录】里正在发生的事为准。）"
        )


    # 旧版记忆仅在增强记忆系统没有长期记忆时展示，避免重复
    if memory_section and not long_term_memory:
        parts.append("")
        parts.append("你之前记住的东西：")
        parts.append(_cap(memory_section, _OLDMEM_CAP))

    if personal_memory_section:
        parts.append("")
        parts.append("你对群友的印象：")
        parts.append(_cap(personal_memory_section, _PERSONAL_CAP))

    parts.append("")
    parts.append("这是最近的聊天记录（从上到下按时间顺序，最后一条是刚刚发生的）：")
    parts.append(formatted_history)
    parts.append("")
    parts.append(
        "请顺着上面这段对话的脉络接话：先理解当前正在聊什么，"
        "再回应最后那条消息。不要重复你之前已经说过的话，"
        "也不要突然跳到无关话题。"
    )
    parts.append(
        "【长度要求】每次回复控制在 80 词左右（中文约 80 字），"
        "像群友接话一样自然，不要太短敷衍也不要长篇大论、不要罗列。"
    )
    parts.append(_LAYOUT_GUIDE)
    parts.append(_MEMORIZE_GUIDE)
    parts.append("")
    parts.append(trigger_marker)

    return "\n".join(parts)


_CQ_IMAGE = re.compile(r"\[CQ:image,[^\]]*\]")
_CQ_AT = re.compile(r"\[CQ:at,qq=([^\],]+)[^\]]*\]")
_CQ_FACE = re.compile(r"\[CQ:face,[^\]]*\]")
_CQ_OTHER = re.compile(r"\[CQ:[^\]]*\]")


def _clean_history_text(text: str) -> str:
    """压缩历史记录里的富文本噪音。

    历史里常混入完整的 CQ 码，其中图片 URL 长达上百字符且对理解
    对话毫无帮助，反而挤占上下文并干扰模型判断。这里把它们统一
    替换成简短标记。
    """
    if not text:
        return ""
    text = _CQ_IMAGE.sub("[图片]", text)
    text = _CQ_AT.sub(lambda m: f"@{m.group(1)}", text)
    text = _CQ_FACE.sub("[表情]", text)
    text = _CQ_OTHER.sub("", text)
    return text.strip()


def format_history(
    history: list[dict],
    history_image_descs: dict[tuple[int, int], str] | None = None,
) -> str:
    if not history:
        return "（暂无聊天记录）"

    history_image_descs = history_image_descs or {}
    lines = []
    for msg_idx, msg in enumerate(history):
        user_id = msg.get("user_id", "?")
        user_name = msg.get("user_name", "未知")
        segments = msg.get("segments")

        if msg.get("msg_type") == "mid_term_memory":
            # 摘要行不是"某人说的话"，混在聊天记录里会被当成逐字发言。单独成块，
            # 并明确标注它只是背景，避免模型照着摘要复述。
            summary_text = _clean_history_text(msg.get("message", ""))
            if summary_text:
                lines.append("【前情提要（更早的对话已压缩，只作背景参考，不要复述）】\n" + summary_text)
            continue

        if segments:
            parts = []
            reply_info = ""
            for seg_idx, seg in enumerate(segments):
                if seg["type"] == "reply":
                    ruid = seg.get("user_id", "")
                    rname = seg.get("user_name", "")
                    rmsg = _clean_history_text(seg.get("message", ""))
                    # 引用的正文截断，避免它喧宾夺主
                    if len(rmsg) > 30:
                        rmsg = rmsg[:30] + "…"
                    if ruid:
                        reply_info = f"回复{rname}：{rmsg}"
                    elif rmsg:
                        reply_info = rmsg
                elif seg["type"] == "text":
                    parts.append(seg.get("text", ""))
                elif seg["type"] == "at":
                    qq = seg.get("qq", "")
                    if qq == "all":
                        parts.append("@所有人")
                    else:
                        parts.append(f"@{qq}")
                elif seg["type"] == "image":
                    # 优先用段落里已持久化的描述（下次对话可直接复用），
                    # 否则用本轮刚识别出的结果
                    desc = seg.get("desc") or history_image_descs.get((msg_idx, seg_idx), "")
                    if desc:
                        parts.append(f"[图片：{desc}]")
                    else:
                        parts.append("[图片]")
            msg_text = _clean_history_text("".join(parts))
            if reply_info:
                line = f"{user_name}({user_id})（{reply_info}）：{msg_text}"
            else:
                line = f"{user_name}({user_id})：{msg_text}"
        else:
            text = _clean_history_text(msg.get("message", ""))
            line = f"{user_name}({user_id})：{text}"

        # 跳过空内容行：形如"某人(123)："后面什么都没有，纯噪音
        if not line.split("：", 1)[-1].strip():
            continue

        lines.append(line)

    return "\n".join(lines) if lines else "（暂无聊天记录）"


def build_message_text(
    segments: list[dict],
    image_descriptions: list[str],
) -> str:
    parts = []
    reply_prefix = ""
    desc_idx = 0
    for seg in segments:
        if seg["type"] == "reply":
            ruid = seg.get("user_id", "")
            rname = seg.get("user_name", "")
            rmsg = seg.get("message", "")
            if ruid:
                reply_prefix = f"（回复{rname}({ruid})：{rmsg}）"
            elif rmsg:
                reply_prefix = f"（回复：{rmsg}）"
        elif seg["type"] == "text":
            parts.append(seg.get("text", ""))
        elif seg["type"] == "at":
            qq = seg.get("qq", "")
            name = seg.get("name", "")
            if qq == "all":
                parts.append("@所有人")
            elif name:
                parts.append(f"@{name}({qq})")
            else:
                parts.append(f"@{qq}")
        elif seg["type"] == "image":
            desc = image_descriptions[desc_idx] if desc_idx < len(image_descriptions) else ""
            desc_idx += 1
            if desc:
                parts.append(f"[图片][图片描述：{desc}]")
            else:
                parts.append("[图片]")
    text = "".join(parts)
    if reply_prefix:
        return reply_prefix + text
    return text


TRIGGER_MAIN = "你看到这里，准备回复"
TRIGGER_AT = "你被at了，准备回复"
TRIGGER_IN_PROGRESS = "你正在准备上一条消息"
TRIGGER_INTERRUPT = "你在回复途中被at了，准备回复。回复将会在上一个消息发送后，发送。"

def build_trigger_line(
    user_name: str,
    user_id: str,
    message_text: str,
    marker: str,
) -> str:
    return f"{user_name}({user_id})：{message_text}  <--{marker}"


def build_image_analysis_prompt(image_analyzer_prompt: str, context: str = "") -> str:
    parts = [image_analyzer_prompt]
    if context:
        parts.append("")
        parts.append("最近的聊天记录供参考：")
        parts.append(context)
    parts.append("")
    parts.append("请为以下图片生成描述，使用 JSON 格式返回：")
    parts.append("{")
    parts.append('    "image_1": "描述",')
    parts.append('    "image_2": "描述",')
    parts.append("    ...")
    parts.append("}")
    return "\n".join(parts)


def build_search_decision_prompt(context: str, search_results_text: str) -> str:
    return (
        f"用户的问题是：{context}\n\n"
        f"这是搜索到的结果：\n{search_results_text}\n\n"
        "你的任务：\n"
        "1. 分析哪些结果有价值\n"
        "2. 决定 fetch 哪些 URL\n"
        "3. 判断是否需要换关键词重新搜索\n\n"
        "请用 JSON 格式返回：\n"
        "{\n"
        '    "fetch_urls": ["url1", "url2"],\n'
        '    "reasoning": "选择理由",\n'
        '    "needs_more_search": false,\n'
        '    "next_query": "新搜索词"\n'
        "}"
    )


def build_search_summary_prompt(fetched_content: str) -> str:
    return (
        f"这是提取的网页内容：\n{fetched_content}\n\n"
        "请综合搜索结果，给出总结。使用 JSON 格式返回：\n"
        "{\n"
        '    "summary": "搜索总结",\n'
        '    "sources": ["来源1"]\n'
        "}"
    )


def build_search_inject_prompt(
    search_summary: str,
    new_messages_since_search: str = "",
    search_failed: bool = False,
    current_time: str = "",
) -> str:
    if not current_time:
        from datetime import datetime
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if search_failed:
        return "联网搜索失败了，无法获取搜索结果。请直接根据已有信息回复用户。"
    parts = [
        f"你刚才进行了联网搜索（当前真实时间 {current_time}），这是搜索结果：",
        search_summary,
    ]
    if new_messages_since_search:
        parts.append("")
        parts.append("联网搜索期间群友的新发言：")
        parts.append(new_messages_since_search)
    parts.append("")
    parts.append("请结合这些信息，重新构思回复。")
    return "\n".join(parts)


def build_summary_prompt(
    old_summary: str,
    history_text: str,
    max_chars: int,
) -> str:
    """滚动摘要提示词：旧摘要 + 新一段逐字历史 → 一条合并后的新摘要。

    合并式（而不是追加式）是这套机制能突破上下文长度的关键：摘要总长封顶，
    压缩次数再多也不会让 prompt 变大，但可回溯的历史长度是无限的。
    """
    parts = [
        "你要把群聊的历史压缩成一段“前情提要”，供之后接着聊时参考。",
        "",
        "要求：",
        "1. 用第三人称陈述，不要写成对话稿，不要加标题、前后缀或解释。",
        f"2. 总长度不超过 {max_chars} 字，宁可精炼也不要堆细节。",
        "3. 优先保留：参与聊天的人及其关系、正在争论或讨论的话题、已给出的结论与"
        "承诺、悬而未决的问题、出现过的昵称/梗、以及明显的时间线索（如“明天”“上周”）。",
        "4. 不要编造历史里没有的信息；无法确定的细节直接省略。",
        "5. 如果一段内容反复出现（例如同一句玩笑），只记一次。",
    ]
    if old_summary:
        parts.append("")
        parts.append("已有的前情提要（请与下面的新内容合并成一段，不要简单拼接）：")
        parts.append(old_summary)
    parts.append("")
    parts.append("需要压缩的新内容：")
    parts.append(history_text)
    parts.append("")
    parts.append("直接输出合并后的前情提要正文：")
    return "\n".join(parts)
