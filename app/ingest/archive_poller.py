"""会话内容存档接入位（企业开通后使用）。

钉钉对「读取群内全部消息」这项能力是限制开放的：
普通企业内部机器人只能收到 @机器人 的消息；要把整个群的消息都留存下来，
企业需要开通「会话内容存档」这类合规留存能力（通常属于付费版本，需企业
主管理员签署协议），由官方提供加密数据拉取通道，企业侧自行解密入库。

本文件留出适配位，等企业侧拿到通道后只需实现 fetch() 即可，
入库、权限、审核流程全部复用，不需要改其他代码。
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Iterable, List, Optional

log = logging.getLogger("kbbot.archive")


class ArchiveSource:
    """会话存档数据源接口。"""

    name = "base"

    def available(self) -> bool:
        return False

    def fetch(self, conversation_id: str, since_ms: int = 0) -> Iterable[Dict]:
        """返回 [{content, sender_id, sender_nick, created_at, msg_id}, ...]"""
        raise NotImplementedError


class OpenPlatformArchiveSource(ArchiveSource):
    """钉钉官方会话存档通道的适配位。

    需要企业侧提供：拉取接口地址、企业鉴权凭证、消息解密密钥。
    拿到后在这里实现 fetch()，把解密后的消息按下面的字段返回即可。
    """

    name = "open_platform"

    def __init__(self) -> None:
        self.endpoint = os.environ.get("ARCHIVE_ENDPOINT", "").strip()
        self.token = os.environ.get("ARCHIVE_TOKEN", "").strip()
        self.secret = os.environ.get("ARCHIVE_SECRET", "").strip()

    def available(self) -> bool:
        return bool(self.endpoint and self.token)

    def fetch(self, conversation_id: str, since_ms: int = 0) -> Iterable[Dict]:
        if not self.available():
            return []
        log.warning(
            "会话存档通道已配置但尚未实现解析逻辑，请参考 README「全量消息」一节补齐 fetch()。"
        )
        return []


def sources() -> List[ArchiveSource]:
    """可用来源：DWS（能拿到群全量消息）+ 官方会话存档适配位。"""
    from . import dws  # 延迟导入，避免循环依赖

    return [dws.DwsMessageSource(), OpenPlatformArchiveSource()]


def any_available() -> bool:
    return any(s.available() for s in sources())


def pull(
    conn,
    conversation_id: str,
    scope_key: Optional[str] = None,
    since_ms: int = 0,
    title: str = "",
) -> Dict:
    """从所有可用的存档源拉取消息并入库。title 用于在消息体缺少群名时补位。"""
    from .. import ingest

    total = {"inserted": 0, "sources": []}
    for src in sources():
        if not src.available():
            continue
        items = list(src.fetch(conversation_id, since_ms=since_ms) or [])
        if not items:
            continue
        payload = [
            {
                "conversation_id": conversation_id,
                "conversation_title": it.get("conversation_title") or title,
                "content": it.get("content", ""),
                "sender_id": it.get("sender_id", ""),
                "sender_nick": it.get("sender_nick", ""),
                "msg_id": it.get("msg_id", ""),
                "created_at": it.get("created_at"),
                "scope_key": scope_key,
            }
            for it in items
        ]
        result = ingest.ingest_batch(conn, payload)
        total["inserted"] += result["inserted"]
        total["sources"].append({"source": src.name, **result})
    return total
