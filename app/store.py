"""知识库读写与混合检索。"""
from __future__ import annotations

import json
import sqlite3
from typing import Dict, Iterable, List, Optional, Sequence, Set

from . import config, db, embedder as emb, text

DEFAULT_SCOPE_DESC = "由钉钉群消息沉淀的知识域"
# 纯向量召回时，允许比本问题最好结果低多少；0.85 表示只能差 15%
VEC_REL_RATIO = 0.85


# ---------------------------------------------------------------- 知识域 / 群

def ensure_scope(conn: sqlite3.Connection, key: str, name: str = "", desc: str = "") -> str:
    db.ensure_scope(conn, key, name, desc or DEFAULT_SCOPE_DESC)
    # 若知识域名称还只是 key 本身，用更可读的名字覆盖（例如群名）
    if name and name != key:
        conn.execute(
            "UPDATE scopes SET name = ? WHERE key = ? AND (name = '' OR name = key)", (name, key)
        )
        conn.commit()
    return key


def bind_group(
    conn: sqlite3.Connection, conversation_id: str, title: str, scope_key: str, enabled: bool = True
) -> None:
    ensure_scope(conn, scope_key, name=title)
    conn.execute(
        """
        INSERT INTO groups (conversation_id, title, scope_key, enabled, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            title = excluded.title, scope_key = excluded.scope_key, enabled = excluded.enabled
        """,
        (conversation_id, title, scope_key, 1 if enabled else 0, db.now_ms()),
    )
    conn.commit()


def get_group(conn: sqlite3.Connection, conversation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM groups WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()


def list_groups(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        """
        SELECT g.*,
               (SELECT COUNT(*) FROM docs d WHERE d.scope_key = g.scope_key AND d.status='approved') AS docs,
               (SELECT COUNT(*) FROM docs d WHERE d.scope_key = g.scope_key AND d.status='pending') AS pending,
               (SELECT COUNT(*) FROM messages m WHERE m.scope_key = g.scope_key) AS msgs
        FROM groups g ORDER BY g.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_user(conn: sqlite3.Connection, staff_id: str, nickname: str = "", role: str = "member") -> None:
    if not staff_id:
        return
    conn.execute(
        """
        INSERT INTO users (staff_id, nickname, role, status, created_at)
        VALUES (?,?,?, 'active', ?)
        ON CONFLICT(staff_id) DO UPDATE SET
            nickname = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE users.nickname END
        """,
        (staff_id, nickname, role, db.now_ms()),
    )
    conn.commit()


def add_grant(
    conn: sqlite3.Connection,
    staff_id: str,
    scope_key: str,
    can_read: bool = True,
    can_write: bool = False,
    note: str = "",
) -> None:
    """单独给某人开某个知识域（跨群授权）。"""
    if not (staff_id and scope_key):
        return
    conn.execute(
        """
        INSERT INTO grants (staff_id, scope_key, can_read, can_write, note, created_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(staff_id, scope_key) DO UPDATE SET
            can_read = excluded.can_read, can_write = excluded.can_write, note = excluded.note
        """,
        (staff_id, scope_key, 1 if can_read else 0, 1 if can_write else 0, note, db.now_ms()),
    )
    conn.commit()


def touch_member(
    conn: sqlite3.Connection, conversation_id: str, staff_id: str, nickname: str = ""
) -> None:
    if not (conversation_id and staff_id):
        return
    conn.execute(
        """
        INSERT INTO group_members (conversation_id, staff_id, nickname, last_seen_at)
        VALUES (?,?,?,?)
        ON CONFLICT(conversation_id, staff_id) DO UPDATE SET
            nickname = CASE WHEN excluded.nickname != '' THEN excluded.nickname ELSE group_members.nickname END,
            last_seen_at = excluded.last_seen_at
        """,
        (conversation_id, staff_id, nickname, db.now_ms()),
    )
    conn.commit()


# ---------------------------------------------------------------- 消息

def record_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    scope_key: str,
    content: str,
    sender_id: str = "",
    sender_nick: str = "",
    msg_id: str = "",
    msg_type: str = "text",
    created_at: Optional[int] = None,
) -> bool:
    """写入一条群消息，按 msg_id 去重。返回是否为新消息。"""
    if not content or not content.strip():
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (msg_id, conversation_id, scope_key, sender_id, sender_nick, msg_type, content, created_at, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            msg_id or None,
            conversation_id,
            scope_key,
            sender_id,
            sender_nick,
            msg_type,
            content.strip(),
            created_at or db.now_ms(),
            db.now_ms(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def pending_messages(
    conn: sqlite3.Connection, scope_key: str, since_ms: int = 0, limit: int = 400
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM messages
        WHERE scope_key = ? AND doc_id IS NULL AND created_at >= ?
        ORDER BY created_at ASC LIMIT ?
        """,
        (scope_key, since_ms, limit),
    ).fetchall()


def mark_messages_used(conn: sqlite3.Connection, msg_ids: Sequence[int], doc_id: int) -> None:
    if not msg_ids:
        return
    conn.executemany(
        "UPDATE messages SET doc_id = ? WHERE id = ?", [(doc_id, mid) for mid in msg_ids]
    )
    conn.commit()


# ---------------------------------------------------------------- 文档

def create_doc(
    conn: sqlite3.Connection,
    scope_key: str,
    title: str,
    content: str,
    tags: str = "",
    status: str = "pending",
    source: str = "distill",
    source_conversation_id: str = "",
    source_msg_ids: Sequence[int] = (),
    created_by: str = "",
) -> int:
    content = (content or "").strip()
    if not content:
        raise ValueError("知识内容为空")
    cur = conn.execute(
        """
        INSERT INTO docs (scope_key, title, content, tags, status, source,
                          source_conversation_id, source_msg_ids, created_by, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scope_key,
            title.strip() or content[:24],
            content,
            tags,
            status,
            source,
            source_conversation_id,
            ",".join(str(i) for i in source_msg_ids),
            created_by,
            db.now_ms(),
        ),
    )
    doc_id = int(cur.lastrowid)
    conn.commit()
    # 待审核文档也建索引：检索时用 d.status 过滤，
    # 这样超级管理员勾上 include_pending 才能真的看到待审核内容。
    index_doc(conn, doc_id)
    return doc_id


def index_doc(conn: sqlite3.Connection, doc_id: int) -> int:
    """为文档建立切片与索引（重复调用会先清理旧切片）。"""
    row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return 0

    old = conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()
    for r in old:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["id"],))
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    pieces = text.chunk_text(row["content"])
    if not pieces:
        return 0

    use_vec = emb.get_embedder().enabled
    vecs: Optional[List[List[float]]] = None
    if use_vec:
        vecs = emb.get_embedder().embed(pieces)
        if vecs is None or len(vecs) != len(pieces):
            vecs = None

    # 标题和标签也进索引：群聊里提炼出来的文档，正文常常只有过程，
    # 关键结论词往往只出现在标题上（如「数据库慢查询排查顺序」），不索引标题会漏召回。
    head_tokens = text.tokenize(" ".join(x for x in [row["title"], row["tags"]] if x))

    count = 0
    for i, piece in enumerate(pieces):
        blob = emb.pack(vecs[i]) if vecs else None
        cur = conn.execute(
            "INSERT INTO chunks (doc_id, scope_key, seq, text, vec) VALUES (?,?,?,?,?)",
            (doc_id, row["scope_key"], i, piece, blob),
        )
        cid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO chunks_fts (rowid, body) VALUES (?, ?)",
            (cid, " ".join(head_tokens + text.tokenize(piece))),
        )
        if db.is_mysql(conn):
            # MySQL 的 ngram 索引直接建在 chunks.fts_body 上，只索引了正文。
            # SQLite 那边是把 head_tokens 塞进 FTS 表，MySQL 这边没有对应的通路，
            # 所以这里把标题和标签以原文形式拼到正文前面，否则「结论只在标题里」
            # 的文档（如「官网产品卡片命名与图标规范」）会搜不到。
            conn.execute(
                "UPDATE chunks SET fts_body = CONCAT(?, ' ', `text`) WHERE id = ?",
                (" ".join(x for x in [row["title"], row["tags"]] if x), cid),
            )
        count += 1
    conn.commit()
    return count


def reindex_all(conn: sqlite3.Connection) -> int:
    """重建全部已生效文档的切片与索引（改了分词/索引策略后调用）。"""
    rows = conn.execute(
        "SELECT id FROM docs WHERE status IN ('approved', 'pending')"
    ).fetchall()
    total = 0
    for r in rows:
        total += index_doc(conn, r["id"])
    return total


def set_doc_status(
    conn: sqlite3.Connection, doc_id: int, status: str, reviewer: str = ""
) -> bool:
    if status not in {"pending", "approved", "rejected"}:
        raise ValueError("非法状态")
    conn.execute(
        "UPDATE docs SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
        (status, reviewer, db.now_ms(), doc_id),
    )
    conn.commit()
    if status in {"approved", "pending"}:
        index_doc(conn, doc_id)
    elif status == "rejected":
        rows = conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall()
        for r in rows:
            conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["id"],))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.commit()
    return True


def list_docs(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    scope_key: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    sql = "SELECT * FROM docs WHERE 1=1"
    args: List = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if scope_key:
        sql += " AND scope_key = ?"
        args.append(scope_key)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_doc(conn: sqlite3.Connection, doc_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- 检索

def _vector_hits(
    conn: sqlite3.Connection, query: str, scopes: Set[str], limit: int, include_pending: bool = False
) -> List[int]:
    if not emb.get_embedder().enabled:
        return []
    qv = emb.get_embedder().embed([query])
    if not qv:
        return []
    qvec = qv[0]
    ph = ",".join("?" * len(scopes))
    status = "" if include_pending else "AND d.status = 'approved'"
    rows = conn.execute(
        f"""
        SELECT c.id, c.vec FROM chunks c
        JOIN docs d ON d.id = c.doc_id
        WHERE 1=1 {status} AND c.vec IS NOT NULL AND c.scope_key IN ({ph})
        """,
        list(scopes),
    ).fetchall()
    scored = []
    for r in rows:
        s = emb.cosine(qvec, emb.unpack(r["vec"]))
        if s > 0.15:
            scored.append((s, r["id"]))
    scored.sort(reverse=True)
    return [(cid, s) for s, cid in scored[:limit]]


def _lexical_mysql(
    conn, query: str, ph: str, scope_args: List[str], status_sql: str, limit: int
) -> List[int]:
    """MySQL 关键词召回：ngram 全文索引负责召回，bigram 命中负责排序。

    ngram 的 MATCH 相关度在多字中文查询上会退化成 0（例如「登录页」命中 5 条但
    分数全是 0），直接 ORDER BY 相关度等于随机排序，所以这里只把它当召回器，
    排序改用和 SQLite 侧完全一致的 bigram 打分，两边检索结果才基本对齐。
    """
    try:
        rows = conn.execute(
            f"""
            SELECT c.id AS cid, c.fts_body AS body
            FROM chunks c
            JOIN docs d ON d.id = c.doc_id
            WHERE MATCH(c.fts_body) AGAINST (? IN NATURAL LANGUAGE MODE)
                  {status_sql} AND c.scope_key IN ({ph})
            LIMIT ?
            """,
            [query, *scope_args, max(limit * 4, 60)],
        ).fetchall()
    except Exception:  # noqa: BLE001 —— 索引缺失或语法不支持时退化为纯向量检索
        return []

    q_tokens = set(text.tokenize(query))
    if not q_tokens:
        return [r["cid"] for r in rows[:limit]]
    scored = []
    for r in rows:
        hits = len(q_tokens & set(text.tokenize(r["body"] or "")))
        scored.append((-hits, r["cid"]))
    scored.sort()
    return [cid for _, cid in scored[:limit]]


def search(
    conn: sqlite3.Connection,
    query: str,
    scopes: Set[str],
    k: int = 6,
    include_pending: bool = False,
) -> List[Dict]:
    """在允许的知识域内做混合检索（BM25 + 可选向量，RRF 融合）。

    scopes 为空时直接返回空，绝不回退到全库检索。
    include_pending 只给超级管理员用，普通用户永远搜不到未审核内容。
    """
    query = (query or "").strip()
    if not query or not scopes:
        return []

    ph = ",".join("?" * len(scopes))
    scope_args = list(scopes)
    status_sql = "" if include_pending else "AND d.status = 'approved'"

    lex_ids: List[int] = []
    if db.is_mysql(conn):
        lex_ids = _lexical_mysql(conn, query, ph, scope_args, status_sql, max(k * 5, 20))
    else:
        q = text.fts_query(query)
        if q:
            try:
                rows = conn.execute(
                    f"""
                    SELECT chunks_fts.rowid AS cid
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    JOIN docs d ON d.id = c.doc_id
                    WHERE chunks_fts MATCH ? {status_sql} AND c.scope_key IN ({ph})
                    ORDER BY bm25(chunks_fts) ASC
                    LIMIT ?
                    """,
                    [q, *scope_args, max(k * 5, 20)],
                ).fetchall()
                lex_ids = [r["cid"] for r in rows]
            except sqlite3.OperationalError:
                lex_ids = []

    vec_hits = _vector_hits(conn, query, scopes, max(k * 5, 20), include_pending)
    vec_ids = [cid for cid, _ in vec_hits]
    sims = {cid: s for cid, s in vec_hits}
    # 纯向量召回的门槛：既要过绝对下限，也要跟本问题的最好结果比一比，
    # 因为不同问题的相似度基线不一样（向量模型对任何中文都容易给 0.5+）。
    rel_cut = max(config.vec_min_sim(), max(sims.values(), default=0.0) * VEC_REL_RATIO)

    fused: Dict[int, float] = {}
    for ranked in (lex_ids, vec_ids):
        for i, cid in enumerate(ranked):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (60 + i)

    if not fused:
        return []

    ordered = sorted(fused.items(), key=lambda kv: -kv[1])[: max(k * 3, 12)]
    ids = [cid for cid, _ in ordered]
    ph2 = ",".join("?" * len(ids))
    detail = conn.execute(
        f"""
        SELECT c.id, c.text, c.doc_id, c.scope_key, d.title, d.tags,
               d.status AS doc_status, d.source_conversation_id, d.created_at
        FROM chunks c JOIN docs d ON d.id = c.doc_id
        WHERE c.id IN ({ph2})
        """,
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in detail}

    q_tokens = set(text.tokenize(query))
    results: List[Dict] = []
    for cid, base in ordered:
        r = by_id.get(cid)
        if r is None:
            continue
        cover = sum(1 for t in q_tokens if t in r["text"])
        # 关键词零命中的片段只能靠向量进来，这时要过门槛：
        # 只靠相对排序会把无关文档一起带进上下文，污染答案。
        if cover == 0 and sims.get(cid, 0.0) < rel_cut:
            continue
        score = fused.get(cid, 0.0) * 100 + cover * 0.6
        results.append(
            {
                "chunk_id": r["id"],
                "doc_id": r["doc_id"],
                "title": r["title"],
                "text": r["text"],
                "scope_key": r["scope_key"],
                "status": r["doc_status"],
                "conversation_id": r["source_conversation_id"],
                "tags": r["tags"],
                "created_at": r["created_at"],
                "score": round(score, 3),
            }
        )
    results.sort(key=lambda x: -x["score"])
    return results[:k]


# ---------------------------------------------------------------- 原始消息兜底检索

# 关键词预筛最多用几个 token、最多捞多少行走 Python 打分。
# 已经按 scope 收窄过，再加这两个上限，库涨到十万条也不会把一次提问拖成秒级。
_RAW_MAX_TERMS = 6
_RAW_MAX_SCAN = 800


def search_messages(
    conn: sqlite3.Connection,
    query: str,
    scopes: Set[str],
    k: int = 4,
) -> List[Dict]:
    """在群聊原始消息里找答案，给「还没被提炼成卡片」的内容兜底。

    只在 store.search() 零命中时调用。消息表没有全文索引（也没必要为它建一个），
    所以先用 LIKE 做粗筛把候选压到几百行，再用和文档检索同一套 bigram 打分排序，
    这样两边的相关性口径是一致的。

    刻意不过滤 doc_id：一条消息虽被某篇文档收走，但那篇文档可能讲的是别的事，
    兜底阶段要的是召回而不是纯度，噪声交给命中数门槛卡。
    """
    query = (query or "").strip()
    if not query or not scopes:
        return []

    q_tokens = set(text.tokenize(query))
    if not q_tokens:
        return []
    # 单字 token（"的""是"这种）做 LIKE 几乎命中全表，过滤掉更划算
    uniq = [t for t in dict.fromkeys(text.tokenize(query)) if len(t) > 1]
    if not uniq:
        return []
    # 长问题只取头尾各 3 个 token：只取开头会把结尾的关键词（"端口"这种）漏掉，
    # 而 LIKE 是 OR，少一个词只影响召回、不影响正确性。
    terms = uniq if len(uniq) <= _RAW_MAX_TERMS else list(dict.fromkeys(uniq[:3] + uniq[-3:]))

    ph = ",".join("?" * len(scopes))
    like = " OR ".join(["m.content LIKE ?"] * len(terms))
    rows = conn.execute(
        f"""
        SELECT m.id, m.msg_id, m.conversation_id, m.scope_key, m.sender_id,
               m.sender_nick, m.content, m.created_at
        FROM messages m
        WHERE m.scope_key IN ({ph}) AND ({like})
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        [*scopes, *["%" + t + "%" for t in terms], _RAW_MAX_SCAN],
    ).fetchall()

    # 查询很短时（"退货"这种），只命中一个 bigram 就是全部命中了，门槛跟着降
    need = 1 if len(q_tokens) <= 2 else 2
    scored: List[Dict] = []
    for r in rows:
        content = r["content"] or ""
        if text.is_noise(content):
            continue
        hits = len(q_tokens & set(text.tokenize(content)))
        if hits < need:
            continue
        scored.append(
            {
                "msg_id": r["msg_id"] or "",
                "conversation_id": r["conversation_id"],
                "scope_key": r["scope_key"],
                "sender_id": r["sender_id"] or "",
                "sender_nick": r["sender_nick"] or "",
                "text": content,
                "created_at": r["created_at"],
                # 命中数优先；同样命中时短消息更可能是"一句话答案"，排前面
                "score": round(hits - len(content) / 2000.0, 3),
            }
        )
    scored.sort(key=lambda x: (-x["score"], -x["created_at"]))
    return scored[:k]


# ---------------------------------------------------------------- 统计

def stats(conn: sqlite3.Connection) -> Dict:
    def one(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0] or 0)

    return {
        "scopes": one("SELECT COUNT(*) FROM scopes"),
        "groups": one("SELECT COUNT(*) FROM groups"),
        "messages": one("SELECT COUNT(*) FROM messages"),
        "messages_unused": one("SELECT COUNT(*) FROM messages WHERE doc_id IS NULL"),
        "docs_total": one("SELECT COUNT(*) FROM docs"),
        "docs_approved": one("SELECT COUNT(*) FROM docs WHERE status='approved'"),
        "docs_pending": one("SELECT COUNT(*) FROM docs WHERE status='pending'"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "users": one("SELECT COUNT(*) FROM users"),
        "denied": one("SELECT COUNT(*) FROM audit_log WHERE allowed=0"),
    }


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def iter_scope_keys(conn: sqlite3.Connection) -> Iterable[str]:
    for r in conn.execute("SELECT key FROM scopes ORDER BY key").fetchall():
        yield r["key"]
