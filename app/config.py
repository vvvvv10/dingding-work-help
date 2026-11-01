"""集中配置。所有值来自环境变量或项目根目录的 .env 文件。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Optional[Path] = None) -> None:
    """极简 .env 解析，不覆盖已存在的环境变量。"""
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_dotenv()


def _str(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    raw = _str(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def db_path() -> Path:
    return Path(_str("KB_DB_PATH", str(ROOT / "data" / "kb.sqlite3")))


def backend() -> str:
    """存储后端：sqlite（默认）或 mysql。"""
    return (_str("KB_BACKEND", "sqlite") or "sqlite").lower()


def mysql_config() -> Optional[dict]:
    """MySQL 连接参数。KB_BACKEND=mysql 时必须齐全。"""
    host = _str("MYSQL_HOST")
    database = _str("MYSQL_DATABASE")
    if not (host and database):
        return None
    return {
        "host": host,
        "port": _int("MYSQL_PORT", 3306),
        "user": _str("MYSQL_USER", "root"),
        "password": _str("MYSQL_PASSWORD"),
        "database": database,
        "timeout": _int("MYSQL_TIMEOUT", 10),
    }


def admin_token() -> str:
    return _str("KB_ADMIN_TOKEN", "dev-token-change-me")


def port() -> int:
    return _int("KB_PORT", 8000)


def acl_require_membership() -> bool:
    """开启后，用户必须至少在一个已绑定的钉钉群里，才允许检索知识。"""
    return _bool("ACL_REQUIRE_GROUP_MEMBERSHIP", True)


def acl_deny_guest() -> bool:
    """开启后，未在 users 表登记的人一律拒绝。"""
    return _bool("ACL_DENY_UNKNOWN_USER", False)


def auto_approve() -> bool:
    """开启后，提炼出的知识直接生效，不进人工审核队列。"""
    return _bool("KNOWLEDGE_AUTO_APPROVE", False)


def redact_secrets() -> bool:
    """入库前替换消息里的 token/密钥，避免被检索出来。"""
    return _bool("REDACT_SECRETS", True)


def distill_min_messages() -> int:
    return _int("DISTILL_MIN_MESSAGES", 3)


def distill_window_minutes() -> int:
    return _int("DISTILL_WINDOW_MINUTES", 180)


def distill_interval_minutes() -> int:
    """定时沉淀的检查间隔。默认 10 分钟，让新消息尽快变成可检索的知识。"""
    return _int("DISTILL_INTERVAL_MINUTES", 10)


def top_k() -> int:
    return _int("RAG_TOP_K", 6)


def max_reply_chars() -> int:
    return _int("RAG_MAX_REPLY_CHARS", 1800)


def raw_fallback() -> bool:
    """知识卡片零命中时，回退到群聊原始消息兜底作答。

    提炼有最小条数门槛，一两句话就说完的答案永远变不成卡片；
    关掉这个开关，那些答案就彻底查不到了。
    """
    return _bool("RAG_RAW_FALLBACK", True)


def raw_top_k() -> int:
    """兜底时最多带几条原始消息进上下文。原文噪声大，不宜给多。"""
    return _int("RAG_RAW_TOP_K", 4)


def vec_min_sim() -> float:
    """纯向量召回（关键词零命中）时的最低余弦相似度，低于这个值不出现在结果里。

    向量模型的相似度普遍偏高——不相关的中文内容也常在 0.4 以上，
    所以不能只靠相对排序，得有一个绝对门槛。
    """
    raw = _str("RAG_VEC_MIN_SIM")
    try:
        return float(raw) if raw else 0.55
    except ValueError:
        return 0.55


def dingtalk_credentials() -> Optional[dict]:
    cid = _str("DINGTALK_CLIENT_ID")
    secret = _str("DINGTALK_CLIENT_SECRET")
    if not cid or not secret:
        return None
    return {
        "client_id": cid,
        "client_secret": secret,
        "robot_code": _str("DINGTALK_ROBOT_CODE", cid),
    }


def embedding_config() -> Optional[dict]:
    """OpenAI 兼容的 /embeddings 接口。不配置则退化为纯 BM25 检索。"""
    base = _str("EMBED_BASE_URL") or _str("LLM_BASE_URL")
    key = _str("EMBED_API_KEY") or _str("LLM_API_KEY")
    model = _str("EMBED_MODEL")
    if not (base and key and model):
        return None
    return {
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
        # 多数厂商对单次 input 条数有限制（百炼 text-embedding-v3 是 10 条），必须分批
        "batch": max(1, _int("EMBED_BATCH", 10)),
        # 0 表示不传 dimensions，用模型默认维度
        "dimensions": _int("EMBED_DIM", 0),
    }


def llm_config() -> Optional[dict]:
    base = _str("LLM_BASE_URL")
    key = _str("LLM_API_KEY")
    model = _str("LLM_MODEL")
    if not (base and key and model):
        return None
    return {
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
        "timeout": _int("LLM_TIMEOUT", 60),
    }


# ---------------------------------------------------------------- DWS（钉钉 CLI）

def dws_bin() -> str:
    return _str("DWS_BIN", "dws")


def dws_event_kind() -> str:
    """group_all=所有群消息 / group=指定群 / at=@我的消息 / o2o_all=所有单聊"""
    return _str("DWS_EVENT_KIND", "group_all")


def dws_history_limit() -> int:
    return _int("DWS_HISTORY_LIMIT", 200)


def dws_profile() -> str:
    return _str("DWS_PROFILE")
