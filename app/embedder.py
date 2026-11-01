"""向量化（可选）。未配置 embedding 接口时整体退化为纯关键词检索。"""
from __future__ import annotations

import array
import json
import math
import urllib.error
import urllib.request
from typing import List, Optional

from . import config


def _post(url: str, payload: dict, api_key: str, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Embedder:
    enabled = False

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        return None


class RemoteEmbedder(Embedder):
    enabled = True

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not texts:
            return []
        size = int(self.cfg.get("batch") or 10)
        out: List[List[float]] = []
        for i in range(0, len(texts), size):
            part = self._embed_batch(texts[i : i + size])
            if part is None:
                return None
            out.extend(part)
        return out

    def _embed_batch(self, texts: List[str]) -> Optional[List[List[float]]]:
        payload = {"model": self.cfg["model"], "input": texts, "encoding_format": "float"}
        dim = int(self.cfg.get("dimensions") or 0)
        if dim > 0:
            payload["dimensions"] = dim
        try:
            body = _post(
                self.cfg["base_url"] + "/embeddings",
                payload,
                self.cfg["api_key"],
                timeout=self.cfg.get("timeout") or 30,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
            return None
        items = body.get("data") or []
        if len(items) != len(texts):
            return None
        try:
            # 不保证顺序的厂商先按 index 排回来
            if all("index" in it for it in items):
                items = sorted(items, key=lambda it: it["index"])
            return [it["embedding"] for it in items]
        except (KeyError, TypeError):
            return None


_embedder: Optional[Embedder] = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        cfg = config.embedding_config()
        _embedder = RemoteEmbedder(cfg) if cfg else Embedder()
    return _embedder


def reset() -> None:
    global _embedder
    _embedder = None


def pack(vec: List[float]) -> bytes:
    arr = array.array("f", vec)
    return arr.tobytes()


def unpack(blob: bytes) -> List[float]:
    arr = array.array("f")
    arr.frombytes(blob)
    return list(arr)


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
