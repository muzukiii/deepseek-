"""聊天历史管理：基于 SQLite 存储群消息，支持上下文选取、裁剪和从 JSONL 迁移"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "history.db"
_local = threading.local()  # 每线程独立的 SQLite 连接，避免多线程问题
_db_initialized = False


def _get_db() -> sqlite3.Connection:
    global _db_initialized
    if not hasattr(_local, "conn") or _local.conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH))
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _init_db(_local.conn)
        if not _db_initialized:
            _db_initialized = True
            _migrate_from_jsonl()
    return _local.conn


def _init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            time INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            msg_type TEXT NOT NULL DEFAULT 'user',
            images TEXT,
            segments TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_group_time ON messages(group_id, time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_group_mid ON messages(group_id, message_id)")
    conn.execute("PRAGMA table_info(messages)")


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("images"):
        d["images"] = json.loads(d["images"])
    if d.get("segments"):
        d["segments"] = json.loads(d["segments"])
    return d


async def append_message(group_id: int, record: dict):
    conn = _get_db()
    images_json = json.dumps(record.get("images") or [], ensure_ascii=False) if record.get("images") else None
    segments_json = json.dumps(record.get("segments") or [], ensure_ascii=False) if record.get("segments") else None
    conn.execute(
        """INSERT INTO messages (group_id, time, message_id, user_id, user_name, message, msg_type, images, segments)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            group_id,
            record.get("time", 0),
            record.get("message_id", 0),
            record.get("user_id", ""),
            record.get("user_name", ""),
            record.get("message", ""),
            record.get("msg_type", "user"),
            images_json,
            segments_json,
        ),
    )
    conn.commit()


async def set_image_descriptions(group_id: int, updates: dict[int, dict]) -> int:
    """按 message_id 回写图片描述。

    updates: {message_id: {"seg_idx": desc, ...}}
    图片 base64 不落库，但描述很小且有长期价值，存下来后同一张图
    不必重复识别。返回实际更新的行数。
    """
    if not updates:
        return 0
    conn = _get_db()
    changed = 0
    for message_id, seg_descs in updates.items():
        row = conn.execute(
            "SELECT id, segments FROM messages WHERE group_id = ? AND message_id = ?",
            (group_id, message_id),
        ).fetchone()
        if not row or not row["segments"]:
            continue
        try:
            segs = json.loads(row["segments"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(segs, list):
            continue
        touched = False
        for seg_idx, desc in seg_descs.items():
            if 0 <= seg_idx < len(segs) and isinstance(segs[seg_idx], dict):
                if segs[seg_idx].get("type") == "image":
                    segs[seg_idx]["desc"] = desc
                    touched = True
        if touched:
            conn.execute(
                "UPDATE messages SET segments = ? WHERE id = ?",
                (json.dumps(segs, ensure_ascii=False), row["id"]),
            )
            changed += 1
    if changed:
        conn.commit()
    return changed


async def get_recent_messages(group_id: int, count: int = 15) -> list[dict]:
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM messages WHERE group_id = ? ORDER BY time DESC, id DESC LIMIT ?",
        (group_id, count),
    )
    rows = cur.fetchall()
    return [_row_to_dict(r) for r in reversed(rows)]


async def trim_history(group_id: int, max_lines: int = 1000):
    conn = _get_db()
    # 只裁原始消息。摘要行的 time 落在很久之前，一旦被"最近 N 条"裁掉，压缩成果和水位线
    # 会一起消失，导致那批消息被反复重新压缩。
    conn.execute(
        """DELETE FROM messages WHERE group_id = ? AND msg_type <> ?
           AND id NOT IN (
               SELECT id FROM messages WHERE group_id = ? AND msg_type <> ?
               ORDER BY time DESC, id DESC LIMIT ?
           )""",
        (group_id, MID_TERM_TYPE, group_id, MID_TERM_TYPE, max_lines),
    )
    conn.commit()


async def delete_by_message_id(group_id: int, message_id: int) -> bool:
    conn = _get_db()
    cur = conn.execute(
        "DELETE FROM messages WHERE group_id = ? AND message_id = ?",
        (group_id, message_id),
    )
    conn.commit()
    return cur.rowcount > 0


async def get_history_since(group_id: int, since_time: int) -> list[dict]:
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM messages WHERE group_id = ? AND time >= ? ORDER BY time ASC, id ASC",
        (group_id, since_time),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


async def get_messages_by_type(group_id: int, msg_type: str, limit: int = 50) -> list[dict]:
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM messages WHERE group_id = ? AND msg_type = ? ORDER BY time DESC, id DESC LIMIT ?",
        (group_id, msg_type, limit),
    )
    return [_row_to_dict(r) for r in reversed(cur.fetchall())]


# ---------------------------------------------------------------------------
# 前情摘要（msg_type='mid_term_memory'）：select_context 早已把这类行 pinned 进
# 上下文，但此前全仓库没有任何写入方，等于一条空通道。下面补齐读写。
# 约定：每个群只保留一条滚动摘要；message_id 复用为"已压缩到的 messages.id 水位线"，
# time 用该批最后一条消息的时间，使摘要按时间自然落在逐字历史之前。
# ---------------------------------------------------------------------------

MID_TERM_TYPE = "mid_term_memory"


async def get_mid_term(group_id: int) -> tuple[str, int]:
    """返回 (当前摘要文本, 已压缩水位线 messages.id)；没有摘要时为 ("", 0)。"""
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT message, message_id FROM messages WHERE group_id = ? AND msg_type = ?"
        " ORDER BY id DESC LIMIT 1",
        (group_id, MID_TERM_TYPE),
    ).fetchone()
    if not row:
        return "", 0
    return row["message"] or "", int(row["message_id"] or 0)


async def set_mid_term_memory(group_id: int, text: str, watermark: int, when: int) -> None:
    """用新的滚动摘要替换旧的那一条（单条覆盖，保证 pinned 体积不随时间增长）。"""
    conn = _get_db()
    conn.execute(
        "DELETE FROM messages WHERE group_id = ? AND msg_type = ?",
        (group_id, MID_TERM_TYPE),
    )
    conn.execute(
        """INSERT INTO messages (group_id, time, message_id, user_id, user_name, message, msg_type)
           VALUES (?, ?, ?, '', '前情提要', ?, ?)""",
        (group_id, when, watermark, text, MID_TERM_TYPE),
    )
    conn.commit()


async def context_window_boundary(group_id: int, keep_messages: int) -> int | None:
    """最近窗口里最旧那条 user 消息的 id；比它更早的消息才有压缩资格。

    返回 None 表示窗口还没被填满（或群里只有机器人自己的话），此时不该压缩。
    """
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id FROM messages WHERE group_id = ? AND msg_type = 'user'"
        " ORDER BY time DESC, id DESC LIMIT 1 OFFSET ?",
        (group_id, max(keep_messages - 1, 0)),
    ).fetchone()
    return int(row["id"]) if row else None


async def count_compressible(group_id: int, after_id: int, before_id: int) -> int:
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE group_id = ? AND msg_type IN ('user','bot')"
        " AND id > ? AND id < ?",
        (group_id, after_id, before_id),
    ).fetchone()
    return int(row["n"]) if row else 0


async def get_compressible(group_id: int, after_id: int, before_id: int, limit: int) -> list[dict]:
    """取一批待压缩的旧消息（同一群内 id 随时间递增，故 id 区间即时间区间）。"""
    conn = _get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT * FROM messages WHERE group_id = ? AND msg_type IN ('user','bot')"
        " AND id > ? AND id < ? ORDER BY time ASC, id ASC LIMIT ?",
        (group_id, after_id, before_id, limit),
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


async def select_context(group_id: int, max_messages: int = 15, exclude_message_id: int | None = None) -> list[dict]:
    """
    选取进入 prompt 的上下文消息。

    规则：
    - msg_type='mid_term_memory' 的消息始终保留（pinned）
    - 普通消息（user/bot）从新到旧取，最多 max_messages 条
    - bot 消息不计入 max_messages 限制
    - 按时间排序返回
    """
    conn = _get_db()
    conn.row_factory = sqlite3.Row

    pinned = conn.execute(
        "SELECT * FROM messages WHERE group_id = ? AND msg_type = 'mid_term_memory' ORDER BY time ASC",
        (group_id,),
    ).fetchall()

    normal = conn.execute(
        """SELECT * FROM messages WHERE group_id = ? AND msg_type IN ('user', 'bot')
           ORDER BY time DESC, id DESC LIMIT ?""",
        (group_id, max_messages * 2),  # 取 2 倍数量，因为 bot 消息不计入限制，实际只保留 max_messages 条 user 消息
    ).fetchall()

    seen = {r["id"] for r in pinned}
    selected = []
    counted = 0
    for r in normal:
        if r["id"] in seen:
            continue
        selected.append(r)
        if r["msg_type"] == "user":
            counted += 1
            if counted >= max_messages:
                break

    combined = list(pinned) + list(reversed(selected))
    if exclude_message_id is not None:
        combined = [r for r in combined if r["message_id"] != exclude_message_id]
    combined.sort(key=lambda r: (r["time"], r["id"]))
    return [_row_to_dict(r) for r in combined]




def _migrate_from_jsonl():
    """将旧 JSONL 数据导入 SQLite。"""
    history_dir = DATA_DIR / "history"
    if not history_dir.exists():
        return
    conn = _get_db()
    for f in sorted(history_dir.glob("*.jsonl")):
        group_id = int(f.stem)
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                rec.setdefault("msg_type", "user")
                images_json = json.dumps(rec.get("images") or [], ensure_ascii=False) if rec.get("images") else None
                segments_json = json.dumps(rec.get("segments") or [], ensure_ascii=False) if rec.get("segments") else None
                conn.execute(
                    """INSERT OR IGNORE INTO messages (group_id, time, message_id, user_id, user_name, message, msg_type, images, segments)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id,
                        rec.get("time", 0),
                        rec.get("message_id", 0),
                        rec.get("user_id", ""),
                        rec.get("user_name", ""),
                        rec.get("message", ""),
                        rec.get("msg_type", "user"),
                        images_json,
                        segments_json,
                    ),
                )
        conn.commit()


HISTORY_DIR = DATA_DIR
