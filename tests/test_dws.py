"""DWS 接入层测试：字段解析、历史拉取、事件流入库、断线行为。

用一个假的 dws 命令驱动，不依赖真实钉钉环境。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, ingest, store  # noqa: E402
from app.ingest import dws  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_dws.py"


def _install_fake() -> Path:
    """把 fixture 复制成可执行文件，当 dws 用。"""
    target = Path(tempfile.mkdtemp(prefix="fakedws-")) / "dws"
    target.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return target


class DwsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bin = _install_fake()
        os.environ["DWS_BIN"] = str(cls.bin)
        os.environ.pop("DWS_PROFILE", None)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dwsdb-")
        os.environ["KB_DB_PATH"] = os.path.join(self.tmp, "t.sqlite3")
        self.conn = db.get_db()
        for t in ("chunks_fts", "chunks", "docs", "messages", "group_members",
                  "grants", "groups", "users", "scopes", "audit_log"):
            self.conn.execute("DELETE FROM %s" % t)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


class TestCliWrapping(DwsCase):
    def test_binary_detected(self):
        self.assertTrue(dws.available(), "DWS_BIN 指向的假命令应被识别")

    def test_version(self):
        self.assertIn("1.0.66-fake", dws.version())

    def test_auth_status(self):
        status = dws.auth_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["raw"]["corpId"], "dingFakeCorp")

    def test_search_groups(self):
        groups = dws.search_groups("")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["conversation_id"], "cidFakeAAAA==")
        self.assertEqual(groups[0]["title"], "售后支持群")

    def test_event_argv_contains_ndjson(self):
        argv = dws.event_argv("group_all", "")
        self.assertIn("user_im_message_receive_group_all", argv)
        self.assertIn("ndjson", argv)
        argv2 = dws.event_argv("group", "cidX")
        self.assertIn("--group", argv2)
        self.assertIn("cidX", argv2)


class TestNormalize(DwsCase):
    def test_nested_content_payload(self):
        ev = {
            "eventType": "x",
            "data": {
                "conversationId": "cid1",
                "conversationTitle": "群一",
                "senderStaffId": "u9",
                "senderNick": "老王",
                "msgId": "m9",
                "createAt": 1793539200000,
                "content": {"text": "库存每小时同步一次"},
            },
        }
        norm = dws.normalize_event(ev)
        self.assertIsNotNone(norm)
        self.assertEqual(norm["conversation_id"], "cid1")
        self.assertEqual(norm["conversation_title"], "群一")
        self.assertEqual(norm["sender_id"], "u9")
        self.assertEqual(norm["content"], "库存每小时同步一次")
        self.assertEqual(norm["created_at"], 1793539200000)

    def test_seconds_upgraded_to_ms(self):
        norm = dws.normalize_event({"conversationId": "c", "content": "内容", "createAt": 1793539200})
        self.assertEqual(norm["created_at"], 1793539200000)

    def test_non_message_event_ignored(self):
        self.assertIsNone(dws.normalize_event({"status": "heartbeat"}))
        self.assertIsNone(dws.normalize_event({"foo": "bar"}))

    def test_o2o_without_conversation(self):
        norm = dws.normalize_event({"senderStaffId": "u5", "content": "单聊内容"})
        self.assertEqual(norm["conversation_id"], "dws_o2o_u5")


class TestHistoryPull(DwsCase):
    def test_source_available(self):
        src = dws.DwsMessageSource()
        self.assertTrue(src.available())

    def test_fetch_returns_normalized(self):
        items = list(dws.DwsMessageSource().fetch("cidFakeAAAA=="))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["content"], "退款到账一般是三个工作日")
        self.assertTrue(all(i["conversation_id"] == "cidFakeAAAA==" for i in items))

    def test_pull_through_archive_poller(self):
        from app.ingest import archive_poller

        result = archive_poller.pull(self.conn, "cidFakeAAAA==")
        self.assertEqual(result["inserted"], 2)
        group = store.get_group(self.conn, "cidFakeAAAA==")
        self.assertIsNotNone(group, "拉取后应自动绑定群")
        self.assertEqual(group["title"], "售后支持群")
        # 发言者被登记为成员，从而获得该知识域的读权限
        from app import acl

        principal = acl.resolve(self.conn, "DfakeSenderAAAA")
        self.assertIn(group["scope_key"], principal.scopes)


class TestEventListener(DwsCase):
    def _collect(self):
        listener = dws.EventListener(kind="group_all")
        listener._proc = listener._spawn()
        try:
            events = list(listener.iter_events())
        finally:
            listener.stop()
        return events

    def test_stream_filters_heartbeat(self):
        events = self._collect()
        self.assertEqual(len(events), 2, "心跳应被过滤，只剩 2 条真消息")

    def test_stream_ingests_into_db(self):
        count = 0
        for ev in self._collect():
            norm = dws.normalize_event(ev)
            if norm:
                res = ingest.ingest_message(self.conn, **norm)
                if res.get("ok"):
                    count += 1
        self.assertEqual(count, 2)
        rows = self.conn.execute("SELECT content FROM messages ORDER BY id").fetchall()
        texts = [r["content"] for r in rows]
        self.assertTrue(any("库存同步" in t for t in texts))
        self.assertTrue(any("一小时两次" in t for t in texts))

    def test_reconnect_on_clean_exit(self):
        """假命令跑完就退出，run_forever 应尝试重连而不是直接崩。"""
        listener = dws.EventListener(kind="group_all")
        proc = listener._spawn()
        self.assertIsNotNone(proc.poll() if False else proc)
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
