"""中文友好的分词、切块与文本清洗。

SQLite 自带的 FTS5 对中文几乎不可用（整段汉字会被切成一个 token），
所以这里自己把文本切成「字二元组 + 英文单词」，再写进 FTS5 索引。
检索时用同样的方式切查询词，命中率接近搜索引擎的 bigram 方案。
"""
from __future__ import annotations

import re
import time
from typing import Iterable, List

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff"
_TOKEN_RE = re.compile(r"[%s]+|[A-Za-z][A-Za-z0-9_+\-#.]*|\d+(?:\.\d+)?" % _CJK)

_SIZE_RE = re.compile(r"^\[?(图片|语音|视频|文件|表情|链接|卡片|红包)\]?$")


def is_cjk(s: str) -> bool:
    return bool(s) and all(_TOKEN_RE.fullmatch(ch) and not ch.isascii() for ch in s[:1])


def tokenize(text: str) -> List[str]:
    """返回检索用的 token 列表（中文 bigram，英文/数字小写单词）。"""
    out: List[str] = []
    for m in _TOKEN_RE.finditer(text or ""):
        run = m.group()
        if run[0].isascii():
            out.append(run.lower())
        elif len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def fts_query(text: str, limit: int = 40) -> str:
    """把自然语言查询转成 FTS5 的 OR 查询串。"""
    seen, tokens = set(), []
    for t in tokenize(text):
        if t in seen:
            continue
        seen.add(t)
        tokens.append(t)
        if len(tokens) >= limit:
            break
    if not tokens:
        return ""
    return " OR ".join('"%s"' % t.replace('"', "") for t in tokens)


def sentence_split_keep(text: str) -> List[str]:
    """按句末标点切句，保留标点。"""
    parts = re.split(r"(?<=[。！？；!?;\n])", text)
    return [p.strip() for p in parts if p and p.strip()]


def chunk_text(text: str, size: int = 320, overlap: int = 60) -> List[str]:
    """把文档切成大小相近、带少量重叠的片段。优先在句子边界断开。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: List[str] = []
    buf = ""
    for sent in sentence_split_keep(text):
        if len(buf) + len(sent) <= size:
            buf += sent
            continue
        if buf:
            chunks.append(buf.strip())
            buf = buf[-overlap:] if overlap and len(buf) > overlap else ""
        while len(sent) > size:
            chunks.append(sent[:size].strip())
            sent = sent[size - overlap :]
        buf += sent
    if buf.strip():
        chunks.append(buf.strip())
    return [c for c in chunks if len(c) >= 8]


def clean_mention(text: str) -> str:
    """去掉 @机器人 之类的残留标记。"""
    text = re.sub(r"@\S+\s*", "", text or "")
    return text.strip()


def is_noise(content: str) -> bool:
    """图片/语音占位符、单表情、极短寒暄，不值得沉淀。"""
    c = (content or "").strip()
    if not c:
        return True
    if _SIZE_RE.match(c):
        return True
    if len(c) <= 2:
        return True
    return False


def fmt_ts(ms: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except (ValueError, OSError, TypeError):
        return ""


_SECRET_PATTERNS = [
    # 不加 \b：真实场景里 token 常紧贴在用户名后面（如 nameghp_xxx）
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[已脱敏:GITHUB_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[已脱敏:GITHUB_TOKEN]"),
    (re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9_\-]{16,}"), "[已脱敏:API_KEY]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[已脱敏:AWS_KEY]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[已脱敏:SLACK_TOKEN]"),
    # key: value / key=value 形式
    (
        re.compile(
            r"(?i)\b(api[_\-]?key|secret|token|password|passwd|pwd|access[_\-]?key)\b\s*[:=：]\s*([^\s，,；;]{6,})"
        ),
        r"\1: [已脱敏]",
    ),
    # key 空格 value 形式（"API Key owJxLX..." "Secret abc123def"）
    (
        re.compile(
            r"(?i)\b(api[_\- ]?key|access[_\- ]?key|app[_\- ]?secret|client[_\- ]?secret|secret)\b\s*[:=：]?\s+([A-Za-z0-9_\-]{12,})"
        ),
        r"\1: [已脱敏]",
    ),
    # 中文说法
    (
        re.compile(r"(密钥|秘钥|口令|私钥|密码)\s*[:=：]?\s*([A-Za-z0-9_\-+/=]{8,})"),
        r"\1: [已脱敏]",
    ),
]


def redact_secrets(s: str) -> str:
    """把消息里的密钥类字符串替换掉，避免它们被检索出来。

    群聊里贴 token / 密钥是常见事故；这些东西一旦进了知识库，
    同群任何人都能问答出来，所以入库前统一脱敏。
    """
    if not s:
        return s
    for pattern, repl in _SECRET_PATTERNS:
        s = pattern.sub(repl, s)
    return s


def join_lines(items: Iterable[str]) -> str:
    return "\n".join(x for x in items if x)
