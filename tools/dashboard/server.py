"""AstraBot 本地运维面板：只读数据，零依赖（stdlib http.server）。

用法：.venv-win\\Scripts\\python.exe tools/dashboard/server.py --port 8000
数据源：astrabot/chat_service/data/{logs/*.log, usage.jsonl, stickers/}。
只读——不写日志、不碰 history.db，也不影响正在运行的 bot。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import socket
import time
from collections import defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "astrabot" / "chat_service" / "data"
STATIC = Path(__file__).resolve().parent
TAIL_BYTES = 32 * 1024 * 1024
STICKER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} ")
# 消息保真：与 message_handler 里生成的 [ctx] 行保持同一格式
CTX_RE = re.compile(
    r"\[ctx\] g=(\d+) base=(\d+) extra=(\d+)\(quote=(\d+),speaker=(\d+)\) "
    r"quote=(\w+) chars=(\d+)( trimmed)?"
)


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _newest_log() -> Path | None:
    files = sorted(DATA.glob("logs/astrabot_*.log"))
    return files[-1] if files else None


def _tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        chunk = f.read()
    lines = chunk.decode("utf-8", "replace").splitlines()
    # 从字节中间切入时第一行是残句，丢掉
    return lines[1:] if size > max_bytes and lines else lines


def _split_log(line: str) -> tuple[str, str, str]:
    """loguru 行 -> (时间, 级别, 正文)。

    格式是 `时间 | 级别 | 模块:函数:行号 - 消息`，只有两个 " | "；正文里可能
    再出现 " | "（比如 nonebot 的 `OneBot V11 | Bot ... connected`），所以
    必须 maxsplit=2，不能按四个段去拆。
    """
    parts = line.split(" | ", 2)
    if len(parts) == 3 and TS_RE.match(parts[0]):
        return parts[0][:19], parts[1].strip(), parts[2]
    return "", "", line


def _load_usage() -> list[dict]:
    path = DATA / "usage.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(e, dict) or not isinstance(e.get("ts"), (int, float)):
            continue
        rows.append(e)
    return rows


def _usage_summary(rows: list[dict]) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "in": 0, "out": 0})
    by_kind: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    by_day: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    today_stats = {"calls": 0, "cost": 0.0}

    for e in rows:
        day = datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d")
        cost = float(e.get("cost_usd") or 0.0)
        model = str(e.get("model") or "?")
        kind = str(e.get("kind") or "?")
        for bucket in (by_model[model],):
            bucket["calls"] += 1
            bucket["cost"] += cost
            bucket["in"] += int(e.get("prompt_tokens") or 0)
            bucket["out"] += int(e.get("completion_tokens") or 0)
        by_kind[kind]["calls"] += 1
        by_kind[kind]["cost"] += cost
        by_day[day]["calls"] += 1
        by_day[day]["cost"] += cost
        if day == today:
            today_stats["calls"] += 1
            today_stats["cost"] += cost

    def rows_of(d: dict, key: str) -> list[dict]:
        out = [{key: k, **v} for k, v in d.items()]
        out.sort(key=lambda x: x["cost"], reverse=True)
        return [dict(r, cost=round(r["cost"], 6)) for r in out]

    # 连续 14 个自然日：没有用量的日子补 0，否则横轴会跳日、看着像数据缺失
    days = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    recent = []
    for e in sorted(rows, key=lambda x: x["ts"], reverse=True)[:12]:
        recent.append({
            "time": datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M:%S"),
            "kind": e.get("kind"),
            "model": e.get("model"),
            "in": int(e.get("prompt_tokens") or 0),
            "out": int(e.get("completion_tokens") or 0),
            "cache": int(e.get("cache_hit_tokens") or 0),
            "cost": round(float(e.get("cost_usd") or 0.0), 6),
        })
    return {
        "total_calls": len(rows),
        "total_cost": round(sum(float(e.get("cost_usd") or 0.0) for e in rows), 6),
        "today": today_stats["calls"],
        "today_cost": round(today_stats["cost"], 6),
        "by_model": rows_of(by_model, "model"),
        "by_kind": rows_of(by_kind, "kind"),
        "by_day": [
            {"day": d, "calls": by_day.get(d, {}).get("calls", 0), "cost": round(by_day.get(d, {}).get("cost", 0.0), 6)}
            for d in days
        ],
        "recent": recent,
    }


def _log_summary(path: Path | None) -> dict:
    info = {"file": None, "mtime": None, "age_s": None, "connected": None,
            "truncated": False, "replies": [], "errors": [], "reply_count": 0,
            "ctx": {"turns": 0, "triggered": 0, "quote_hit_id": 0, "quote_hit_text": 0,
                    "quote_miss": 0, "trimmed": 0, "extra_rows": 0, "chars_last": None}}
    if path is None:
        return info
    size = path.stat().st_size
    info["file"] = path.name
    info["mtime"] = int(path.stat().st_mtime)
    info["age_s"] = int(time.time() - path.stat().st_mtime)
    info["truncated"] = size > TAIL_BYTES

    replies, errors = [], []
    connected: bool | None = None
    ctx = info["ctx"]
    for line in _tail_lines(path):
        ts, level, body = _split_log(line)
        body = body.strip()
        m = CTX_RE.search(body)
        if m:
            ctx["turns"] += 1
            extra = int(m.group(3))
            ctx["extra_rows"] += extra
            if extra:
                ctx["triggered"] += 1
            state = m.group(6)
            key = {"hit_id": "quote_hit_id", "hit_text": "quote_hit_text", "miss": "quote_miss"}.get(state)
            if key:
                ctx[key] += 1
            if m.group(8):
                ctx["trimmed"] += 1
            ctx["chars_last"] = int(m.group(7))
            continue
        if " - Replied to group " in body:
            tail = body.split(" - Replied to group ", 1)[1]
            gid, _, text = tail.partition(": ")
            replies.append({"time": ts, "group": gid, "text": text[:400]})
        elif "Bot " in body and body.endswith("connected"):
            connected = True
        elif "WebSocket Closed" in body or "Error while setup websocket" in body:
            connected = False
        if level in ("ERROR", "WARNING"):
            errors.append({"time": ts, "level": level, "text": body[:260]})
    info["connected"] = connected
    info["reply_count"] = len(replies)
    info["replies"] = replies[-20:][::-1]
    info["errors"] = errors[-14:][::-1]
    return info


def _sticker_summary(limit: int = 30) -> dict:
    index_path = DATA / "stickers" / "index.json"
    if not index_path.exists():
        return {"count": 0, "top": []}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"count": 0, "top": []}
    if not isinstance(index, dict):
        return {"count": 0, "top": []}
    items = []
    for sid, meta in index.items():
        if not isinstance(meta, dict):
            continue
        items.append({
            "id": sid,
            "file": meta.get("file"),
            "uses": int(meta.get("uses") or 0),
            "desc": (meta.get("description") or "").strip(),
        })
    items.sort(key=lambda x: (-x["uses"], x["id"]))
    return {"count": len(items), "top": items[:limit]}


def build_summary() -> dict:
    log_path = _newest_log()
    return {
        "generated_at": int(time.time()),
        "status": {
            "bot_up": _port_open(8080),
            "bridge_up": _port_open(3001),
        },
        "log": _log_summary(log_path),
        "usage": _usage_summary(_load_usage()),
        "stickers": _sticker_summary(),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AstraDashboard/1.0"

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/style.css":
                self._send(200, (STATIC / "style.css").read_bytes(), "text/css; charset=utf-8")
            elif path == "/app.js":
                self._send(200, (STATIC / "app.js").read_bytes(), "text/javascript; charset=utf-8")
            elif path == "/api/summary":
                body = json.dumps(build_summary(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            elif path.startswith("/api/sticker/"):
                self._send_sticker(path.rsplit("/", 1)[-1])
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as exc:  # 面板自身异常不该把进程带死
            self._send(500, f"dashboard error: {exc}".encode("utf-8"), "text/plain; charset=utf-8")

    def _send_sticker(self, sid: str) -> None:
        if not STICKER_ID_RE.match(sid):
            self._send(400, b"bad id", "text/plain; charset=utf-8")
            return
        index_path = DATA / "stickers" / "index.json"
        try:
            meta = json.loads(index_path.read_text(encoding="utf-8")).get(sid) or {}
        except (json.JSONDecodeError, OSError):
            meta = {}
        filename = meta.get("file") or ""
        target = (DATA / "stickers" / filename).resolve()
        # 只允许停留在表情库目录内，防止索引里出现奇怪路径时被读出去
        if not filename or DATA.joinpath("stickers").resolve() not in target.parents or not target.is_file():
            self._send(404, b"no such sticker", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype, cache="max-age=3600")

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="AstraBot 本地运维面板")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"AstraBot dashboard: http://{args.host}:{args.port}  (Ctrl+C 结束)")
    print(f"data: {DATA}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
