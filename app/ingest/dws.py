"""DWS（DingTalk Workspace CLI）消息接入。

为什么用它：企业内部机器人只能收到 @机器人 的消息，而 DWS 的
`event consume user_im_message_receive_group_all` 能订阅到群内全部消息，
`chat message list` 还能补历史。它以「登录用户」的身份工作，
所以拿到的范围 = 这个账号能看到的范围，权限上依然是钉钉自己在管。

设计要点：
1. 不在代码里写死字段名。`--flatten` 输出的字段在不同版本里可能微调，
   这里用「多别名深度匹配 + 可覆盖的字段映射」来兜底，
   先用 `python -m app.main dws-dump` 抓真实事件核对一遍最稳。
2. 事件流是长进程，断了要能自动重连。
3. 所有消息最终都走 ingest_message()，和机器人来源共用同一套权限与沉淀流程。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .. import config
from .archive_poller import ArchiveSource

log = logging.getLogger("kbbot.dws")

# 字段别名：键统一小写、去掉下划线和点后比较
_ALIASES = {
    "conversation_id": [
        "conversationid", "openconversationid", "chatid", "cid", "conversation",
    ],
    "conversation_title": ["conversationtitle", "grouptitle", "chattitle", "title", "groupname"],
    "sender_id": [
        "senderstaffid", "senderid", "userid", "staffid", "senderuserid", "senderopendingtalkid",
    ],
    "sender_nick": ["sendernick", "sendername", "nick", "sendernickname", "username", "sender"],
    "content": ["content", "text", "msg", "body", "msgcontent", "messagecontent"],
    "msg_id": ["msgid", "messageid", "eventid", "id"],
    "created_at": ["createat", "createtime", "timestamp", "msgtime", "msgtimestamp", "time"],
    "msg_type": ["msgtype", "messagetype"],
}

_NOISE_KEYS = {"status", "heartbeat", "ping", "connected", "keepalive"}


class DwsError(RuntimeError):
    pass


def dws_bin() -> str:
    return config.dws_bin()


def available() -> bool:
    return shutil.which(dws_bin()) is not None


def _run(argv: List[str], timeout: int = 60) -> str:
    cmd = [dws_bin()] + argv
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise DwsError("找不到 dws 命令，请先安装并确认在 PATH 中（DWS_BIN 可指定路径）") from e
    except subprocess.TimeoutExpired as e:
        raise DwsError("dws 命令超时：%s" % " ".join(argv)) from e
    if proc.returncode != 0:
        raise DwsError(
            "dws 执行失败(%d)：%s\n%s" % (proc.returncode, " ".join(argv), (proc.stderr or "")[:500])
        )
    return proc.stdout or ""


def _loads(raw: str) -> Any:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # ndjson：取最后一条完整对象
    last = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def run_json(argv: List[str], timeout: int = 60) -> Any:
    return _loads(_run(argv, timeout=timeout))


def version() -> str:
    try:
        return _run(["version"], timeout=20).strip().splitlines()[0]
    except (DwsError, IndexError):
        return "unknown"


def auth_status() -> Dict:
    try:
        data = run_json(["auth", "status", "-f", "json"], timeout=30)
    except DwsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "raw": data}


def search_groups(query: str = "", limit: int = 30) -> List[Dict]:
    """按关键词搜群（dws chat search）。query 为空时退化为列出全部会话。"""
    if not (query or "").strip():
        return list_groups(limit=limit)
    try:
        data = run_json(["chat", "search", "--query", query, "--limit", str(limit), "-f", "json"], timeout=60)
    except DwsError as e:
        log.warning("搜索群失败：%s", e)
        return []
    return _map_chats(_dig(data, "groups") or [])


def list_groups(limit: int = 100) -> List[Dict]:
    """列出当前登录用户加入的群（dws chat +chat-list）。"""
    try:
        data = run_json(
            ["chat", "+chat-list", "--types", "group", "--page-size", str(min(limit, 100)), "-f", "json"],
            timeout=90,
        )
    except DwsError as e:
        log.warning("列出群失败：%s", e)
        return []
    return _map_chats(_dig(data, "chats") or [])


def _dig(data: Any, key: str) -> Any:
    """从 {'result': {'groups': [...]}} 这类包装里取出数组。"""
    if isinstance(data, dict):
        if isinstance(data.get(key), list):
            return data[key]
        for v in data.values():
            if isinstance(v, (dict, list)):
                got = _dig(v, key)
                if got:
                    return got
    elif isinstance(data, list):
        for v in data:
            got = _dig(v, key)
            if got:
                return got
    return None


def _map_chats(items: Iterable[Dict]) -> List[Dict]:
    out: List[Dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = it.get("openConversationId") or it.get("conversationId") or ""
        if not cid:
            continue
        out.append(
            {
                "conversation_id": str(cid),
                "title": str(it.get("title") or it.get("name") or ""),
                "group_type": str(it.get("groupType") or ""),
                "member_count": it.get("memberCount"),
            }
        )
    return out


# ------------------------------------------------------------------ 字段映射

def _norm_key(k: str) -> str:
    return (k or "").lower().replace("_", "").replace(".", "").replace("-", "")


def _deep_pick(obj: Any, keys: List[str], depth: int = 0) -> Any:
    """在一个嵌套结构里按别名找第一个非空值。

    别名列表的顺序就是优先级，不能按对象字段顺序撞运气：DWS 的事件里
    `event_id` 排在 `message_id` 前面，若按字段顺序取，msg_id 会拿到 event_id，
    断线重连后同一个消息会带着新 event_id 再入一次库。
    """
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        lower = {}
        for k, v in obj.items():
            lower.setdefault(_norm_key(k), v)
        for want in keys:
            v = lower.get(_norm_key(want))
            if v in (None, "", [], {}):
                continue
            if isinstance(v, (dict, list)):
                continue
            return v
        for v in obj.values():
            got = _deep_pick(v, keys, depth + 1)
            if got not in (None, ""):
                return got
    elif isinstance(obj, list):
        for v in obj[:5]:
            got = _deep_pick(v, keys, depth + 1)
            if got not in (None, ""):
                return got
    return None


def _to_ms(value: Any) -> Optional[int]:
    """兼容三种时间表示：epoch(秒/毫秒)、'2026-11-01 13:29:45'、ISO8601 带时区。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10**11 else v * 1000
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v if v > 10**11 else v * 1000
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return int(time.mktime(dt.timetuple()) * 1000)
        return int(dt.timestamp() * 1000)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(time.mktime(time.strptime(s, fmt)) * 1000)
        except ValueError:
            continue
    return None


def normalize_event(event: Dict) -> Optional[Dict]:
    """把一条 DWS 事件转成 ingest_message 的参数。不是消息事件则返回 None。"""
    if not isinstance(event, dict):
        return None
    # 有些版本把业务体包在 data / payload / event 里
    body = event
    for wrapper in ("data", "payload", "event", "message"):
        if isinstance(event.get(wrapper), dict):
            body = {**event, **event[wrapper]}
            break

    content = _deep_pick(body, _ALIASES["content"])
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False)
    if not content or not str(content).strip():
        return None

    conversation_id = _deep_pick(body, _ALIASES["conversation_id"])
    if not conversation_id:
        # 单聊事件也可能没有会话 ID，用发送人兜底
        sender = _deep_pick(body, _ALIASES["sender_id"])
        if not sender:
            return None
        conversation_id = "dws_o2o_%s" % sender

    sender_id = _deep_pick(body, _ALIASES["sender_id"]) or ""
    return {
        "conversation_id": str(conversation_id),
        "conversation_title": str(_deep_pick(body, _ALIASES["conversation_title"]) or ""),
        "content": str(content),
        "sender_id": str(sender_id),
        "sender_nick": str(_deep_pick(body, _ALIASES["sender_nick"]) or ""),
        "msg_id": str(_deep_pick(body, _ALIASES["msg_id"]) or ""),
        "created_at": _to_ms(_deep_pick(body, _ALIASES["created_at"])),
        "msg_type": str(_deep_pick(body, _ALIASES["msg_type"]) or "text"),
    }


# ------------------------------------------------------------------ 历史消息

EVENT_PRESETS = {
    "group_all": ["event", "consume", "user_im_message_receive_group_all"],
    "group": ["event", "consume", "user_im_message_receive_group"],
    "at": ["event", "consume", "user_im_message_receive_at"],
    "o2o_all": ["event", "consume", "user_im_message_receive_o2o_all"],
}


def event_argv(kind: str = "group_all", chat_id: str = "") -> List[str]:
    base = EVENT_PRESETS.get(kind)
    if base is None:
        raise DwsError("不支持的事件类型：%s（可选 %s）" % (kind, "/".join(EVENT_PRESETS)))
    argv = list(base)
    if kind in {"group", "at"} and chat_id:
        argv += ["--group", chat_id]
    argv += ["--flatten", "-f", "ndjson"]
    profile = config.dws_profile()
    if profile:
        argv = ["--profile", profile] + argv
    return argv


class DwsMessageSource(ArchiveSource):
    """把 DWS 当作「消息存档源」接入已有的 pull 流程，用于补历史消息。"""

    name = "dws"
    _title_cache: Dict[str, str] = {}

    def available(self) -> bool:
        return available()

    def _title_for(self, conversation_id: str) -> str:
        if conversation_id in self._title_cache:
            return self._title_cache[conversation_id]
        try:
            for g in list_groups(limit=100):
                self._title_cache[g["conversation_id"]] = g["title"]
        except DwsError:
            pass
        return self._title_cache.get(conversation_id, "")

    def fetch(self, conversation_id: str, since_ms: int = 0) -> Iterable[Dict]:
        limit = config.dws_history_limit()
        argv = [
            "chat", "message", "list",
            "--conversation-id", conversation_id,
            "--limit", str(limit),
            "-f", "json",
        ]
        try:
            data = run_json(argv, timeout=120)
        except DwsError as e:
            log.warning("拉取历史消息失败(%s)：%s", conversation_id, e)
            return []
        items = _dig(data, "messages") or _extract_list(data)
        title = ""
        out: List[Dict] = []
        for raw in items:
            norm = normalize_event(raw)
            if norm is None:
                continue
            norm["conversation_id"] = norm.get("conversation_id") or conversation_id
            if not norm.get("conversation_title"):
                if not title:
                    title = self._title_for(conversation_id)
                norm["conversation_title"] = title
            if since_ms and (norm.get("created_at") or 0) < since_ms:
                continue
            out.append(norm)
        log.info("DWS 拉取 %s：%d 条", conversation_id, len(out))
        return out


def _extract_list(data: Any) -> List[Dict]:
    """从各种包装里取出消息数组。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("result", "data", "items", "messages", "list", "records", "chats", "groups"):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            inner = _extract_list(v)
            if inner:
                return inner
    return []


# ------------------------------------------------------------------ 实时监听

class EventListener:
    """常驻子进程读取 NDJSON 事件流，断线自动重连。"""

    def __init__(self, kind: str = "group_all", chat_id: str = "", on_event=None):
        self.kind = kind
        self.chat_id = chat_id
        self.on_event = on_event
        self._proc: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for pipe in (getattr(self._proc, "stdout", None), getattr(self._proc, "stderr", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass

    def _spawn(self) -> subprocess.Popen:
        argv = [dws_bin()] + event_argv(self.kind, self.chat_id)
        log.info("启动事件流：%s", " ".join(argv))
        return subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
        )

    def iter_events(self) -> Iterator[Dict]:
        assert self._proc is not None and self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                if not any(k in line.lower() for k in _NOISE_KEYS):
                    log.debug("跳过非 JSON 行：%s", line[:120])
                continue
            if isinstance(event, dict) and set(event.keys()) & _NOISE_KEYS and len(event) <= 2:
                continue
            yield event

    def run_forever(self, backoff_max: int = 60) -> None:
        backoff = 2
        while True:
            try:
                self._proc = self._spawn()
                started = time.time()
                for event in self.iter_events():
                    try:
                        if self.on_event:
                            self.on_event(event)
                    except Exception:  # noqa: BLE001
                        log.exception("处理事件失败")
                code = self._proc.wait()
                stderr = ""
                if self._proc.stderr is not None:
                    try:
                        stderr = self._proc.stderr.read()[-500:]
                    except (ValueError, OSError):
                        stderr = ""
                log.warning("事件流结束(code=%s)：%s", code, stderr.strip())
                if code != 0 and ("auth" in stderr.lower() or "登录" in stderr):
                    log.error("看起来是登录失效，请重新执行 dws auth login")
                    return
                if time.time() - started > 120:
                    backoff = 2
            except FileNotFoundError:
                raise DwsError("找不到 dws 命令，请先安装（见 README）")
            except Exception:  # noqa: BLE001
                log.exception("事件流异常")
            log.info("%d 秒后重连", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
