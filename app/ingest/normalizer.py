"""消息接入的统一入口。

三条来源，最终都收敛到 ingest_message()：
1. 钉钉 Stream 机器人（实时，能收到 @机器人 的消息）
2. 聊天记录导出文件（一次性地把历史群消息灌进来）
3. 会话内容存档（企业开通后由官方接口推送，需另配适配器）

无论哪条来源，入库时都必须绑定到一个「知识域」——默认一群一域，
管理员可以在后台把多个群合并到同一个知识域。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Dict, Optional

from .. import config, db, store, text


def default_scope_key(conversation_id: str) -> str:
    digest = hashlib.sha1(conversation_id.encode("utf-8")).hexdigest()[:10]
    return "chat_%s" % digest


def ensure_group(
    conn: sqlite3.Connection, conversation_id: str, title: str = "", scope_key: Optional[str] = None
) -> str:
    group = store.get_group(conn, conversation_id)
    if group is not None:
        if title and title != group["title"]:
            store.bind_group(conn, conversation_id, title, group["scope_key"], bool(group["enabled"]))
        return group["scope_key"]

    key = scope_key or default_scope_key(conversation_id)
    store.bind_group(conn, conversation_id, title or conversation_id, key)
    db.audit(conn, "group.bind", actor="system", target=key, detail=title or conversation_id)
    return key


def ingest_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    content: str,
    sender_id: str = "",
    sender_nick: str = "",
    msg_id: str = "",
    msg_type: str = "text",
    created_at: Optional[int] = None,
    conversation_title: str = "",
    scope_key: Optional[str] = None,
) -> Dict:
    if not conversation_id:
        return {"ok": False, "reason": "缺少会话 ID"}
    if text.is_noise(content):
        return {"ok": False, "reason": "噪声消息"}

    key = ensure_group(conn, conversation_id, conversation_title, scope_key)
    if sender_id:
        store.touch_member(conn, conversation_id, sender_id, sender_nick)
        store.upsert_user(conn, sender_id, sender_nick)

    clean = text.clean_mention(content)
    if config.redact_secrets():
        clean = text.redact_secrets(clean)

    is_new = store.record_message(
        conn,
        conversation_id=conversation_id,
        scope_key=key,
        content=clean,
        sender_id=sender_id,
        sender_nick=sender_nick,
        msg_id=msg_id,
        msg_type=msg_type,
        created_at=created_at,
    )
    return {"ok": True, "is_new": is_new, "scope": key, "conversation_id": conversation_id}


def ingest_batch(conn: sqlite3.Connection, items) -> Dict:
    ok = skipped = dup = 0
    for it in items:
        r = ingest_message(conn, **it)
        if not r.get("ok"):
            skipped += 1
        elif r.get("is_new"):
            ok += 1
        else:
            dup += 1
    return {"inserted": ok, "duplicated": dup, "skipped": skipped, "total": len(items)}
