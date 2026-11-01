"""SQLite 存储层：连接、建表、通用工具。"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scopes (
    key         TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT UNIQUE NOT NULL,
    title           TEXT DEFAULT '',
    scope_key       TEXT NOT NULL,
    enabled         INTEGER DEFAULT 1,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    staff_id   TEXT PRIMARY KEY,
    nickname   TEXT DEFAULT '',
    role       TEXT DEFAULT 'member',
    status     TEXT DEFAULT 'active',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    conversation_id TEXT NOT NULL,
    staff_id        TEXT NOT NULL,
    nickname        TEXT DEFAULT '',
    last_seen_at    INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, staff_id)
);

CREATE TABLE IF NOT EXISTS grants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id   TEXT NOT NULL,
    scope_key  TEXT NOT NULL,
    can_read   INTEGER DEFAULT 1,
    can_write  INTEGER DEFAULT 0,
    note       TEXT DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE (staff_id, scope_key)
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id          TEXT UNIQUE,
    conversation_id TEXT NOT NULL,
    scope_key       TEXT NOT NULL,
    sender_id       TEXT DEFAULT '',
    sender_nick     TEXT DEFAULT '',
    msg_type        TEXT DEFAULT 'text',
    content         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    ingested_at     INTEGER NOT NULL,
    doc_id          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_scope ON messages (scope_key, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, created_at);

CREATE TABLE IF NOT EXISTS docs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key              TEXT NOT NULL,
    title                  TEXT NOT NULL,
    content                TEXT NOT NULL,
    tags                   TEXT DEFAULT '',
    status                 TEXT NOT NULL DEFAULT 'pending',
    source                 TEXT DEFAULT 'distill',
    source_conversation_id TEXT DEFAULT '',
    source_msg_ids         TEXT DEFAULT '',
    created_by             TEXT DEFAULT '',
    reviewed_by            TEXT DEFAULT '',
    created_at             INTEGER NOT NULL,
    reviewed_at            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_docs_scope ON docs (scope_key, status);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL,
    scope_key TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    vec       BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks (scope_key);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body, tokenize='unicode61');

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    actor   TEXT DEFAULT '',
    action  TEXT NOT NULL,
    target  TEXT DEFAULT '',
    detail  TEXT DEFAULT '',
    allowed INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: Optional[Path] = None):
    """按 KB_BACKEND 返回 SQLite 或 MySQL 连接。两者对外接口一致。"""
    if config.backend() == "mysql" and path is None:
        from . import db_mysql

        cfg = config.mysql_config()
        if cfg is None:
            raise RuntimeError(
                "KB_BACKEND=mysql 但 MYSQL_HOST / MYSQL_DATABASE 没配齐，检查 .env"
            )
        return db_mysql.connect(cfg)

    p = Path(path) if path else config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn) -> None:
    if getattr(conn, "backend", "sqlite") == "mysql":
        from . import db_mysql

        db_mysql.init_db(conn)
        return
    conn.executescript(SCHEMA)
    conn.commit()


def get_db():
    conn = connect()
    init_db(conn)
    return conn


def is_mysql(conn) -> bool:
    return getattr(conn, "backend", "sqlite") == "mysql"


def audit(
    conn: sqlite3.Connection,
    action: str,
    actor: str = "",
    target: str = "",
    detail: str = "",
    allowed: bool = True,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, detail, allowed) VALUES (?,?,?,?,?,?)",
        (now_ms(), actor, action, target, detail[:2000], 1 if allowed else 0),
    )
    conn.commit()


def ensure_scope(conn: sqlite3.Connection, key: str, name: str = "", description: str = "") -> str:
    row = conn.execute("SELECT key FROM scopes WHERE key = ?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO scopes (key, name, description, created_at) VALUES (?,?,?,?)",
            (key, name or key, description, now_ms()),
        )
        conn.commit()
    return key
