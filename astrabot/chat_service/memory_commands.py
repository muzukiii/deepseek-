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
    delete_long_term_memories_by_ids,
    delete_long_term_memory,
    find_long_term_memories,
    get_long_term_memories,
    get_recent_facts,
    delete_fact,
)
from astrabot.logmanager.logger import logger


_DESTRUCTIVE_RES = (
    re.compile(r"/memory\s+delete\s+[mf]?\d+", re.IGNORECASE),
    re.compile(r"/删除记忆\s+[mf]?\d+", re.IGNORECASE),
    # 批量删：预览也要白名单（否则任何群友都能枚举并看到统计），确认更要
    re.compile(r"/memory\s+delete\s+before\s+\S+", re.IGNORECASE),
    re.compile(r"/memory\s+delete\s+keyword\s+\S+", re.IGNORECASE),
    re.compile(r"/memory\s+clear\s+long", re.IGNORECASE),
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


def _unique_path(name: str) -> Path:
    """同名文件已存在时追加 -2/-3…，避免连续导出或连续删除互相覆盖备份。"""
    path = _EXPORT_DIR / name
    if not path.exists():
        return path
    base, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = _EXPORT_DIR / f"{base}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


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
    name = f"{title}_{group_id}_{now.strftime('%m%d-%H%M%S')}-{now.microsecond // 1000:03d}.txt"
    path = _unique_path(name)
    name = path.name
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
/memory delete before 10-20 - 批量删早于该日的长期记忆
/memory delete keyword 表情包 - 批量删含关键词的长期记忆
/memory clear long - 清空全部长期记忆
/memory clear - 只回显提示，不删（确认用下一行）
/memory clear confirm - 清空本群事实记忆（长期记忆保留）
/memory help - 显示此帮助

说明：
• 长期记忆：你明确要求记住的内容，会始终加载
• 事实记忆：AI 自动从对话中提取的事实，按相关性加载
• 列表改发群文件（txt）；上传失败时自动回退为最近 20 条
• 导出文件里的 m12 / f311 标签可以直接用在删除命令里
• 三条批量命令都是先预览条数，确认时在同一句末尾加 confirm；
  确认那一步会先把要删的条目导出成备份文件再执行"""
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
    bulk = try_bulk_delete(group_id, message)
    if bulk is not None:
        return bulk

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


# ========== 批量删除长期记忆（预览 → 同一条件 + confirm，先备份再删） ==========
_BULK_BEFORE_RE = re.compile(
    r"^/memory\s+delete\s+before\s+(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})(\s+confirm)?$", re.IGNORECASE
)
_BULK_KW_RE = re.compile(r"^/memory\s+delete\s+keyword\s+(.+?)(\s+confirm)?$", re.IGNORECASE)
_CLEAR_LONG_RE = re.compile(r"^/memory\s+clear\s+long(\s+confirm)?$", re.IGNORECASE)


def _parse_cutoff(raw: str) -> int:
    """`2026-10-20` 或 `10-20` → 当天 00:00 的时间戳（本地时区）。"""
    txt = raw if len(raw) == 10 else f"{datetime.now().year}-{raw}"
    return int(datetime.strptime(txt, "%Y-%m-%d").timestamp())


def _write_delete_backup(group_id: int, entries: list[dict]) -> dict:
    """把"本次要删的条目"写成备份 txt，返回 {path, name, caption, fallback}。"""
    now = datetime.now()
    lines = [
        "【AstraBot 删除备份 · 长期记忆】",
        f"群号：{group_id}",
        f"导出时间：{now.strftime('%Y-%m-%d %H:%M')}",
        f"总数：{len(entries)} 条（本次删除的条目，按日期倒序）",
    ]
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
    name = f"删除备份_长期记忆_{group_id}_{now.strftime('%m%d-%H%M%S')}-{now.microsecond // 1000:03d}.txt"
    path = _unique_path(name)
    name = path.name
    path.write_text("\n".join(lines), encoding="utf-8")

    stale = sorted(_EXPORT_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in stale[_EXPORT_KEEP:]:
        old.unlink(missing_ok=True)
    return {
        "path": path,
        "name": name,
        "caption": f"备份见群文件：{name}",
        "fallback": f"备份文件发送失败，本地备份：{name}",
    }


def _bulk_delete_long(
    group_id: int,
    *,
    label: str,
    confirm_hint: str,
    before_ts: int | None = None,
    keyword: str | None = None,
    confirm: bool = False,
) -> tuple[bool, str, dict | None]:
    """按条件批量删长期记忆：预览只报数；confirm 才先备份、再删。"""
    rows = find_long_term_memories(group_id, before_ts=before_ts, keyword=keyword)
    if not rows:
        return True, f"没有符合条件的长期记忆（{label}）", None

    entries = [
        {"ts": r.get("created_at"), "tag": f"m{r['id']}", "text": (r.get("content") or "").strip()}
        for r in rows
    ]
    ids = [r["id"] for r in rows]
    times = [int(e["ts"]) for e in entries if e["ts"]]
    span = f"{_fmt_ts(min(times))} ~ {_fmt_ts(max(times))}" if times else "—"

    if not confirm:
        return True, (
            f"将删除 {len(ids)} 条长期记忆（{label}，{span}，m{min(ids)} ~ m{max(ids)}）\n"
            f"删除前会自动导出备份。确认请发：\n{confirm_hint}"
        ), None

    spec = _write_delete_backup(group_id, entries)
    removed = delete_long_term_memories_by_ids(group_id, ids)
    logger.warning(
        f"Bulk deleted {removed} long-term memories in group {group_id} ({label}); "
        f"backup={spec['name']}"
    )
    spec["caption"] = f"已删除 {removed} 条长期记忆（{label}），备份见群文件：{spec['name']}"
    spec["fallback"] = f"已删除 {removed} 条长期记忆（{label}）。备份文件发送失败，本地备份：{spec['name']}"
    return True, spec["caption"], spec


def try_bulk_delete(group_id: int, message: str) -> tuple[bool, str, dict | None] | None:
    """批量删除入口；不是批量删除指令时返回 None，交给原处理器。"""
    m = message.strip()
    mb = _BULK_BEFORE_RE.match(m)
    if mb:
        cutoff = _parse_cutoff(mb.group(1))
        return _bulk_delete_long(
            group_id,
            before_ts=cutoff,
            confirm=bool(mb.group(2)),
            label=f"早于 {datetime.fromtimestamp(cutoff).strftime('%Y-%m-%d')}",
            confirm_hint=f"/memory delete before {mb.group(1)} confirm",
        )
    mk = _BULK_KW_RE.match(m)
    if mk:
        kw = mk.group(1).strip()
        if not kw:
            return True, "关键词不能为空", None
        return _bulk_delete_long(
            group_id,
            keyword=kw,
            confirm=bool(mk.group(2)),
            label=f"含「{kw}」",
            confirm_hint=f"/memory delete keyword {kw} confirm",
        )
    mcl = _CLEAR_LONG_RE.match(m)
    if mcl:
        return _bulk_delete_long(
            group_id,
            confirm=bool(mcl.group(1)),
            label="清空全部长期记忆",
            confirm_hint="/memory clear long confirm",
        )
    return None
