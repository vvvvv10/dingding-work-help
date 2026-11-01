#!/usr/bin/env python3
"""假的 dws 命令，用于测试解析与入库链路。

输出结构严格对齐 dws v1.0.66 的真实返回：
  chat search        -> {"result": {"groups": [...]}}
  chat +chat-list    -> {"chats": [...]}
  chat message list  -> {"messages": [...]}（createTime 是 'YYYY-MM-DD HH:MM:SS' 字符串）
  event consume      -> 每行一个扁平化事件（--flatten）
事件流里混入一条心跳和非消息事件，用来验证过滤逻辑。
"""
import json
import sys

ARGS = sys.argv[1:]
CID_A = "cidFakeAAAA=="
CID_B = "cidFakeBBBB=="


def out(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main():
    if not ARGS or ARGS[0] in ("version", "--version"):
        print("dws version 1.0.66-fake")
        return 0

    if ARGS[0] == "auth":
        out({"success": True, "authenticated": True, "corpId": "dingFakeCorp", "userId": "collector01"})
        return 0

    if ARGS[0] == "chat" and len(ARGS) > 1 and ARGS[1] == "search":
        out(
            {
                "errorCode": None,
                "result": {
                    "groups": [
                        {
                            "openConversationId": CID_A,
                            "title": "售后支持群",
                            "groupType": "INTERNAL_GROUP",
                            "memberCount": 8,
                        },
                        {
                            "openConversationId": CID_B,
                            "title": "仓储协同群",
                            "groupType": "NEW_EXTERNAL_GROUP",
                            "memberCount": 3,
                        },
                    ]
                },
            }
        )
        return 0

    if ARGS[0] == "chat" and len(ARGS) > 1 and ARGS[1] == "+chat-list":
        out(
            {
                "chats": [
                    {
                        "openConversationId": CID_A,
                        "name": "售后支持群",
                        "conversationType": "group",
                        "memberCount": 8,
                    },
                    {
                        "openConversationId": CID_B,
                        "name": "仓储协同群",
                        "conversationType": "group",
                        "memberCount": 3,
                    },
                ],
                "complete": True,
            }
        )
        return 0

    if ARGS[0] == "chat" and len(ARGS) > 1 and ARGS[1] == "message":
        out(
            {
                "contractVersion": "im.message-list.v1",
                "complete": True,
                "count": 2,
                "messages": [
                    {
                        "conversationId": CID_A,
                        "openConversationId": CID_A,
                        "messageId": "msgFake1==",
                        "openMessageId": "msgFake1==",
                        "content": "退款到账一般是三个工作日",
                        "text": "退款到账一般是三个工作日",
                        "createTime": "2026-11-01 09:00:00",
                        "sendType": "user",
                        "sender": "甲",
                        "senderId": "DfakeSenderAAAA",
                        "senderOpenDingTalkId": "DfakeSenderAAAA",
                    },
                    {
                        "conversationId": CID_A,
                        "openConversationId": CID_A,
                        "messageId": "msgFake2==",
                        "openMessageId": "msgFake2==",
                        "content": "跨境订单要七个工作日",
                        "text": "跨境订单要七个工作日",
                        "createTime": "2026-11-01 09:10:00",
                        "sendType": "user",
                        "sender": "乙",
                        "senderId": "DfakeSenderBBBB",
                        "senderOpenDingTalkId": "DfakeSenderBBBB",
                    },
                ],
            }
        )
        return 0

    if ARGS[0] == "event":
        events = [
            {
                "type": "EVENT",
                "event_type": "user_im_message_receive_group_all",
                "data": {
                    "conversationId": CID_A,
                    "conversationTitle": "售后支持群",
                    "senderStaffId": "DfakeSenderCCC",
                    "senderNick": "丙",
                    "msgId": "e1",
                    "createAt": 1793539200000,
                    "msgtype": "text",
                    "content": {"text": "库存同步是每小时整点一次"},
                },
            },
            {"status": "heartbeat"},
            {
                "data": {
                    "conversationId": CID_A,
                    "conversationTitle": "售后支持群",
                    "senderStaffId": "DfakeSenderDDD",
                    "senderNick": "丁",
                    "msgId": "e2",
                    "createAt": 1793539300000,
                    "content": "对，大促期间改成一小时两次",
                }
            },
        ]
        for e in events:
            print(json.dumps(e, ensure_ascii=False), flush=True)
        return 0

    print("fake dws: 不支持的命令 %s" % " ".join(ARGS), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
