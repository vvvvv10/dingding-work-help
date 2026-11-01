"""MySQL 存储层（可选后端）。

本项目的 SQL 全部按 SQLite 的 `?` 占位符和写法编写，这里用一层薄适配器把差异吃掉，
避免为了换库去改上百处查询：

| SQLite 写法                        | MySQL 改写                                    |
|------------------------------------|-----------------------------------------------|
| `?`                                | `%s`                                          |
| `INSERT OR IGNORE INTO`            | `INSERT IGNORE INTO`                          |
| `ON CONFLICT(...) DO NOTHING`      | 转成 `INSERT IGNORE`，去掉后半段              |
| `ON CONFLICT(...) DO UPDATE SET`   | `ON DUPLICATE KEY UPDATE` + `excluded.x` → `VALUES(x)` |
| `chunks_fts` 的插入/删除           | 落到 `chunks.fts_body`（ngram 全文索引）       |

检索用 MySQL 的 ngram 全文索引替代 FTS5，中文按 2 字切分，和 SQLite 侧的 bigram 一致。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable, List, Optional, Sequence

DDL = [
    """
    CREATE TABLE IF NOT EXISTS scopes (
        `key`       VARCHAR(64) NOT NULL PRIMARY KEY,
        `name`      VARCHAR(191) NOT NULL,
        `description` VARCHAR(500) NULL,
        `created_at` BIGINT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS `groups` (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `conversation_id` VARCHAR(160) NOT NULL,
        `title` VARCHAR(191) NULL,
        `scope_key` VARCHAR(64) NOT NULL,
        `enabled` TINYINT NOT NULL DEFAULT 1,
        `created_at` BIGINT NOT NULL,
        UNIQUE KEY uk_groups_conv (`conversation_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        `staff_id` VARCHAR(160) NOT NULL PRIMARY KEY,
        `nickname` VARCHAR(191) NULL,
        `role` VARCHAR(32) NULL,
        `status` VARCHAR(32) NULL,
        `created_at` BIGINT NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS group_members (
        `conversation_id` VARCHAR(160) NOT NULL,
        `staff_id` VARCHAR(160) NOT NULL,
        `nickname` VARCHAR(191) NULL,
        `last_seen_at` BIGINT NOT NULL,
        PRIMARY KEY (`conversation_id`, `staff_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS grants (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `staff_id` VARCHAR(160) NOT NULL,
        `scope_key` VARCHAR(64) NOT NULL,
        `can_read` TINYINT NOT NULL DEFAULT 1,
        `can_write` TINYINT NOT NULL DEFAULT 0,
        `note` VARCHAR(500) NULL,
        `created_at` BIGINT NOT NULL,
        UNIQUE KEY uk_grants (`staff_id`, `scope_key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `msg_id` VARCHAR(160) NULL,
        `conversation_id` VARCHAR(160) NOT NULL,
        `scope_key` VARCHAR(64) NOT NULL,
        `sender_id` VARCHAR(160) NULL,
        `sender_nick` VARCHAR(191) NULL,
        `msg_type` VARCHAR(32) NULL,
        `content` MEDIUMTEXT NOT NULL,
        `created_at` BIGINT NOT NULL,
        `ingested_at` BIGINT NOT NULL,
        `doc_id` BIGINT NULL,
        UNIQUE KEY uk_msg (`msg_id`),
        KEY idx_messages_scope (`scope_key`, `created_at`),
        KEY idx_messages_conv (`conversation_id`, `created_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS docs (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `scope_key` VARCHAR(64) NOT NULL,
        `title` VARCHAR(255) NOT NULL,
        `content` MEDIUMTEXT NOT NULL,
        `tags` VARCHAR(255) NULL,
        `status` VARCHAR(16) NOT NULL DEFAULT 'pending',
        `source` VARCHAR(32) NULL,
        `source_conversation_id` VARCHAR(160) NULL,
        `source_msg_ids` VARCHAR(1000) NULL,
        `created_by` VARCHAR(160) NULL,
        `reviewed_by` VARCHAR(160) NULL,
        `created_at` BIGINT NOT NULL,
        `reviewed_at` BIGINT NULL,
        KEY idx_docs_scope (`scope_key`, `status`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `doc_id` BIGINT NOT NULL,
        `scope_key` VARCHAR(64) NOT NULL,
        `seq` INT NOT NULL,
        `text` MEDIUMTEXT NOT NULL,
        `vec` LONGBLOB NULL,
        `fts_body` MEDIUMTEXT NULL,
        KEY idx_chunks_doc (`doc_id`),
        KEY idx_chunks_scope (`scope_key`),
        FULLTEXT KEY ft_chunks (`fts_body`) WITH PARSER ngram
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        `id` BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        `ts` BIGINT NOT NULL,
        `actor` VARCHAR(160) NULL,
        `action` VARCHAR(64) NOT NULL,
        `target` VARCHAR(255) NULL,
        `detail` TEXT NULL,
        `allowed` TINYINT NOT NULL DEFAULT 1,
        KEY idx_audit_ts (`ts`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
]

TABLES = [
    "scopes",
    "groups",
    "users",
    "group_members",
    "grants",
    "messages",
    "docs",
    "chunks",
    "audit_log",
]

_ON_CONFLICT_UPDATE = re.compile(
    r"ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+UPDATE\s+SET\s+(.*?)(?=\s*\)?\s*$)",
    re.IGNORECASE | re.DOTALL,
)
_ON_CONFLICT_NOTHING = re.compile(r"ON\s+CONFLICT\s*\([^)]*\)\s+DO\s+NOTHING", re.IGNORECASE)
_EXCLUDED = re.compile(r"excluded\.(\w+)", re.IGNORECASE)

# MySQL 8.0 里 groups / key 都是保留字，SQLite 里不是。代码里到处是
# `FROM groups g`、`SELECT key FROM scopes`，逐个加引号要改几十处，统一在这一层补。
_RESERVED = ("groups", "key")
_RESERVED_RE = {w: re.compile(r"(?<![\w`])%s(?![\w`])" % w, re.IGNORECASE) for w in _RESERVED}
_SQL_STRING = re.compile(r"('(?:[^']|'')*')")


def _quote_reserved(sql: str) -> str:
    """给保留字标识符加反引号，只改字符串字面量之外的部分（别动 'groups' 这种取值）。"""
    parts = _SQL_STRING.split(sql)
    for i in range(0, len(parts), 2):  # 偶数下标是 SQL 本体，奇数下标是 '引号里的值'
        for word, pat in _RESERVED_RE.items():
            parts[i] = pat.sub("`%s`" % word, parts[i])
    return "".join(parts)


class Cursor:
    """把 pymysql 的游标包装成 sqlite3 游标的样子。"""

    def __init__(self, cur, rowcount_hint: Optional[int] = None):
        self._cur = cur
        self.rowcount = cur.rowcount if rowcount_hint is None else rowcount_hint

    @property
    def lastrowid(self) -> int:
        return self._cur.lastrowid or 0

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self) -> List[dict]:
        return list(self._cur.fetchall())

    def __iter__(self):
        return iter(self._cur)


class Connection:
    """sqlite3.Connection 的最小兼容层。"""

    def __init__(self, raw):
        self.raw = raw
        self.backend = "mysql"

    # ------------------------------------------------------------------
    def _run(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        rewritten, args = _translate(sql, params)
        cur = self.raw.cursor()
        if not rewritten.strip():
            # 被优化掉的语句（比如 SQLite 专有的 FTS 维护），直接当作空操作
            cur.close()

            class _Noop:
                rowcount = 0
                lastrowid = 0

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

                def __iter__(self):
                    return iter(())

            return _Noop()
        cur.execute(rewritten, args)
        return Cursor(cur)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        return self._run(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> Cursor:
        rewritten, _ = _translate(sql, ())
        cur = self.raw.cursor()
        cur.executemany(rewritten, list(seq))
        return Cursor(cur)

    def executescript(self, script: str) -> None:
        cur = self.raw.cursor()
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            cur.execute(stmt)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def cursor(self):
        return self.raw.cursor()


def _translate(sql: str, params: Sequence[Any]) -> tuple[str, tuple]:
    """把 SQLite 方言改写成 MySQL 方言，返回 (SQL, 参数)。"""
    body = sql.strip()

    # 1) chunks_fts：MySQL 侧没有 virutal table，索引直接挂在 chunks.fts_body 上，
    #    内容取 chunks.text 原文，由 ngram 分词器负责切分。
    low = body.lower()
    if low.startswith("delete from chunks_fts"):
        return "UPDATE chunks SET fts_body = '' WHERE id = %s", tuple(params)
    if low.startswith("insert into chunks_fts"):
        return "UPDATE chunks SET fts_body = `text` WHERE id = %s", (params[0],)

    # 2) INSERT OR IGNORE → INSERT IGNORE
    body = re.sub(r"^\s*INSERT\s+OR\s+IGNORE", "INSERT IGNORE", body, flags=re.IGNORECASE)
    body = re.sub(r"^\s*INSERT\s+OR\s+REPLACE", "REPLACE", body, flags=re.IGNORECASE)

    # 2.5) 保留字补引号（必须在生成 ON DUPLICATE KEY UPDATE 之前，否则 KEY 也会被加引号）
    body = _quote_reserved(body)

    # 3) ON CONFLICT ... DO NOTHING → INSERT IGNORE
    if _ON_CONFLICT_NOTHING.search(body):
        body = _ON_CONFLICT_NOTHING.sub("", body)
        body = re.sub(r"^\s*INSERT\s+", "INSERT IGNORE ", body, flags=re.IGNORECASE)

    # 4) ON CONFLICT (...) DO UPDATE SET ... → ON DUPLICATE KEY UPDATE ...=VALUES(...)
    m = _ON_CONFLICT_UPDATE.search(body)
    if m:
        sets = m.group(1).strip()
        sets = _EXCLUDED.sub(lambda x: "VALUES(%s)" % x.group(1), sets)
        body = body[: m.start()] + "ON DUPLICATE KEY UPDATE " + sets
        # 去掉可能被一起吞进来的右括号
        body = body.rstrip()
        while body.endswith(")") and body.count("(") < body.count(")"):
            body = body[:-1].rstrip()

    # 5) 占位符
    body = body.replace("?", "%s")
    return body, tuple(params)


def connect(cfg: dict):
    import pymysql
    import pymysql.cursors

    raw = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=cfg.get("timeout", 10),
    )
    return Connection(raw)


def create_database(cfg: dict) -> None:
    """建库（若不存在）。连接时不指定 database，避免库不存在连不上。"""
    import pymysql

    raw = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        charset="utf8mb4",
        connect_timeout=cfg.get("timeout", 10),
    )
    try:
        with raw.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4 "
                "COLLATE utf8mb4_unicode_ci" % cfg["database"]
            )
        raw.commit()
    finally:
        raw.close()


def init_db(conn: Connection) -> None:
    cur = conn.raw.cursor()
    for stmt in DDL:
        cur.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------- 迁移

SQLITE_TO_MYSQL = {
    # 表名: 列顺序（与 SQLite 一致，显式列名避免顺序依赖）
}


def migrate_from_sqlite(src: sqlite3.Connection, dst: Connection, batch: int = 500) -> dict:
    """把 SQLite 库整库搬到 MySQL（先清空目标表）。"""
    report = {}
    cur = dst.raw.cursor()
    for table in reversed(TABLES):
        cur.execute("DELETE FROM `%s`" % table)
    cur.execute("DELETE FROM chunks")
    dst.commit()

    for table in TABLES:
        src.row_factory = sqlite3.Row
        rows = src.execute("SELECT * FROM `%s`" % table).fetchall()
        if not rows:
            report[table] = 0
            continue
        cols = [d[0] for d in src.execute("SELECT * FROM `%s` LIMIT 0" % table).description]
        collist = ", ".join("`%s`" % c for c in cols)
        holders = ", ".join(["%s"] * len(cols))
        sql = "INSERT INTO `%s` (%s) VALUES (%s)" % (table, collist, holders)
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            cur.executemany(sql, [tuple(r[c] for c in cols) for r in chunk])
        # 自增列回填，避免后续插入撞 id
        if "id" in cols:
            cur.execute("SELECT MAX(id) AS m FROM `%s`" % table)
            maxid = (cur.fetchone() or {}).get("m") or 0
            if maxid:
                cur.execute("ALTER TABLE `%s` AUTO_INCREMENT = %d" % (table, int(maxid) + 1))
        report[table] = len(rows)

    # chunks.fts_body 回填：标题 + 标签 + 正文，ngram 索引才有内容（和 store.index_doc 保持一致）
    cur.execute(
        "UPDATE chunks c JOIN docs d ON d.id = c.doc_id "
        "SET c.fts_body = CONCAT(COALESCE(d.title, ''), ' ', COALESCE(d.tags, ''), ' ', c.`text`)"
    )
    dst.commit()
    return report
