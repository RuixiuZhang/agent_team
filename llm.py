"""
LLM 通信 — send_chat / parse_json / extract_code
"""

import json
from typing import Any, Optional

from .config import (
    BASE_API_URL, _session,
    RE_THINK, RE_CODE_OPEN, RE_CODE_CLOSE,
    RE_PYTHON_BLOCK, RE_ANY_BLOCK,
)


def send_chat(model_id: str, sys_prompt: str, user_input: str,
              timeout: int = 120) -> str:
    payload = {
        "model": model_id,
        "system_prompt": sys_prompt,
        "input": user_input,
    }
    resp = _session.post(BASE_API_URL, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"LM Studio HTTP {resp.status_code}: {resp.text[:200]}")

    res_json = resp.json()
    raw = res_json.get("output", "")

    if isinstance(raw, list):
        parts = [
            item.get("content", "")
            for item in raw
            if isinstance(item, dict) and item.get("type") == "message"
        ]
        if not parts:
            parts = [
                item.get("content", "") if isinstance(item, dict) else str(item)
                for item in raw
            ]
        return "".join(parts)
    elif isinstance(raw, dict):
        return raw.get("content", str(raw))
    return str(raw) if raw else ""


def parse_json(text: str) -> Optional[Any]:
    """从 LLM 输出中提取 JSON（对象或数组均支持）"""
    cleaned = RE_THINK.sub("", text).strip()
    cleaned = RE_CODE_OPEN.sub("", cleaned)
    cleaned = RE_CODE_CLOSE.sub("", cleaned).strip()

    arr_start = cleaned.find("[")
    arr_end   = cleaned.rfind("]")
    obj_start = cleaned.find("{")
    obj_end   = cleaned.rfind("}")

    candidates = []
    if arr_start != -1 and arr_end > arr_start:
        candidates.append((arr_start, cleaned[arr_start: arr_end + 1]))
    if obj_start != -1 and obj_end > obj_start:
        candidates.append((obj_start, cleaned[obj_start: obj_end + 1]))

    candidates.sort(key=lambda x: x[0])
    for _, snippet in candidates:
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            continue
    return None


def extract_code(text: str) -> Optional[str]:
    m = RE_PYTHON_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    m = RE_ANY_BLOCK.search(text)
    if m:
        return m.group(1).strip()
    return None
