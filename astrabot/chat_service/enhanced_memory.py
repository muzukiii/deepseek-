"""
增强的记忆管理系统

三层架构：
1. 短期记忆：最近的对话上下文（已有的 history）
2. 中期记忆：自动提取的事实，按相关性检索
3. 长期记忆：用户明确要求记住的内容，始终加载
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from astrabot.chat_service.ai_client import chat
from astrabot.chat_service.text_tokenizer import extract_keywords, term_list
from astrabot.config import get_config
from astrabot.logmanager.logger import logger

DATA_DIR = Path(__file__).resolve().parent / "data"
MEMORY_DB = DATA_DIR / "memories.db"
_local = threading.local()


def _get_db() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(MEMORY_DB))
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _init_db(_local.conn)
    return _local.conn


def _init_db(conn: sqlite3.Connection):
    """初始化数据库表"""

    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id TEXT,
            fact TEXT NOT NULL,
            keywords TEXT,
            created_at INTEGER NOT NULL,
            access_count INTEGER DEFAULT 0,
            last_accessed INTEGER,
            importance INTEGER DEFAULT 1,
            UNIQUE(group_id, fact)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_group ON facts(group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_keywords ON facts(keywords)")


    conn.execute("""
        CREATE TABLE IF NOT EXISTS long_term_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            created_by TEXT,
            UNIQUE(group_id, content)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ltm_group ON long_term_memories(group_id)")


    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_impressions (
            group_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            impression TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (group_id, user_id)
        )
    """)

    conn.commit()




async def extract_facts_from_conversation(
    group_id: int,
    user_id: str,
    user_name: str,
    user_msg: str,
    ai_reply: str,
) -> list[str]:
    """从对话中提取事实，返回提取的事实列表"""
    try:
        prompt = f"""从以下对话中提取值得长期记住的事实。

对话：
{user_name}: {user_msg[:500]}
AI: {ai_reply[:500]}

提取规则：
1. 只提取"以后还用得上"的稳定信息：
   - 用户的个人偏好、习惯、性格特点
   - 用户的个人背景、技能、身份
   - 明确的约定或计划
   - 群内的长期规则、共识
2. 绝对不要提取：
   - 本次对话发生了什么（例如"某人问了X，小池回应了Y"）
   - 小池自己的回复内容或立场
   - 随口说的话、玩笑、情绪化表达、日常寒暄
3. 每条一句话，必须包含主语（谁的信息）。
4. 宁缺毋滥：大多数对话都提取不出东西。没有值得记录的返回 []。

判断标准：这条信息一周后再看，还有用吗？没用就不要记。

输出格式：纯 JSON 数组
["事实1", "事实2"]"""

        messages = [{"role": "user", "content": prompt}]
        # 事实提取复用主 API：原先写死 provider="MINIMAX" 且参数名有误
        # （chat() 接受的是 protocol），导致该功能从未真正生效。
        config = get_config()
        result = await chat(
            protocol=config.API_PROTOCOL,
            base_url=config.API_BASE_URL,
            api_key=config.API_KEY,
            model=config.API_MODEL,
            messages=messages,
            timeout=30,
        )

        content = result.get("content", "").strip()
        if not content:
            return []


        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        facts = json.loads(content)
        if not isinstance(facts, list):
            return []


        stored = []
        for fact in facts:
            if not isinstance(fact, str) or not fact.strip():
                continue
            if add_fact(group_id, fact, user_id=user_id):
                stored.append(fact)

        if stored:
            logger.info(f"Extracted {len(stored)} facts from {user_name}({user_id})")

        return stored

    except json.JSONDecodeError as e:
        logger.debug(f"Fact extraction JSON parse failed: {e}")
        return []
    except Exception as e:
        logger.debug(f"Fact extraction failed: {e}")
        return []


def _normalize(text: str) -> str:
    """归一化记忆文本：把连续空白（空格/制表/换行）压成单个空格。

    UNIQUE 约束只做精确去重，`喜欢  吃辣` 与 `喜欢 吃辣` 会被当成两条。
    入库前统一压空白，让仅有空白差异的内容真正命中去重。
    """
    return " ".join(text.split())


def add_fact(
    group_id: int,
    fact: str,
    user_id: str | None = None,
    importance: int = 1,
) -> bool:
    """添加一条事实记忆"""
    fact = _normalize(fact)
    if not fact:
        return False


    keywords = _extract_keywords(fact)

    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO facts (group_id, user_id, fact, keywords, created_at, importance)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (group_id, user_id, fact, keywords, int(time.time()), importance),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # 事实已存在（UNIQUE 约束），改为增加访问计数
        conn.execute(
            """UPDATE facts SET access_count = access_count + 1, last_accessed = ?
               WHERE group_id = ? AND fact = ?""",
            (int(time.time()), group_id, fact),
        )
        conn.commit()
        return False


def _extract_keywords(text: str) -> str:
    """提取关键词（供 LIKE 检索使用，空格分隔）。

    原先用 text.split() 切分，而中文没有空格，整句会被当成一个
    "词"，导致关键词检索形同虚设。现改为走 text_tokenizer 的中文
    bigram 分词。
    """
    return extract_keywords(text)


def search_relevant_facts(
    group_id: int,
    query: str,
    limit: int = 5,
    user_id: str | None = None,
) -> list[dict]:
    """按关键词检索与查询相关的事实记忆。

    原实现只匹配 keywords 字段，且从未被任何地方调用（等于没有检索）。
    现在同时匹配事实正文，并按「命中关键词数量」排序，让最相关的排前面。

    user_id 非空时，该用户的事实会获得加权，避免被其他人的记忆盖过。
    """
    tokens = term_list(query, limit=12)
    if not tokens:
        return []

    conn = _get_db()
    conn.row_factory = sqlite3.Row

    # 每个词元分别匹配 keywords 与 fact 正文
    clauses = []
    params: list = []
    for tok in tokens:
        clauses.append("(keywords LIKE ? OR fact LIKE ?)")
        params.extend([f"%{tok}%", f"%{tok}%"])

    sql = (
        "SELECT id, fact, user_id, keywords, created_at, access_count, importance "
        "FROM facts WHERE group_id = ? AND (" + " OR ".join(clauses) + ")"
    )
    params.insert(0, group_id)

    try:
        rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        logger.debug(f"search_relevant_facts failed: {e}")
        return []

    scored: list[tuple[int, float, dict]] = []
    for row in rows:
        haystack = f"{row['keywords'] or ''} {row['fact'] or ''}"
        hits = sum(1 for tok in tokens if tok in haystack)
        if hits == 0:
            continue
        # 命中数为主，importance 与访问次数为辅
        score = hits * 10 + float(row["importance"] or 0) + min(row["access_count"] or 0, 5) * 0.1
        if user_id and str(row["user_id"]) == str(user_id):
            score += 5  # 与当前对话对象相关，优先
        scored.append((hits, score, dict(row)))

    scored.sort(key=lambda x: (-x[1], -x[2].get("created_at", 0)))
    results = [item for _, _, item in scored[:limit]]

    now = int(time.time())
    for item in results:
        conn.execute(
            "UPDATE facts SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
            (now, item["id"]),
        )
    if results:
        conn.commit()
    return results


def get_recent_facts(
    group_id: int,
    limit: int = 10,
    priority_user_ids: set[str] | None = None,
    priority_ratio: float = 0.7,
    query: str = "",
) -> list[dict]:
    """获取事实记忆，优先返回与当前对话对象及当前话题相关的条目。

    机器人在群里通常是回应某个人，而群记忆是整个群共享的。如果不加
    区分，回复 A 时可能被 B、C、D 的各种事实带偏。取样分三层：

    1) 与当前消息关键词相关的事实（无论属于谁）—— 保证"问什么答什么"；
    2) 属于 priority_user_ids（当前发言者、被 @ / 被回复的人）的事实，
       占 limit 的 priority_ratio；
    3) 剩余名额用群体的近期事实补齐，保留群氛围与共同背景。

    query 为空时不启用第 1 层，行为与旧版一致。
    """
    conn = _get_db()
    conn.row_factory = sqlite3.Row

    selected: list[dict] = []
    seen: set[int] = set()
    # 记录前多少条属于「当前对话对象」，排序时要保证它们不被挤掉
    priority_count = 0

    # 第 1 层：与当前话题相关的记忆，优先占位
    if query and limit > 0:
        relevant = search_relevant_facts(
            group_id,
            query,
            limit=max(1, limit // 2),
            user_id=next(iter(priority_user_ids), None) if priority_user_ids else None,
        )
        for item in relevant:
            if item["id"] in seen:
                continue
            selected.append(item)
            seen.add(item["id"])
        priority_count = len(selected)

    uids = {str(u) for u in (priority_user_ids or set()) if str(u).strip()}
    if uids and len(selected) < limit and priority_ratio > 0:
        quota = max(1, int(limit * max(0.0, min(1.0, priority_ratio))))
        placeholders = ",".join("?" for _ in uids)
        cur = conn.execute(
            f"""SELECT id, fact, user_id, created_at, access_count, importance
                FROM facts
                WHERE group_id = ? AND user_id IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?""",
            (group_id, *uids, quota),
        )
        for row in cur.fetchall():
            selected.append(dict(row))
            seen.add(row["id"])
        priority_count = len(selected)

    # 用群体事实补足剩余名额
    remaining = limit - len(selected)
    if remaining > 0:
        cur = conn.execute(
            """SELECT id, fact, user_id, created_at, access_count, importance
               FROM facts
               WHERE group_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (group_id, remaining + len(seen)),
        )
        for row in cur.fetchall():
            if row["id"] in seen:
                continue
            selected.append(dict(row))
            seen.add(row["id"])
            if len(selected) >= limit:
                break

    # 排序：优先组的条目必须留在前面，否则后面按时间截断时会被挤掉
    # （对话对象的事实通常较早，纯按时间排序正好会把它排到最后）。
    priority_ids = {f["id"] for f in selected[:priority_count]}
    priority_list = [f for f in selected if f["id"] in priority_ids]
    other_list = [f for f in selected if f["id"] not in priority_ids]
    priority_list.sort(key=lambda f: f.get("created_at", 0), reverse=True)
    other_list.sort(key=lambda f: f.get("created_at", 0), reverse=True)

    return (priority_list + other_list)[:limit]


def delete_fact(group_id: int, fact_id: int) -> bool:
    """删除一条事实记忆"""
    conn = _get_db()
    cur = conn.execute(
        "DELETE FROM facts WHERE group_id = ? AND id = ?",
        (group_id, fact_id),
    )
    conn.commit()
    return cur.rowcount > 0




def add_long_term_memory(
    group_id: int,
    content: str,
    created_by: str | None = None,
) -> bool:
    """添加长期记忆"""
    content = _normalize(content)
    if not content:
        return False

    conn = _get_db()
    try:
        conn.execute(
            """INSERT INTO long_term_memories (group_id, content, created_at, created_by)
               VALUES (?, ?, ?, ?)""",
            (group_id, content, int(time.time()), created_by),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def search_long_term_memories(
    group_id: int,
    query: str,
    limit: int = 8,
) -> list[dict]:
    """按关键词检索长期记忆，返回按相关度排序的结果。"""
    tokens = term_list(query, limit=12)
    if not tokens:
        return []

    conn = _get_db()
    conn.row_factory = sqlite3.Row
    clauses = " OR ".join("content LIKE ?" for _ in tokens)
    params = [group_id] + [f"%{tok}%" for tok in tokens]
    try:
        rows = conn.execute(
            f"""SELECT id, content, created_at, created_by
                FROM long_term_memories
                WHERE group_id = ? AND ({clauses})""",
            params,
        ).fetchall()
    except Exception as e:
        logger.debug(f"search_long_term_memories failed: {e}")
        return []

    scored = []
    for row in rows:
        hits = sum(1 for tok in tokens if tok in (row["content"] or ""))
        if hits:
            scored.append((hits, dict(row)))
    scored.sort(key=lambda x: (-x[0], -x[1].get("created_at", 0)))
    return [item for _, item in scored[:limit]]


def get_long_term_memories(
    group_id: int,
    limit: int | None = None,
    query: str = "",
) -> list[dict]:
    """获取长期记忆。

    limit 为 None 时返回全部；否则：
    - 若给了 query，先取与当前话题相关的若干条，再用最近的记忆补齐，
      这样积累的大量记忆都有机会被用到，而不是永远只用最新的几条；
    - 没有 query 时退化为「取最近 limit 条」。

    返回按时间正序，便于阅读。
    """
    conn = _get_db()
    conn.row_factory = sqlite3.Row

    if limit is None:
        cur = conn.execute(
            """SELECT id, content, created_at, created_by
               FROM long_term_memories
               WHERE group_id = ?
               ORDER BY created_at ASC""",
            (group_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    if query:
        picked: list[dict] = []
        seen: set[int] = set()
        # 相关记忆占约一半名额
        for item in search_long_term_memories(group_id, query, limit=max(1, limit // 2)):
            picked.append(item)
            seen.add(item["id"])
        # 余下用最近的补齐
        remaining = limit - len(picked)
        if remaining > 0:
            cur = conn.execute(
                """SELECT id, content, created_at, created_by
                   FROM long_term_memories
                   WHERE group_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (group_id, remaining + len(seen)),
            )
            for row in cur.fetchall():
                if row["id"] in seen:
                    continue
                picked.append(dict(row))
                seen.add(row["id"])
                if len(picked) >= limit:
                    break
        picked.sort(key=lambda m: m.get("created_at", 0))
        return picked[:limit]

    # 先按时间倒序取最近的 N 条，再翻转成正序
    cur = conn.execute(
        """SELECT id, content, created_at, created_by
           FROM long_term_memories
           WHERE group_id = ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (group_id, limit),
    )
    rows = [dict(row) for row in cur.fetchall()]
    rows.reverse()
    return rows


def delete_long_term_memory(group_id: int, memory_id: int) -> bool:
    """删除长期记忆"""
    conn = _get_db()
    cur = conn.execute(
        "DELETE FROM long_term_memories WHERE group_id = ? AND id = ?",
        (group_id, memory_id),
    )
    conn.commit()
    return cur.rowcount > 0




def set_user_impression(
    group_id: int,
    user_id: str,
    impression: str,
):
    """设置对用户的印象"""
    conn = _get_db()
    conn.execute(
        """INSERT OR REPLACE INTO user_impressions (group_id, user_id, impression, updated_at)
           VALUES (?, ?, ?, ?)""",
        (group_id, user_id, impression, int(time.time())),
    )
    conn.commit()


def get_user_impression(group_id: int, user_id: str) -> str | None:
    """获取对用户的印象"""
    conn = _get_db()
    cur = conn.execute(
        "SELECT impression FROM user_impressions WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def get_all_user_impressions(group_id: int) -> dict[str, str]:
    """获取所有用户印象"""
    conn = _get_db()
    cur = conn.execute(
        "SELECT user_id, impression FROM user_impressions WHERE group_id = ?",
        (group_id,),
    )
    return {row[0]: row[1] for row in cur.fetchall()}




_TS_PREFIX = re.compile(r"^\[?\d{2}-\d{2}\s+\d{2}:\d{2}\]?\s*")


def _strip_ts(text: str) -> str:
    """去掉内容开头已有的时间戳，避免与渲染时添加的重复。"""
    return _TS_PREFIX.sub("", (text or "").strip())


def build_memory_context(
    group_id: int,
    current_message: str,
    max_facts: int = 6,
    max_long_term: int = 12,
    priority_user_ids: set[str] | None = None,
    priority_ratio: float = 0.7,
) -> dict[str, object]:
    """构建记忆上下文，返回长期记忆文本、相关事实文本，以及本轮展示给
    模型的长期记忆 id 列表。

    priority_user_ids 用于把事实记忆的注意力集中在当前对话对象身上
    （通常是发言人 / 被 @ 的人 / 被回复的人），避免被其他群成员的信息带偏。

    long_term_ids 与 "long_term" 文本里的编号严格同序：第 k 条展示记忆对应
    long_term_ids[k-1]。模型删除记忆时输出的是它看到的序号，调用方据此把序
    号映射回真实 SQLite id，杜绝"按 JSON 插入序删错条目"的旧问题。
    """

    ltm = get_long_term_memories(group_id, limit=max_long_term, query=current_message)
    ltm_text = "\n".join(
        f"{i+1}. [{datetime.fromtimestamp(m['created_at']).strftime('%m-%d %H:%M')}] "
        f"{_strip_ts(m['content'])}"
        for i, m in enumerate(ltm)
    ) if ltm else ""

    # 中期记忆：先按当前话题检索相关事实，再按对话对象与群体补齐
    recent_facts = get_recent_facts(
        group_id,
        limit=max_facts,
        priority_user_ids=priority_user_ids,
        priority_ratio=priority_ratio,
        query=current_message,
    )
    facts_text = "\n".join(
        f"- [{datetime.fromtimestamp(f['created_at']).strftime('%m-%d %H:%M')}] "
        f"{_strip_ts(f['fact'])}"
        for f in recent_facts
    ) if recent_facts else ""

    return {
        "long_term": ltm_text,
        "facts": facts_text,
        "long_term_ids": [m["id"] for m in ltm],
    }




def migrate_from_old_memory_system():
    """从旧的 memories.json 迁移数据"""
    from astrabot.chat_service.memory_manager import _load_json, MEMORIES_PATH

    data = _load_json(MEMORIES_PATH)
    migrated_count = 0

    for key, value in data.items():
        if not key.startswith("group_"):
            continue

        try:
            group_id = int(key.replace("group_", ""))
        except ValueError:
            continue

        if isinstance(value, dict):

            memories = value.get("memories", [])
            for mem in memories:
                if add_long_term_memory(group_id, mem):
                    migrated_count += 1


            users = value.get("users", {})
            for user_id, impression in users.items():
                set_user_impression(group_id, user_id, impression)

    logger.info(f"Migrated {migrated_count} memories from old system")
