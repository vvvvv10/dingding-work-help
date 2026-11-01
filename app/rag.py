"""问答主链路：权限判定 → 检索 → 作答 → 附来源。

这一层是安全和体验的交汇点，所有对外入口（钉钉、后台、HTTP API）都必须走这里。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import acl, config, db, llm, store, text

SYSTEM_PROMPT = """你是企业内部知识库助手。必须遵守：
1. 只依据【参考资料】作答，不得用通用常识补充事实；资料里没有的，就说没有。
2. 每条关键结论句末标注来源编号，例如 [1]、[2]。
3. 资料不足或互相冲突时，直接说明"知识库里没有明确说法"或指出冲突，不要猜。
4. 中文作答，200-400 字，必要时用短列表；不要复述问题，不要解释你自己是谁。
5. 标了「待审核」的资料尚未确认，只能作为参考线索，引用时要明确提示这一点。"""


@dataclass
class Answer:
    text: str
    refused: bool = False
    reason: str = ""
    citations: List[Dict] = field(default_factory=list)
    scopes: List[str] = field(default_factory=list)
    mode: str = "llm"

    def to_dict(self) -> Dict:
        return {
            "text": self.text,
            "refused": self.refused,
            "reason": self.reason,
            "citations": self.citations,
            "scopes": self.scopes,
            "mode": self.mode,
        }


def _group_title(conn: sqlite3.Connection, conversation_id: str) -> str:
    if not conversation_id:
        return ""
    row = conn.execute(
        "SELECT title FROM groups WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    return (row["title"] if row else "") or ""


def resolve_scopes(
    conn: sqlite3.Connection, principal: acl.Principal, conversation_id: Optional[str] = None
) -> Set[str]:
    """确定本次问答的检索范围。

    普通用户：群内问答严格收窄到该群绑定的知识域。
    超级管理员：不收敛，始终可跨全部知识域检索。
    """
    if principal.is_super:
        return set(principal.scopes)
    if conversation_id:
        group = store.get_group(conn, conversation_id)
        if group is not None:
            if not group["enabled"]:
                return set()
            scope = group["scope_key"]
            if principal.can_read(scope):
                return {scope}
            return set()
    return set(principal.scopes)


def ask(
    conn: sqlite3.Connection,
    staff_id: str,
    question: str,
    conversation_id: Optional[str] = None,
    nickname: str = "",
) -> Answer:
    question = (question or "").strip()
    principal = acl.resolve(conn, staff_id, nickname)

    if not question:
        return Answer(text="请问你想了解什么？", refused=True, reason="空问题", mode="meta")
    if principal.blocked:
        acl.log_deny(conn, principal, "ask", conversation_id or "", principal.reason)
        return Answer(text="你的账号已被禁用，无法查询知识库。", refused=True, reason=principal.reason, mode="meta")

    group = store.get_group(conn, conversation_id) if conversation_id else None
    if group is not None and not principal.can_read(group["scope_key"]):
        acl.log_deny(
            conn,
            principal,
            "ask",
            group["scope_key"],
            "用户在群 %s 中无该知识域权限" % (group["title"] or conversation_id),
        )
        return Answer(
            text="你没有这个群的知识访问权限，本次提问已被拒绝并记录。",
            refused=True,
            reason="越权访问",
            scopes=[],
            mode="meta",
        )

    scopes = resolve_scopes(conn, principal, conversation_id)
    if not scopes:
        acl.log_deny(conn, principal, "ask", conversation_id or "", principal.reason or "无可用知识域")
        hint = principal.reason or "你当前没有可访问的知识域"
        return Answer(
            text="暂时无法为你检索：%s。\n请联系管理员把你加入对应的钉钉群，或单独授权知识域。" % hint,
            refused=True,
            reason=hint,
            scopes=[],
            mode="meta",
        )

    hits = store.search(
        conn,
        question,
        scopes,
        k=config.top_k(),
        include_pending=principal.is_super,
    )
    db.audit(
        conn,
        "ask",
        actor=staff_id,
        target=",".join(sorted(scopes)),
        detail="q=%s hits=%d" % (question[:120], len(hits)),
    )

    if not hits:
        return Answer(
            text="知识库里没有检索到相关内容。可以换个说法，或者在群里 @我 用 /沉淀 把相关讨论整理进知识库。",
            refused=False,
            reason="无命中",
            scopes=sorted(scopes),
            mode="empty",
        )

    if not llm.is_configured():
        return _snippet_answer(hits, scopes)

    context = _build_context(hits)
    try:
        raw = llm.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "【参考资料】\n%s\n\n【问题】%s" % (context, question)},
            ],
            temperature=0.2,
            max_tokens=900,
        )
    except llm.LLMError as e:
        ans = _snippet_answer(hits, scopes)
        ans.text = "（模型暂时不可用，先给你原文片段）\n\n" + ans.text
        ans.reason = str(e)
        return ans

    body = (raw or "").strip()
    if not body:
        return _snippet_answer(hits, scopes)

    ans = Answer(text=body, scopes=sorted(scopes), mode="llm")
    ans.citations = _citations(body, hits)
    ans.text = render(body, ans.citations, conn)
    return ans


def _build_context(hits: List[Dict]) -> str:
    parts = []
    for i, h in enumerate(hits, 1):
        when = text.fmt_ts(h["created_at"]) if h.get("created_at") else ""
        # 待审核文档只有超级管理员能检索到，作答时要显式标明，避免把未确认内容当结论
        flag = "｜待审核" if h.get("status") == "pending" else ""
        parts.append(
            "[%d] 《%s》（群：%s｜%s%s）\n%s"
            % (i, h["title"], h.get("conversation_id") or "未知", when, flag, h["text"][:600])
        )
    return "\n\n".join(parts)


_CITE_RE = re.compile(r"\[(\d+)\]")


def _citations(body: str, hits: List[Dict]) -> List[Dict]:
    used = sorted({int(m) for m in _CITE_RE.findall(body)})
    pairs = [(n, hits[n - 1]) for n in used if 0 < n <= len(hits)]
    if not pairs:
        # 模型没标编号时退回前三条，编号按它们在上下文里的位置算
        pairs = [(i, h) for i, h in enumerate(hits[:3], 1)]
    out, seen = [], set()
    for n, h in pairs:
        key = h["chunk_id"]
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                # 保留正文里的原始编号，否则正文写 [2]、来源列表却从 1 开始，对不上
                "n": n,
                "title": h["title"],
                "doc_id": h["doc_id"],
                "conversation_id": h.get("conversation_id", ""),
                "snippet": h["text"][:160].replace("\n", " "),
                "scope": h["scope_key"],
                "status": h.get("status", "approved"),
            }
        )
    return out


def render(body: str, citations: List[Dict], conn: Optional[sqlite3.Connection] = None) -> str:
    lines = [body.strip(), "", "---", "**来源**"]
    for i, c in enumerate(citations, 1):
        gname = _group_title(conn, c["conversation_id"]) if conn else ""
        where = " · ".join(x for x in [gname, c.get("scope", "")] if x)
        if c.get("status") == "pending":
            where = (where + " · 待审核").lstrip(" · ")
        lines.append(
            "%d. 《%s》%s" % (c.get("n") or i, c["title"], "（%s）" % where if where else "")
        )
    out = "\n".join(lines)
    limit = config.max_reply_chars()
    return out if len(out) <= limit else out[: limit - 20] + "\n…（内容过长已截断）"


def _snippet_answer(hits: List[Dict], scopes: Set[str]) -> Answer:
    lines = ["下面是和你的问题最接近的原文片段：", ""]
    citations = []
    for i, h in enumerate(hits[:3], 1):
        tag = "（待审核，仅供参考）" if h.get("status") == "pending" else ""
        lines.append("**[%d] 《%s》**%s" % (i, h["title"], tag))
        lines.append("> " + h["text"][:300].replace("\n", "\n> "))
        lines.append("")
        citations.append(
            {
                "title": h["title"],
                "doc_id": h["doc_id"],
                "conversation_id": h.get("conversation_id", ""),
                "snippet": h["text"][:160],
                "scope": h["scope_key"],
                "status": h.get("status", "approved"),
            }
        )
    return Answer(text="\n".join(lines).strip(), citations=citations, scopes=sorted(scopes), mode="snippet")
