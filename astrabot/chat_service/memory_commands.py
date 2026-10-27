"""
记忆管理命令处理器

支持的命令：
- 记住：XXX - 添加长期记忆
- /memory list - 查看所有长期记忆
- /memory facts - 查看最近的事实记忆
- /memory delete <id> - 删除指定记忆
- /memory clear - 清空所有事实记忆（保留长期记忆）
- /人设 - 查看本群人设；/人设 <预设名> - 切换本群人设（预设见 persona_store.PRESETS）
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from astrabot.chat_service.enhanced_memory import (
    add_long_term_memory,
    delete_long_term_memory,
    get_long_term_memories,
    get_recent_facts,
    delete_fact,
)
from astrabot.logmanager.logger import logger


_DESTRUCTIVE_RES = (
    re.compile(r"/memory\s+delete\s+[mf]?\d+", re.IGNORECASE),
    re.compile(r"/删除记忆\s+[mf]?\d+", re.IGNORECASE),
)

# /人设、/persona：不带参数=查看，带参数=切换
_PERSONA_RE = re.compile(r"^/(?:人设|persona)[ \t]*(.*)$", re.IGNORECASE)

# 导出文件落盘目录（data/ 已 gitignore）；只保留最近若干份，避免无限堆积
_EXPORT_DIR = Path(__file__).resolve().parent / "data" / "exports"
_EXPORT_KEEP = 20
_EXPORT_LINE_MAX = 200   # 文件里单条最多显示多少字
_INLINE_TOP = 20         # 上传失败时聊天里最多列多少条

_EXPORT_TITLES = {"long": "长期记忆", "facts": "事实记忆"}


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "未知日期"
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")


def build_memory_export(group_id: int, kind: str) -> dict:
    """把记忆导出成 txt 文件，返回 {path, name, caption, fallback}。

    群聊里直接列几百条会把一条消息撑爆（实测 388 条 = 23K 字），所以改成发文件：
    文件里给统计摘要 + 按日期分组的全量内容，聊天里只回一行统计。
    """
    kind = "facts" if kind == "facts" else "long"
    title = _EXPORT_TITLES[kind]
    now = datetime.now()

    if kind == "long":
        rows = get_long_term_memories(group_id)
        entries = [
            {"ts": m.get("created_at"), "tag": f"m{m['id']}", "text": (m.get("content") or "").strip()}
            for m in rows
        ]
        limit_note = "全量"
    else:
        rows = get_recent_facts(group_id, limit=200)
        entries = [
            {
                "ts": f.get("created_at"),
                "tag": f"f{f['id']}" + (f" (访问{f['access_count']})" if f.get("access_count") else ""),
                "text": (f.get("fact") or "").strip(),
            }
            for f in rows
        ]
        limit_note = "最近 200 条"

    entries = [e for e in entries if e["text"]]
    entries.sort(key=lambda e: (e["ts"] or 0), reverse=True)

    times = [int(e["ts"]) for e in entries if e["ts"]]
    span = f"{_fmt_ts(min(times))} ~ {_fmt_ts(max(times))}" if times else "—"
    by_month: dict[str, int] = {}
    for t in times:
        key = datetime.fromtimestamp(t).strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + 1
    month_note = " · ".join(f"{k} {v} 条" for k, v in sorted(by_month.items(), reverse=True)[:6])

    lines = [
        f"【AstraBot 记忆导出 · {title}】",
        f"群号：{group_id}",
        f"导出时间：{now.strftime('%Y-%m-%d %H:%M')}",
        f"总数：{len(entries)} 条（{limit_note}） | 时间跨度：{span}",
    ]
    if month_note:
        lines.append(f"按月：{month_note}")
    lines.append(f"说明：每行是「标签 内容」，标签可用于 /memory delete <标签>")
    lines.append("")
    lines.append("—— 按日期倒序 ——")

    current_day = None
    for e in entries:
        day = _fmt_ts(e["ts"])
        if day != current_day:
            lines.append("")
            lines.append(f"[{day}]")
            current_day = day
        text = e["text"]
        if len(text) > _EXPORT_LINE_MAX:
            text = text[:_EXPORT_LINE_MAX] + "…"
        lines.append(f"  {e['tag']} {text}")

    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{title}_{group_id}_{now.strftime('%m%d-%H%M')}.txt"
    path = _EXPORT_DIR / name
    path.write_text("\n".join(lines), encoding="utf-8")

    stale = sorted(_EXPORT_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in stale[_EXPORT_KEEP:]:
        old.unlink(missing_ok=True)

    caption = f"已导出{title} {len(entries)} 条，见群文件：{name}"
    top = [f"{e['tag']} {e['text'][:60]}" for e in entries[:_INLINE_TOP]]
    fallback = "\n".join(
        [f"文件发送失败，先列最近 {len(top)} 条（共 {len(entries)} 条）：", *top]
    )
    return {"path": path, "name": name, "caption": caption, "fallback": fallback}


def requires_whitelist(message: str) -> bool:
    """需要白名单才能执行的命令（不含破坏性记忆命令，那部分另算）。

    切换人设改的是机器人在**整个群**的说话方式，一条消息就能让全群都
    被呛，影响面大于删一条记忆；没配白名单时一律 fail-safe 拒绝。
    光秃秃的 `/人设` 只是查列表，不受限。
    """
    m = _PERSONA_RE.match(message.strip())
    return bool(m and m.group(1).strip())


def is_destructive_command(message: str) -> bool:
    """判断是否为破坏性记忆操作（删条目 / 清空事实库）。

    这类命令在没有配置白名单时必须 fail-safe 拒绝，避免任意群友
    一键清空机器人的记忆。`/memory clear`（未带 confirm）只是回显
    提示，不算破坏性。
    """
    m = message.strip()
    if m == "/memory clear confirm":
        return True
    return any(p.search(m) for p in _DESTRUCTIVE_RES)


def handle_memory_command(
    group_id: int,
    user_id: str,
    message: str,
) -> tuple[bool, str]:
    """
    处理记忆相关命令

    返回：(是否处理, 回复消息)
    """
    message = message.strip()


    if message.startswith("记住：") or message.startswith("记住:"):
        content = message[3:].strip()
        if not content:
            return True, "要记住什么呢？"

        if add_long_term_memory(group_id, content, created_by=user_id):
            logger.info(f"User {user_id} added long-term memory: {content}")
            return True, f"好的，我会记住：{content}"
        else:
            return True, "这个我已经记住了"


    if message == "/memory list" or message == "/记忆列表":
        memories = get_long_term_memories(group_id)
        if not memories:
            return True, "还没有长期记忆"

        lines = ["【长期记忆列表】"]
        for m in memories:
            lines.append(f"{m['id']}. {m['content']}")

        return True, "\n".join(lines)


    if message == "/memory facts" or message == "/事实记忆":
        facts = get_recent_facts(group_id, limit=20)
        if not facts:
            return True, "还没有提取到事实记忆"

        lines = ["【最近的事实记忆】"]
        for f in facts:
            access_info = f"(访问{f['access_count']}次)" if f['access_count'] > 0 else ""
            lines.append(f"{f['id']}. {f['fact']} {access_info}")

        return True, "\n".join(lines)


    if message in ("/用量", "/usage", "/cost"):
        from astrabot.chat_service.usage_tracker import summary

        return True, summary()

    if message in ("/用量 今日", "/usage today"):
        from astrabot.chat_service.usage_tracker import summary

        return True, summary(days=1)

    if message in ("/用量 7天", "/usage 7d"):
        from astrabot.chat_service.usage_tracker import summary

        return True, summary(days=7)


    pm = _PERSONA_RE.match(message)
    if pm:
        from astrabot.chat_service import persona_store

        arg = pm.group(1).strip()
        if not arg:
            return True, persona_store.persona_status(group_id)
        ok, reply = persona_store.set_persona(group_id, arg)
        if ok:
            logger.info(f"User {user_id} set persona '{arg}' in group {group_id}")
        return True, reply


    # 支持 /memory delete m12（只删长期记忆）、f311（只删事实）、以及旧的纯数字形式。
    # 纯数字仍是"先长期、再事实"，因为导出/列表里给的是带前缀的标签，两者不会撞车。
    match = re.match(r"/memory\s+delete\s+([mf]?)(\d+)", message)
    if not match:
        match = re.match(r"/删除记忆\s+([mf]?)(\d+)", message)
    if match:
        prefix, raw_id = (match.group(1) or "").lower(), int(match.group(2))
        if prefix == "m":
            if delete_long_term_memory(group_id, raw_id):
                logger.info(f"User {user_id} deleted long-term memory {raw_id}")
                return True, f"已删除长期记忆 m{raw_id}"
            return True, f"找不到长期记忆 m{raw_id}"
        if prefix == "f":
            if delete_fact(group_id, raw_id):
                logger.info(f"User {user_id} deleted fact {raw_id}")
                return True, f"已删除事实记忆 f{raw_id}"
            return True, f"找不到事实记忆 f{raw_id}"
        if delete_long_term_memory(group_id, raw_id):
            logger.info(f"User {user_id} deleted long-term memory {raw_id}")
            return True, f"已删除长期记忆 #{raw_id}（下次请用 m{raw_id} 精确指定）"
        if delete_fact(group_id, raw_id):
            logger.info(f"User {user_id} deleted fact {raw_id}")
            return True, f"已删除事实记忆 #{raw_id}（下次请用 f{raw_id} 精确指定）"
        return True, f"找不到记忆 #{raw_id}"


    if message == "/memory clear" or message == "/清空事实记忆":

        return True, "此命令会清空所有自动提取的事实记忆（保留长期记忆），请使用 /memory clear confirm 确认"

    if message == "/memory clear confirm":
        from astrabot.chat_service.enhanced_memory import _get_db
        conn = _get_db()
        cur = conn.execute("DELETE FROM facts WHERE group_id = ?", (group_id,))
        conn.commit()
        count = cur.rowcount
        logger.warning(f"User {user_id} cleared {count} facts in group {group_id}")
        return True, f"已清空 {count} 条事实记忆"


    if message == "/memory help" or message == "/记忆帮助":
        help_text = """【记忆管理命令】
记住：XXX - 添加长期记忆
/memory list - 导出长期记忆（txt 文件）
/memory facts - 导出事实记忆（txt 文件）
/memory delete m12 - 删除长期记忆 #12
/memory delete f311 - 删除事实记忆 #311
/memory delete 12 - 旧写法（先查长期记忆，再查事实记忆）
/memory clear - 清空所有事实记忆
/memory help - 显示此帮助

说明：
• 长期记忆：你明确要求记住的内容，会始终加载
• 事实记忆：AI 自动从对话中提取的事实，按相关性加载
• 导出文件里的 m12 / f311 标签可以直接用在删除命令里"""
        return True, help_text

    return False, ""


def check_implicit_memory_request(message: str) -> str | None:
    """
    检查隐式的记忆请求

    例如："记住我喜欢吃辣" -> "我喜欢吃辣"

    返回：提取的记忆内容，如果不是记忆请求则返回 None
    """
    patterns = [
        r'^记住[，：:]\s*(.+)$',
        r'^记住\s+(.+)$',
        r'^帮我记住[，：:]\s*(.+)$',
        r'^帮我记住\s+(.+)$',
        r'^请记住[，：:]\s*(.+)$',
        r'^请记住\s+(.+)$',
    ]

    for pattern in patterns:
        match = re.match(pattern, message.strip())
        if match:
            return match.group(1).strip()

    return None


_LIST_CMDS = ("/memory list", "/记忆列表")
_FACT_CMDS = ("/memory facts", "/事实记忆")


def handle_memory_command_with_export(
    group_id: int, user_id: str, message: str
) -> tuple[bool, str, dict | None]:
    """命令入口：长列表命令改为导出 txt 文件，其余命令原样走 handle_memory_command。

    返回 (是否已处理, 回复文本, 导出文件信息或 None)。文件由调用方上传；
    上传失败时用 spec["fallback"] 退回"最近 20 条"的内联展示。
    """
    m = message.strip()
    if m in _LIST_CMDS or m in _FACT_CMDS:
        kind = "facts" if m in _FACT_CMDS else "long"
        rows = get_recent_facts(group_id, limit=1) if kind == "facts" else get_long_term_memories(group_id)
        if not rows:
            handled, text = handle_memory_command(group_id, user_id, message)
            return handled, text, None
        spec = build_memory_export(group_id, kind)
        logger.info(f"Memory export for group {group_id}: {spec['name']}")
        return True, spec["caption"], spec

    handled, text = handle_memory_command(group_id, user_id, message)
    return handled, text, None
