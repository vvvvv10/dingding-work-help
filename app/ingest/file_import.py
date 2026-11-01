"""历史聊天记录导入：把导出的文件一次性灌进知识库。

支持的输入：
- JSON：数组，或 {"conversation_id":"...","title":"...","messages":[...]}
  每条消息字段兼容常见导出工具：content/text/msg，sender/sender_name/nick，
  createTime/timestamp/time/created_at，msgId/msg_id
- 纯文本：每行 "2026-10-01 10:00 张三：内容"，或 "【张三】内容"
时间解析不出来就按导入时间排。
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from .. import text

_TIME_PATTERNS = [
    ("%Y-%m-%d %H:%M:%S", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"),
    ("%Y-%m-%d %H:%M", r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"),
    ("%Y/%m/%d %H:%M:%S", r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"),
    ("%Y/%m/%d %H:%M", r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}"),
]
_LINE_RE = re.compile(
    r"^\[?(?P<ts>[\d/\-: ]{10,19})\]?\s*(?P<sender>[^:：\]\s]{1,24})\s*[:：\]]\s*(?P<content>.+)$"
)
_BRACKET_RE = re.compile(r"^【(?P<sender>[^】]{1,24})】\s*(?P<content>.+)$")


def _parse_ts(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if v > 10**11 else v * 1000
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v if v > 10**11 else v * 1000
    for fmt, _ in _TIME_PATTERNS:
        try:
            return int(time.mktime(time.strptime(s, fmt)) * 1000)
        except ValueError:
            continue
    return None


def _pick(d: Dict, *keys, default=""):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def parse_json(raw: str) -> List[Dict]:
    data = json.loads(raw)
    conversation_id = ""
    title = ""
    if isinstance(data, dict):
        conversation_id = str(_pick(data, "conversation_id", "chat_id", "openConversationId", "cid", default=""))
        title = str(_pick(data, "title", "conversation_title", "name", default=""))
        data = _pick(data, "messages", "items", "data", "list", default=[])
    if not isinstance(data, list):
        return []

    out: List[Dict] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        content = str(_pick(m, "content", "text", "msg", "message", default="")).strip()
        if not content:
            continue
        cid = str(_pick(m, "conversation_id", "chat_id", "openConversationId", "cid", default=conversation_id))
        out.append(
            {
                "conversation_id": cid or "import_unknown",
                "conversation_title": str(_pick(m, "conversation_title", "chat_title", default=title)),
                "content": content,
                "sender_id": str(_pick(m, "sender_id", "senderStaffId", "staffId", "sender", default="")),
                "sender_nick": str(_pick(m, "sender_nick", "sender_name", "nick", "senderName", default="")),
                "msg_id": str(_pick(m, "msg_id", "msgId", "id", default="")),
                "created_at": _parse_ts(_pick(m, "created_at", "createTime", "timestamp", "time", "ts", default=None)),
            }
        )
    return out


def parse_text(raw: str, conversation_id: str = "import_txt", title: str = "") -> List[Dict]:
    out: List[Dict] = []
    last_ts: Optional[int] = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if m:
            ts = _parse_ts(m.group("ts"))
            if ts:
                last_ts = ts
            out.append(
                {
                    "conversation_id": conversation_id,
                    "conversation_title": title,
                    "content": m.group("content").strip(),
                    "sender_nick": m.group("sender").strip(),
                    "created_at": last_ts,
                }
            )
            continue
        m2 = _BRACKET_RE.match(line)
        if m2:
            out.append(
                {
                    "conversation_id": conversation_id,
                    "conversation_title": title,
                    "content": m2.group("content").strip(),
                    "sender_nick": m2.group("sender").strip(),
                    "created_at": last_ts,
                }
            )
            continue
        if out:
            out[-1]["content"] += "\n" + line
    return out


def parse_csv(raw: str, conversation_id: str = "import_csv") -> List[Dict]:
    reader = csv.DictReader(io.StringIO(raw))
    items: List[Dict] = []
    for row in reader:
        content = (row.get("content") or row.get("text") or "").strip()
        if not content:
            continue
        items.append(
            {
                "conversation_id": row.get("conversation_id") or conversation_id,
                "conversation_title": row.get("conversation_title", ""),
                "content": content,
                "sender_id": row.get("sender_id", ""),
                "sender_nick": row.get("sender_nick") or row.get("sender", ""),
                "created_at": _parse_ts(row.get("created_at") or row.get("time")),
            }
        )
    return items


def load_file(path: Path, conversation_id: str = "", title: str = "") -> List[Dict]:
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return parse_json(raw)
    if suffix == ".csv":
        return parse_csv(raw, conversation_id or "import_csv")
    return parse_text(raw, conversation_id or "import_txt", title)
