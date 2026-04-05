"""
搜索引擎 — DuckDuckGo 搜索 + 缓存
"""

import re
import time
from typing import Dict, List

from ddgs import DDGS


_ddgs = None
_search_cache: Dict[str, tuple] = {}
_CACHE_TTL = 60


def _get_ddgs():
    global _ddgs
    if _ddgs is None:
        _ddgs = DDGS()
    return _ddgs


def _reset_ddgs():
    global _ddgs
    _ddgs = DDGS()
    return _ddgs


def _do_search(query: str, num_results: int) -> List[dict]:
    for attempt in range(2):
        try:
            ddgs = _get_ddgs() if attempt == 0 else _reset_ddgs()
            results = list(ddgs.text(query, max_results=num_results))
            if results:
                return results
        except Exception:
            if attempt == 0:
                continue
            raise
    return []


def execute_web_search(query: str, num_results: int = 5) -> str:
    now = time.monotonic()
    cached = _search_cache.get(query)
    if cached and (now - cached[0]) < _CACHE_TTL:
        print(f"\n[Agent 缓存命中] 关键词: '{query}'")
        return cached[1]

    print(f"\n[Agent 正在执行搜索] 关键词: '{query}'...")
    try:
        results = _do_search(query, num_results)
        if not results:
            print(f"[Agent 搜索无结果，尝试变体] '{query}'")
            simplified = re.sub(r'\d{4}年?', '', query).strip()
            if simplified and simplified != query:
                results = _do_search(simplified, num_results)

        if not results:
            return f"未找到与 '{query}' 相关的搜索结果。"

        formatted = [
            f"来源 [{i}]\n标题: {r['title']}\n链接: {r['href']}\n摘要: {r['body']}\n"
            for i, r in enumerate(results, 1)
        ]
        text = "\n".join(formatted)
        _search_cache[query] = (now, text)
        return text
    except Exception as e:
        return f"搜索执行出错: {str(e)}"
