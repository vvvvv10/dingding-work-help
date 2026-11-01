"""把群聊消息提炼成可检索的知识条目。

两条路：
- 配了大模型：让模型从这段聊天里抽出「结论型」知识（问答对/规则/决策），一条一份文档。
- 没配大模型：退化为「原始记录归档」，按时间窗口把消息整理成文档，一样能被检索到。
两条路都受同一套权限和审核流程约束。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from . import config, db, llm, store, text

SYSTEM_PROMPT = """你是企业知识库的知识提炼器。输入是一段钉钉群聊记录。
请只提炼「有长期复用价值」的内容，忽略寒暄、排班、无结论的讨论。

输出的每一段内容必须能独立被检索和引用，不要出现"上面""那个""他说"这类指代。

最重要的一条：只能使用聊天记录里出现过的事实。严禁补充聊天里没有的内容，
包括法规条文、文件编号、制度条款号、生效日期、人名、金额、系统名称。
聊天里没写依据，就不要写依据；聊天里说得含糊（"应该""大概"），
就在 content 里保留这种不确定性。信息够就写足，聊天本身很简短就写得短一点——
但绝不能为了凑长度而编。

返回严格的 JSON 对象：{"items": [{"title": "...", "content": "...", "tags": "..."}]}
- title：不超过 20 字，是知识点的标题
- content：结论、规则、步骤或结论性说明，一般 40-300 字，自包含
- tags：2-4 个关键词，用英文逗号分隔
没有可提炼内容时返回 {"items": []}"""


def _format_messages(rows: List[sqlite3.Row]) -> str:
    lines = []
    for r in rows:
        lines.append("[%s] %s：%s" % (text.fmt_ts(r["created_at"]), r["sender_nick"] or "成员", r["content"]))
    return "\n".join(lines)


def _fallback_digest(rows: List[sqlite3.Row], title_prefix: str = "群聊记录") -> Dict:
    """无大模型时的兜底：按窗口归档原始消息。"""
    if not rows:
        return {}
    first, last = rows[0]["created_at"], rows[-1]["created_at"]
    body = _format_messages(rows)
    return {
        "title": "%s %s" % (title_prefix, text.fmt_ts(first)[:10]),
        "content": "# %s ~ %s 共 %d 条消息\n\n%s"
        % (text.fmt_ts(first), text.fmt_ts(last), len(rows), body),
        "tags": "原始记录",
    }


def distill_scope(
    conn: sqlite3.Connection,
    scope_key: str,
    since_ms: int = 0,
    actor: str = "system",
    auto_approve: Optional[bool] = None,
) -> Dict:
    rows = store.pending_messages(conn, scope_key, since_ms=since_ms)
    if len(rows) < config.distill_min_messages():
        return {"created": 0, "reason": "待处理消息不足", "scope": scope_key}

    usable = [r for r in rows if not text.is_noise(text.clean_mention(r["content"]))]
    if len(usable) < config.distill_min_messages():
        return {"created": 0, "reason": "有效消息不足", "scope": scope_key}

    approve = config.auto_approve() if auto_approve is None else auto_approve
    status = "approved" if approve else "pending"
    created: List[int] = []
    conversation_id = usable[0]["conversation_id"]

    if llm.is_configured():
        snippet = _format_messages(usable)[:12000]
        raw = llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "群聊记录如下：\n\n" + snippet},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        items = llm.parse_json_array(raw or "")
        for item in items:
            title = str(item.get("title") or "").strip()
            content = str(item.get("content") or "").strip()
            if len(content) < 10:
                continue
            doc_id = store.create_doc(
                conn,
                scope_key=scope_key,
                title=title or content[:20],
                content=content,
                tags=str(item.get("tags") or ""),
                status=status,
                source="distill",
                source_conversation_id=conversation_id,
                source_msg_ids=[r["id"] for r in usable],
                created_by=actor,
            )
            created.append(doc_id)
        if created:
            store.mark_messages_used(conn, [r["id"] for r in usable], created[0])
    else:
        digest = _fallback_digest(usable)
        if digest:
            doc_id = store.create_doc(
                conn,
                scope_key=scope_key,
                title=digest["title"],
                content=digest["content"],
                tags=digest["tags"],
                status=status,
                source="archive",
                source_conversation_id=conversation_id,
                source_msg_ids=[r["id"] for r in usable],
                created_by=actor,
            )
            created.append(doc_id)
            store.mark_messages_used(conn, [r["id"] for r in usable], doc_id)

    db.audit(
        conn,
        "distill",
        actor=actor,
        target=scope_key,
        detail="messages=%d docs=%d status=%s" % (len(usable), len(created), status),
    )
    return {
        "scope": scope_key,
        "messages": len(usable),
        "created": len(created),
        "doc_ids": created,
        "status": status,
        "mode": "llm" if llm.is_configured() else "archive",
    }


def distill_all(conn: sqlite3.Connection, actor: str = "scheduler") -> List[Dict]:
    return [distill_scope(conn, key, actor=actor) for key in store.iter_scope_keys(conn)]
