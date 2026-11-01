"""零依赖 HTTP 服务：管理后台 API + 静态页面。

用标准库 http.server，服务器上不需要装任何东西就能跑起来。
所有 /api/* 都需要 KB_ADMIN_TOKEN，前端把 token 存在 localStorage 里。
"""
from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import acl, config, db, distiller, ingest, rag, store
from .ingest import file_import

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

Routes = Dict[str, Callable[..., Tuple[int, Any]]]


def _csv(v: str) -> list:
    return [x for x in (v or "").replace("，", ",").split(",") if x.strip()]


def _dws_available() -> bool:
    """只看命令是否装了，不去 shell 调用，避免后台首页变慢。"""
    try:
        from .ingest import dws

        return dws.available()
    except Exception:  # noqa: BLE001
        return False


class Api:
    """把路由和业务逻辑分开，方便直接单测。"""

    def __init__(self) -> None:
        self.routes: Routes = {
            ("GET", "/api/overview"): self.overview,
            ("GET", "/api/groups"): self.groups,
            ("POST", "/api/groups"): self.bind_group,
            ("GET", "/api/scopes"): self.scopes,
            ("GET", "/api/docs"): self.docs,
            ("POST", "/api/docs"): self.create_doc,
            ("POST", "/api/docs/status"): self.doc_status,
            ("POST", "/api/docs/delete"): self.delete_doc,
            ("GET", "/api/grants"): self.grants,
            ("POST", "/api/grants"): self.add_grant,
            ("POST", "/api/grants/delete"): self.del_grant,
            ("GET", "/api/users"): self.users,
            ("POST", "/api/users"): self.add_user,
            ("POST", "/api/users/role"): self.set_role,
            ("POST", "/api/ask"): self.ask,
            ("POST", "/api/search"): self.search,
            ("POST", "/api/distill"): self.distill,
            ("POST", "/api/ingest"): self.ingest,
            ("GET", "/api/audit"): self.audit,
            ("GET", "/api/identities"): self.identities,
        }

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _conn() -> sqlite3.Connection:
        return db.get_db()

    # ------------------------------------------------------------ handlers
    def overview(self, q: Dict[str, list], body: Dict) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            data = store.stats(conn)
            data["llm"] = bool(config.llm_config())
            data["embedding"] = bool(config.embedding_config())
            data["acl_require_membership"] = config.acl_require_membership()
            data["auto_approve"] = config.auto_approve()
            data["dingtalk"] = config.dingtalk_credentials() is not None
            data["dws"] = {
                "available": _dws_available(),
                "event_kind": config.dws_event_kind(),
            }
            data["scopes"] = [dict(r) for r in conn.execute("SELECT * FROM scopes ORDER BY key")]
            return 200, data
        finally:
            conn.close()

    def groups(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            return 200, {"items": store.list_groups(conn)}
        finally:
            conn.close()

    def bind_group(self, q, body) -> Tuple[int, Any]:
        cid = (body.get("conversation_id") or "").strip()
        if not cid:
            return 400, {"error": "conversation_id 必填"}
        conn = self._conn()
        try:
            title = (body.get("title") or cid).strip()
            scope = (body.get("scope_key") or "").strip()
            if scope:
                store.ensure_scope(conn, scope)
                store.bind_group(conn, cid, title, scope, bool(body.get("enabled", True)))
            else:
                ingest.ensure_group(conn, cid, title)
            db.audit(conn, "group.bind", actor="admin", target=scope or cid, detail=title)
            return 200, {"ok": True}
        finally:
            conn.close()

    def scopes(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT s.*,
                  (SELECT COUNT(*) FROM groups g WHERE g.scope_key = s.key) AS groups,
                  (SELECT COUNT(*) FROM docs d WHERE d.scope_key = s.key AND d.status='approved') AS docs,
                  (SELECT COUNT(*) FROM grants gr WHERE gr.scope_key = s.key AND gr.can_read=1) AS grants
                FROM scopes s ORDER BY s.key
                """
            ).fetchall()
            return 200, {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    def docs(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            status = (q.get("status", [""])[0] or "").strip() or None
            scope = (q.get("scope", [""])[0] or "").strip() or None
            return 200, {"items": store.list_docs(conn, status=status, scope_key=scope, limit=200)}
        finally:
            conn.close()

    def create_doc(self, q, body) -> Tuple[int, Any]:
        scope = (body.get("scope_key") or "").strip()
        content = (body.get("content") or "").strip()
        if not (scope and content):
            return 400, {"error": "scope_key 与 content 必填"}
        conn = self._conn()
        try:
            store.ensure_scope(conn, scope)
            doc_id = store.create_doc(
                conn,
                scope_key=scope,
                title=(body.get("title") or content[:20]).strip(),
                content=content,
                tags=body.get("tags", ""),
                status=body.get("status", "approved"),
                source="manual",
                created_by="admin",
            )
            db.audit(conn, "doc.create", actor="admin", target=str(doc_id), detail=scope)
            return 200, {"ok": True, "doc_id": doc_id}
        finally:
            conn.close()

    def doc_status(self, q, body) -> Tuple[int, Any]:
        try:
            doc_id = int(body.get("id"))
        except (TypeError, ValueError):
            return 400, {"error": "id 必填"}
        status = body.get("status", "approved")
        conn = self._conn()
        try:
            store.set_doc_status(conn, doc_id, status, reviewer="admin")
            db.audit(conn, "doc.%s" % status, actor="admin", target=str(doc_id))
            return 200, {"ok": True}
        finally:
            conn.close()

    def delete_doc(self, q, body) -> Tuple[int, Any]:
        try:
            doc_id = int(body.get("id"))
        except (TypeError, ValueError):
            return 400, {"error": "id 必填"}
        conn = self._conn()
        try:
            for r in conn.execute("SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)).fetchall():
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["id"],))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
            conn.commit()
            db.audit(conn, "doc.delete", actor="admin", target=str(doc_id))
            return 200, {"ok": True}
        finally:
            conn.close()

    def grants(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT gr.*, (SELECT nickname FROM users u WHERE u.staff_id = gr.staff_id) AS nickname
                FROM grants gr ORDER BY gr.id DESC
                """
            ).fetchall()
            return 200, {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    def add_grant(self, q, body) -> Tuple[int, Any]:
        staff = (body.get("staff_id") or "").strip()
        scope = (body.get("scope_key") or "").strip()
        if not (staff and scope):
            return 400, {"error": "staff_id 与 scope_key 必填"}
        conn = self._conn()
        try:
            store.ensure_scope(conn, scope)
            store.upsert_user(conn, staff, body.get("nickname", ""), role="member")
            conn.execute(
                """
                INSERT INTO grants (staff_id, scope_key, can_read, can_write, note, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(staff_id, scope_key) DO UPDATE SET
                    can_read = excluded.can_read, can_write = excluded.can_write, note = excluded.note
                """,
                (
                    staff,
                    scope,
                    1 if body.get("can_read", True) else 0,
                    1 if body.get("can_write", False) else 0,
                    body.get("note", ""),
                    db.now_ms(),
                ),
            )
            conn.commit()
            db.audit(conn, "grant.add", actor="admin", target="%s@%s" % (staff, scope))
            return 200, {"ok": True}
        finally:
            conn.close()

    def del_grant(self, q, body) -> Tuple[int, Any]:
        try:
            gid = int(body.get("id"))
        except (TypeError, ValueError):
            return 400, {"error": "id 必填"}
        conn = self._conn()
        try:
            conn.execute("DELETE FROM grants WHERE id = ?", (gid,))
            conn.commit()
            db.audit(conn, "grant.delete", actor="admin", target=str(gid))
            return 200, {"ok": True}
        finally:
            conn.close()

    def users(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT u.*,
                  (SELECT COUNT(*) FROM group_members m WHERE m.staff_id = u.staff_id) AS groups,
                  (SELECT COUNT(*) FROM grants g WHERE g.staff_id = u.staff_id) AS grants
                FROM users u
                ORDER BY CASE u.role
                           WHEN 'superadmin' THEN 0
                           WHEN 'admin'      THEN 1
                           WHEN 'member'     THEN 2
                           WHEN 'guest'      THEN 3
                           ELSE 4
                         END,
                         u.created_at, u.staff_id
                LIMIT 500
                """
            ).fetchall()
            return 200, {"items": [dict(r) for r in rows]}
        finally:
            conn.close()

    def add_user(self, q, body) -> Tuple[int, Any]:
        staff = (body.get("staff_id") or "").strip()
        if not staff:
            return 400, {"error": "staff_id 必填"}
        conn = self._conn()
        try:
            store.upsert_user(conn, staff, body.get("nickname", ""), body.get("role", "member"))
            db.audit(conn, "user.add", actor="admin", target=staff)
            return 200, {"ok": True}
        finally:
            conn.close()

    def set_role(self, q, body) -> Tuple[int, Any]:
        staff = (body.get("staff_id") or "").strip()
        role = (body.get("role") or "").strip()
        if not staff or role not in {"superadmin", "admin", "member", "guest"}:
            return 400, {"error": "staff_id / role 不合法"}
        conn = self._conn()
        try:
            cur = conn.execute("SELECT role FROM users WHERE staff_id = ?", (staff,)).fetchone()
            if cur is None:
                return 404, {"error": "用户不存在"}
            if cur["role"] == acl.ROLE_SUPERADMIN and role != acl.ROLE_SUPERADMIN:
                left = conn.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE role = ?", (acl.ROLE_SUPERADMIN,)
                ).fetchone()["n"]
                if left <= 1:
                    # 不能把自己锁在门外：至少要留一个超级管理员
                    return 400, {"error": "这是最后一个超级管理员，不能降级"}
            conn.execute("UPDATE users SET role = ? WHERE staff_id = ?", (role, staff))
            conn.commit()
            db.audit(conn, "user.role", actor="admin", target=staff, detail=role)
            return 200, {"ok": True}
        finally:
            conn.close()

    def ask(self, q, body) -> Tuple[int, Any]:
        staff = (body.get("staff_id") or "").strip()
        question = (body.get("question") or "").strip()
        if not question:
            return 400, {"error": "question 必填"}
        conn = self._conn()
        try:
            ans = rag.ask(
                conn,
                staff_id=staff,
                question=question,
                conversation_id=body.get("conversation_id") or None,
                nickname=body.get("nickname", ""),
            )
            return 200, ans.to_dict()
        finally:
            conn.close()

    def search(self, q, body) -> Tuple[int, Any]:
        staff = (body.get("staff_id") or "").strip()
        query = (body.get("query") or body.get("question") or "").strip()
        conn = self._conn()
        try:
            principal = acl.resolve(conn, staff)
            scopes = principal.scopes
            if not scopes:
                return 200, {"items": [], "scopes": [], "reason": principal.reason}
            hits = store.search(conn, query, scopes, k=int(body.get("k", 8)))
            return 200, {"items": hits, "scopes": sorted(scopes)}
        finally:
            conn.close()

    def distill(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            scope = (body.get("scope_key") or "").strip()
            if scope:
                return 200, distiller.distill_scope(conn, scope, actor="admin")
            return 200, {"results": distiller.distill_all(conn, actor="admin")}
        finally:
            conn.close()

    def ingest(self, q, body) -> Tuple[int, Any]:
        conn = self._conn()
        try:
            items = []
            if body.get("messages"):
                items = body["messages"]
            elif body.get("path"):
                items = file_import.load_file(
                    Path(body["path"]),
                    conversation_id=body.get("conversation_id", ""),
                    title=body.get("title", ""),
                )
            elif body.get("content"):
                fmt = (body.get("format") or "text").lower()
                if fmt == "json":
                    items = file_import.parse_json(body["content"])
                elif fmt == "csv":
                    items = file_import.parse_csv(body["content"], body.get("conversation_id", ""))
                else:
                    items = file_import.parse_text(
                        body["content"],
                        body.get("conversation_id", "import_txt"),
                        body.get("title", ""),
                    )
            if not items:
                return 400, {"error": "没有可导入的消息"}
            result = ingest.ingest_batch(conn, items)
            db.audit(conn, "ingest", actor="admin", detail=json.dumps(result, ensure_ascii=False))
            return 200, result
        finally:
            conn.close()

    def identities(self, q, body) -> Tuple[int, Any]:
        """给问答页用的身份列表：每个身份能看哪些知识域，一目了然。"""
        conn = self._conn()
        try:
            rows = conn.execute(
                """
                SELECT staff_id, nickname, role, status FROM users
                ORDER BY CASE role
                           WHEN 'superadmin' THEN 0
                           WHEN 'admin'      THEN 1
                           WHEN 'member'     THEN 2
                           WHEN 'guest'      THEN 3
                           ELSE 4
                         END,
                         staff_id
                LIMIT 300
                """
            ).fetchall()
            items = []
            for r in rows:
                principal = acl.resolve(conn, r["staff_id"], r["nickname"] or "")
                items.append(
                    {
                        "staff_id": r["staff_id"],
                        "nickname": r["nickname"] or "",
                        "role": r["role"],
                        "blocked": principal.blocked,
                        "is_super": principal.is_super,
                        "scopes": sorted(principal.scopes),
                        "reason": principal.reason,
                    }
                )
            return 200, {"items": items}
        finally:
            conn.close()

    def audit(self, q, body) -> Tuple[int, Any]:
        try:
            limit = int(q.get("limit", ["100"])[0])
        except ValueError:
            limit = 100
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (min(limit, 500),)
            ).fetchall()
            return 200, {"items": [dict(r) for r in rows]}
        finally:
            conn.close()


API = Api()


class Handler(BaseHTTPRequestHandler):
    server_version = "kb-bot/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: A003
        if self.path.startswith("/api/"):
            return
        super().log_message(fmt, *args)

    # ------------------------------------------------------------ utils
    def _send(self, code: int, payload: Any, ctype: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (dict, list)):
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(payload, str):
            raw = payload.encode("utf-8")
        else:
            raw = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def _authorized(self, parsed) -> bool:
        token = config.admin_token()
        header = self.headers.get("X-Admin-Token", "")
        query = parse_qs(parsed.query).get("token", [""])[0]
        return bool(token) and (header == token or query == token)

    def _body(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel in {"ask", "chat", "ask.html"}:
            rel = "ask.html"
        elif rel in {"m", "mobile", "m.html"}:
            rel = "m.html"
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.exists():
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ------------------------------------------------------------ verbs
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            if not self._authorized(parsed):
                self._send(401, {"error": "token 无效"})
                return
            route = API.routes.get(("GET", parsed.path))
            if route is None:
                self._send(404, {"error": "未找到接口"})
                return
            try:
                code, payload = route(parse_qs(parsed.query), {})
                self._send(code, payload)
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": str(e)})
            return
        self._static(parsed.path)

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, "Not Found", "text/plain; charset=utf-8")
            return
        if not self._authorized(parsed):
            self._send(401, {"error": "token 无效"})
            return
        route = API.routes.get(("POST", parsed.path))
        if route is None:
            self._send(404, {"error": "未找到接口"})
            return
        body = self._body()
        try:
            code, payload = route({}, body)
            self._send(code, payload)
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})


def serve(host: str = "0.0.0.0", aclport: Optional[int] = None) -> None:
    conn = db.get_db()
    db.init_db(conn)
    conn.close()
    p = aclport or config.port()
    httpd = ThreadingHTTPServer((host, p), Handler)
    print("管理后台: http://%s:%d   (token 见 KB_ADMIN_TOKEN)" % (host, p))
    httpd.serve_forever()


def serve_background(host: str = "127.0.0.1", port_num: int = 0) -> ThreadingHTTPServer:
    """测试用：起一个后台线程服务。"""
    conn = db.get_db()
    conn.close()
    httpd = ThreadingHTTPServer((host, port_num), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
