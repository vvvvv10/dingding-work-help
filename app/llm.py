"""大模型调用（OpenAI 兼容 /chat/completions），只用标准库。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from . import config


class LLMError(RuntimeError):
    pass


def is_configured() -> bool:
    return config.llm_config() is not None


def chat(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 1200,
    json_mode: bool = False,
) -> Optional[str]:
    cfg = config.llm_config()
    if cfg is None:
        return None

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % cfg["api_key"],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 60)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:400]
        except Exception:  # noqa: BLE001
            pass
        raise LLMError("模型接口返回 %s：%s" % (e.code, detail)) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise LLMError("模型接口不可达：%s" % e) from e

    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError("模型返回结构异常：%s" % str(body)[:300]) from e


_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


def parse_json_array(raw: str) -> List[dict]:
    """从模型输出里尽力取出 JSON 数组，兼容各种不规范的包裹。"""
    if not raw:
        return []
    cleaned = _FENCE.sub("", raw).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("data") or []
    return [x for x in data if isinstance(x, dict)]
