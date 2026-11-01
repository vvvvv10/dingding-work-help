"""钉钉 Stream 机器人：接收 @ 消息、沉淀知识、回答问题。

Stream 模式用出站长连接，不需要公网 IP、不需要验签，服务器能上网就行。
依赖：pip install dingtalk-stream
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .. import acl, config, db, distiller, ingest, rag, store
from ..ingest.normalizer import ensure_group

log = logging.getLogger("kbbot.stream")

HELP = """**我可以做这些事**
- 直接提问：在群里 @我 说问题，我在你有权限的知识域里找答案
- `/沉淀`：把本群最近的讨论整理成知识（进审核队列，管理员确认后生效）
- `/权限`：查看你能访问哪些知识域
- `/帮助`：显示这条说明

知识按群隔离：A 群沉淀的知识，只有 A 群的人和被单独授权的人能查到。"""


def handle_text(
    conn,
    staff_id: str,
    content: str,
    conversation_id: str,
    nickname: str = "",
    conversation_title: str = "",
    is_group: bool = True,
) -> str:
    """纯文本入口，便于脱离钉钉 SDK 做测试。"""
    content = (content or "").strip()
    if not content:
        return ""

    if conversation_id:
        ensure_group(conn, conversation_id, conversation_title)

    principal = acl.resolve(conn, staff_id, nickname)

    if content in {"/帮助", "帮助", "/help", "help"}:
        return HELP

    if content.startswith("/权限") or content.startswith("/permission"):
        if principal.blocked:
            return "你的账号已被禁用。"
        if not principal.scopes:
            return "你当前没有可访问的知识域。原因：%s" % (principal.reason or "未分配")
        return "**你可访问的知识域**\n" + "\n".join("- `%s`" % s for s in sorted(principal.scopes))

    if content.startswith("/沉淀") or content.startswith("/distill"):
        group = store.get_group(conn, conversation_id) if conversation_id else None
        if group is None:
            return "这个会话没有绑定知识域，无法沉淀。"
        if not principal.can_read(group["scope_key"]):
            acl.log_deny(conn, principal, "distill", group["scope_key"], "无沉淀权限")
            return "你没有这个知识域的权限，操作已拒绝并记录。"
        result = distiller.distill_scope(conn, group["scope_key"], actor=staff_id)
        if not result.get("created"):
            return "这次没有值得沉淀的新内容（%s）。" % result.get("reason", "消息不足")
        if result["status"] == "approved":
            return "已沉淀 %d 条知识，已直接生效。" % result["created"]
        return "已沉淀 %d 条知识，等待管理员审核后生效。" % result["created"]

    question = re.sub(r"^/问答\s*", "", content)
    answer = rag.ask(
        conn,
        staff_id=staff_id,
        question=question,
        conversation_id=conversation_id,
        nickname=nickname,
    )
    return answer.text


class _Handler:
    """真正的 SDK Handler 在下面动态构造，避免没装 SDK 时导入失败。"""


def build_client():
    try:
        import dingtalk_stream  # type: ignore
    except ImportError as e:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "缺少 dingtalk-stream 依赖。执行：pip install dingtalk-stream"
        ) from e

    creds = config.dingtalk_credentials()
    if creds is None:
        raise RuntimeError("未配置 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET")

    class Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):
            data = callback.data or {}
            try:
                msg = dingtalk_stream.ChatbotMessage.from_dict(data)
            except Exception:  # noqa: BLE001
                log.exception("消息解析失败")
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            msg_type = getattr(msg, "message_type", "text") or "text"
            text_obj = getattr(msg, "text", None)
            content = getattr(text_obj, "content", "") if text_obj else ""
            if msg_type != "text" or not content:
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

            staff_id = getattr(msg, "sender_staff_id", "") or ""
            nickname = getattr(msg, "sender_nick", "") or ""
            conversation_id = getattr(msg, "conversation_id", "") or ""
            is_group = str(getattr(msg, "conversation_type", "2")) == "2"
            title = getattr(msg, "conversation_title", "") or ""

            conn = db.get_db()
            try:
                ingest.ingest_message(
                    conn,
                    conversation_id=conversation_id,
                    content=content,
                    sender_id=staff_id,
                    sender_nick=nickname,
                    msg_id=getattr(msg, "message_id", "") or "",
                    created_at=getattr(msg, "create_at", None),
                    conversation_title=title,
                )
                reply = handle_text(
                    conn,
                    staff_id=staff_id,
                    content=content,
                    conversation_id=conversation_id,
                    nickname=nickname,
                    conversation_title=title,
                    is_group=is_group,
                )
            except Exception:  # noqa: BLE001
                log.exception("处理消息失败")
                reply = "处理这条消息时出错了，已记录日志。"
            finally:
                conn.close()

            if reply:
                try:
                    self.reply_markdown("知识助手", reply, msg)
                except Exception:  # noqa: BLE001
                    log.exception("回复失败")
                    try:
                        self.reply_text(reply[:1500], msg)
                    except Exception:  # noqa: BLE001
                        log.exception("文本回复也失败")
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(creds["client_id"], creds["client_secret"])
    )
    client.register_callback_handler(
        dingtalk_stream.chatbot.ChatbotMessage.TOPIC, Handler()
    )
    return client


def run_forever() -> None:
    client = build_client()
    log.info("Stream 机器人已启动，等待钉钉推送消息…")
    client.start_forever()
