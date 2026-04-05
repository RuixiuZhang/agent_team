"""
工具函数 — 任务去重 / DAG 依赖注入 / 内存压缩
"""

import re
from typing import Any, Dict, List

from .config import next_worker_model
from .llm import send_chat
from .models import SubTask, WorkerResult
from .prompts import MEMORY_COMPRESS_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# 任务去重
# ─────────────────────────────────────────────────────────────────────────────

class TaskDedup:
    def __init__(self):
        self._seen: Dict[str, int] = {}

    def _hash(self, desc: str) -> str:
        return re.sub(r"\s+", "", desc.lower())[:100]

    def check_and_record(self, subtask: SubTask) -> tuple:
        h     = self._hash(subtask.description)
        count = self._seen.get(h, 0)
        self._seen[h] = count + 1
        return count > 0, count


# ─────────────────────────────────────────────────────────────────────────────
# DAG 调度辅助
# ─────────────────────────────────────────────────────────────────────────────

def inject_dependency_context(subtask: SubTask,
                              result_map: Dict[str, WorkerResult]) -> SubTask:
    deps = subtask.depends_on or []
    satisfied = [did for did in deps if did in result_map]
    if not satisfied:
        return subtask

    dep_blocks = []
    for dep_id in satisfied:
        dep_result  = result_map[dep_id]
        dep_excerpt = dep_result.result[:600]
        dep_blocks.append(
            f"=== 上游结果 {dep_result.task_id} ({dep_result.worker_role}) ===\n"
            f"{dep_excerpt}"
        )

    enriched_desc = subtask.description + "\n\n" + "\n\n".join(dep_blocks)
    return SubTask(
        task_id=subtask.task_id,
        description=enriched_desc,
        assigned_worker=subtask.assigned_worker,
        depends_on=subtask.depends_on,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 内存压缩
# ─────────────────────────────────────────────────────────────────────────────

def compress_memory(entries: List[Dict[str, Any]],
                    max_entries: int = 6) -> List[Dict[str, Any]]:
    if len(entries) <= max_entries:
        return entries

    keep_recent  = max_entries // 2
    old_entries  = entries[:-keep_recent]
    recent_entries = entries[-keep_recent:]

    old_text = "\n".join(
        f"[{e.get('role', '?')}] {e.get('content', '')[:200]}"
        for e in old_entries
    )
    try:
        summary = send_chat(next_worker_model(), MEMORY_COMPRESS_PROMPT,
                            old_text, timeout=30)
    except Exception:
        summary = f"(已压缩 {len(old_entries)} 条记录)"

    return [{"role": "memory_summary", "content": summary}] + recent_entries
