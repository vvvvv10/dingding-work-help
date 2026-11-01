"""权限层：把「钉钉身份」翻译成「可读的知识域集合」。

设计原则（很重要）：
1. 权限只在这里判断，检索与问答不得自行放宽 —— 所有查询都必须先拿到 scopes。
2. 默认拒绝：解析不出任何知识域时返回空集，而不是返回全部。
3. 每一次判定都写审计日志，便于事后回查"谁在什么时候问了哪一块知识"。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Set

from . import config, db

ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"
ROLE_GUEST = "guest"
ROLE_UNKNOWN = "unknown"

PRIVILEGED = {ROLE_SUPERADMIN, ROLE_ADMIN}


@dataclass
class Principal:
    staff_id: str
    nickname: str = ""
    role: str = ROLE_UNKNOWN
    scopes: Set[str] = field(default_factory=set)
    blocked: bool = False
    reason: str = ""

    @property
    def is_admin(self) -> bool:
        return self.role in PRIVILEGED

    @property
    def is_super(self) -> bool:
        """超级管理员：全部知识域 + 不受群隔离 + 可看待审内容。"""
        return self.role == ROLE_SUPERADMIN

    def can_read(self, scope_key: str) -> bool:
        return self.is_super or scope_key in self.scopes


def _all_scope_keys(conn: sqlite3.Connection) -> Set[str]:
    rows = conn.execute("SELECT key FROM scopes").fetchall()
    return {r["key"] for r in rows}


def resolve(conn: sqlite3.Connection, staff_id: str, nickname: str = "") -> Principal:
    staff_id = (staff_id or "").strip()
    if not staff_id:
        return Principal(staff_id="", role=ROLE_UNKNOWN, reason="缺少用户标识")

    row = conn.execute("SELECT * FROM users WHERE staff_id = ?", (staff_id,)).fetchone()
    if row is None:
        principal = Principal(staff_id=staff_id, nickname=nickname, role=ROLE_UNKNOWN)
        if config.acl_deny_guest():
            principal.reason = "未登记用户，已按策略拒绝"
            return principal
        # 未登记但可能已经在某个群里被动记录过成员身份
        principal.scopes = _scopes_from_membership(conn, staff_id)
        if not principal.scopes:
            principal.reason = "不在任何已授权群内"
        return principal

    if row["status"] == "blocked":
        return Principal(
            staff_id=staff_id,
            nickname=row["nickname"] or nickname,
            role=row["role"],
            blocked=True,
            reason="账号已被禁用",
        )

    principal = Principal(staff_id=staff_id, nickname=row["nickname"] or nickname, role=row["role"])
    if principal.role in PRIVILEGED:
        principal.scopes = _all_scope_keys(conn)
        return principal

    if principal.role == ROLE_GUEST and config.acl_deny_guest():
        principal.reason = "外部联系人无内部知识权限"
        return principal

    scopes = _scopes_from_membership(conn, staff_id)
    scopes |= _scopes_from_grants(conn, staff_id)
    principal.scopes = scopes
    if not scopes:
        principal.reason = "未被分配任何知识域"
    return principal


def _scopes_from_membership(conn: sqlite3.Connection, staff_id: str) -> Set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT g.scope_key
        FROM group_members m
        JOIN groups g ON g.conversation_id = m.conversation_id
        WHERE m.staff_id = ? AND g.enabled = 1
        """,
        (staff_id,),
    ).fetchall()
    return {r["scope_key"] for r in rows}


def _scopes_from_grants(conn: sqlite3.Connection, staff_id: str) -> Set[str]:
    rows = conn.execute(
        "SELECT scope_key FROM grants WHERE staff_id = ? AND can_read = 1", (staff_id,)
    ).fetchall()
    return {r["scope_key"] for r in rows}


def check_violation(conn: sqlite3.Connection, principal: Principal, asked_scope: str) -> bool:
    """用户试图在某个群里问答，但该群绑定的知识域不在他可读范围内。"""
    if not asked_scope:
        return False
    return not principal.can_read(asked_scope)


def log_deny(conn: sqlite3.Connection, principal: Principal, action: str, target: str, detail: str) -> None:
    db.audit(conn, action, actor=principal.staff_id, target=target, detail=detail, allowed=False)


def intersection(allowed: Set[str], requested: Iterable[str]) -> Set[str]:
    return {s for s in requested if s in allowed}
