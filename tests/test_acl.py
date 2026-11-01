"""权限与检索的关键行为测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import acl, config, db, embedder, rag, store  # noqa: E402
from app.scripts_seed import seed_demo  # noqa: E402


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kbbot-test-")
        os.environ["KB_DB_PATH"] = os.path.join(cls.tmp, "t.sqlite3")
        # 测试永远跑临时 SQLite：.env 里可能配了 KB_BACKEND=mysql，绝不能连过去清库
        os.environ["KB_BACKEND"] = "sqlite"
        for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "EMBED_MODEL", "EMBED_BASE_URL"):
            os.environ.pop(k, None)
        os.environ["ACL_REQUIRE_GROUP_MEMBERSHIP"] = "true"
        os.environ["KNOWLEDGE_AUTO_APPROVE"] = "false"
        embedder.reset()

    def setUp(self):
        self.conn = db.get_db()
        for t in ("chunks_fts", "chunks", "docs", "messages", "group_members", "grants", "groups", "users", "scopes", "audit_log"):
            self.conn.execute("DELETE FROM %s" % t)
        self.conn.commit()
        seed_demo(self.conn, force=True)

    def tearDown(self):
        self.conn.close()


class TestAcl(BaseCase):
    def test_member_scopes_follow_group(self):
        p = acl.resolve(self.conn, "u1001")
        self.assertEqual(p.scopes, {"chat_support"}, "客服群成员只应拿到客服知识域")

    def test_other_group_member_isolated(self):
        p = acl.resolve(self.conn, "u2001")
        self.assertEqual(p.scopes, {"chat_dev"})

    def test_admin_sees_everything(self):
        p = acl.resolve(self.conn, "admin001")
        self.assertTrue(p.is_admin)
        self.assertIn("chat_dev", p.scopes)
        self.assertIn("chat_support", p.scopes)

    def test_unknown_user_gets_nothing(self):
        p = acl.resolve(self.conn, "nobody999")
        self.assertEqual(p.scopes, set())
        self.assertTrue(p.reason)

    def test_grant_opens_a_scope(self):
        self.conn.execute(
            "INSERT INTO grants (staff_id, scope_key, can_read, can_write, note, created_at)"
            " VALUES ('u1001','chat_dev',1,0,'临时协作',0)"
        )
        self.conn.commit()
        p = acl.resolve(self.conn, "u1001")
        self.assertIn("chat_dev", p.scopes)


class TestRetrieval(BaseCase):
    def test_chinese_bigram_search_hits(self):
        hits = store.search(self.conn, "客户拆封了能退货吗", {"chat_support"}, k=5)
        self.assertTrue(hits, "中文查询应能命中退货政策")
        self.assertTrue(any("退货" in h["text"] for h in hits))

    def test_search_never_crosses_scope(self):
        hits = store.search(self.conn, "发布窗口 告警 数据库", {"chat_support"}, k=10)
        self.assertTrue(all(h["scope_key"] == "chat_support" for h in hits), "检索不得越域")

        hits_dev = store.search(self.conn, "生产发布窗口", {"chat_dev"}, k=5)
        self.assertTrue(hits_dev)
        self.assertTrue(all(h["scope_key"] == "chat_dev" for h in hits_dev))

    def test_empty_scopes_returns_nothing(self):
        self.assertEqual(store.search(self.conn, "退货", set(), k=5), [])

    def test_pending_doc_not_searchable(self):
        hits = store.search(self.conn, "定制款 拆封 软件卡", {"chat_support"}, k=10)
        self.assertFalse(any("定制款商品" in h["text"] for h in hits), "待审核内容不应进入检索")


class TestRag(BaseCase):
    def test_group_question_answers_within_scope(self):
        ans = rag.ask(self.conn, "u1001", "拆封的软件卡能退吗", conversation_id="cidSupportAAAA")
        self.assertFalse(ans.refused)
        self.assertEqual(ans.scopes, ["chat_support"])
        self.assertTrue(ans.citations)

    def test_cross_group_question_refused(self):
        ans = rag.ask(self.conn, "u2001", "发票怎么开", conversation_id="cidSupportAAAA")
        self.assertTrue(ans.refused, "非本群成员在群里提问必须被拒绝")
        self.assertEqual(ans.scopes, [])

    def test_outsider_private_question_refused(self):
        ans = rag.ask(self.conn, "nobody999", "退货政策")
        self.assertTrue(ans.refused)

    def test_admin_can_ask_anything(self):
        ans = rag.ask(self.conn, "admin001", "生产发布窗口")
        self.assertFalse(ans.refused)
        self.assertTrue(any("发布" in c["title"] or "发布" in c["snippet"] for c in ans.citations))

    def test_denials_are_audited(self):
        rag.ask(self.conn, "u2001", "发票怎么开", conversation_id="cidSupportAAAA")
        row = self.conn.execute("SELECT COUNT(*) FROM audit_log WHERE allowed = 0").fetchone()
        self.assertGreaterEqual(row[0], 1, "越权必须留痕")


class TestRawFallback(BaseCase):
    """提炼有最小条数门槛，一两句话说完的答案永远变不成卡片，只能从原文兜底。"""

    def _say(self, content: str, nick: str = "张三", ts: int = 1735689600000) -> None:
        store.record_message(
            self.conn,
            "cidSupportAAAA",
            "chat_support",
            content,
            sender_id="u1001",
            sender_nick=nick,
            created_at=ts,
        )

    def test_uncarded_answer_found_in_raw_messages(self):
        self._say("临时配对码要在白名单里吗")
        self._say("不用，配对码的地址是 10.2.3.4，端口 9527")
        hits = store.search(self.conn, "配对码走哪个端口", {"chat_support"}, k=5)
        self.assertEqual(hits, [], "这两条没被提炼，卡片检索本来就该是 0")

        ans = rag.ask(self.conn, "u1001", "配对码走哪个端口", conversation_id="cidSupportAAAA")
        self.assertFalse(ans.refused)
        self.assertEqual(ans.mode, "raw")
        self.assertTrue(ans.citations)
        self.assertEqual(ans.citations[0]["status"], "raw")
        self.assertIn("未经整理", ans.text)
        self.assertIn("9527", ans.text, "原文里的端口号要能出现在答案里")

    def test_fallback_never_crosses_scope(self):
        self._say("不用，配对码的地址是 10.2.3.4，端口 9527")
        self.assertEqual(store.search_messages(self.conn, "配对码 端口", {"chat_dev"}, k=4), [])

    def test_fallback_can_be_switched_off(self):
        self._say("不用，配对码的地址是 10.2.3.4，端口 9527")
        os.environ["RAG_RAW_FALLBACK"] = "0"
        try:
            ans = rag.ask(self.conn, "u1001", "配对码走哪个端口", conversation_id="cidSupportAAAA")
            self.assertEqual(ans.mode, "empty")
        finally:
            os.environ.pop("RAG_RAW_FALLBACK", None)

    def test_cards_still_win_when_present(self):
        ans = rag.ask(self.conn, "u1001", "拆封的软件卡能退吗", conversation_id="cidSupportAAAA")
        self.assertNotEqual(ans.mode, "raw", "有卡片时不该走兜底")


class TestStreamHandler(BaseCase):
    def test_slash_permission_lists_scopes(self):
        from app.ingest import stream_bot

        text = stream_bot.handle_text(
            self.conn, "u1001", "/权限", "cidSupportAAAA", "张三", "客服支持群"
        )
        self.assertIn("chat_support", text)
        self.assertNotIn("chat_dev", text)

    def test_group_question_replies(self):
        from app.ingest import stream_bot

        text = stream_bot.handle_text(
            self.conn, "u1001", "大促期间多久发货", "cidSupportAAAA", "张三", "客服支持群"
        )
        self.assertIn("72", text)


class TestApiAuth(unittest.TestCase):
    def test_token_required(self):
        os.environ["KB_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "api.sqlite3")
        os.environ["KB_ADMIN_TOKEN"] = "unit-test-token"
        from app import api

        httpd = api.serve_background("127.0.0.1", 0)
        port = httpd.server_address[1]
        import urllib.error
        import urllib.request

        url = "http://127.0.0.1:%d/api/overview" % port
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

        req = urllib.request.Request(url, headers={"X-Admin-Token": "unit-test-token"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        httpd.shutdown()


class FakeEmbedder(embedder.Embedder):
    """假向量器：不联网，固定返回同一个向量，用来测向量检索那条 SQL。"""

    enabled = True

    def embed(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class TestVectorPath(BaseCase):
    def setUp(self):
        super().setUp()
        self._old = embedder._embedder
        embedder._embedder = FakeEmbedder()
        # 给所有切片塞一个非空向量，否则向量分支会被跳过
        vec = embedder.pack([1.0, 0.0, 0.0])
        self.conn.execute("UPDATE chunks SET vec = ?", (vec,))
        self.conn.commit()

    def tearDown(self):
        embedder._embedder = self._old
        super().tearDown()

    def test_vector_search_respects_scope(self):
        hits = store.search(self.conn, "退货", {"chat_support"}, k=5)
        self.assertTrue(hits)
        self.assertTrue(all(h["scope_key"] == "chat_support" for h in hits))

    def test_vector_search_hides_pending_for_member(self):
        # 这条曾经把 WHERE 拼成 "WHERE AND ..." 直接 500
        hits = store.search(self.conn, "退货", {"chat_support"}, k=20, include_pending=False)
        self.assertTrue(all(h["status"] == "approved" for h in hits))

    def test_vector_search_shows_pending_for_super(self):
        hits = store.search(self.conn, "退货", {"chat_support"}, k=20, include_pending=True)
        self.assertTrue(any(h["status"] == "pending" for h in hits))


if __name__ == "__main__":
    unittest.main(verbosity=2)
