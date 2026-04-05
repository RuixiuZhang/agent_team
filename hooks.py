"""
HookManager — 质量门 (quality gates)
"""

from typing import List, Optional

from .models import SubTask, WorkerResult


class HookManager:
    """Pluggable quality gates for task lifecycle events.
    Hook callbacks return True to proceed, False to reject/feedback."""

    def __init__(self):
        self._task_created_hooks   : List = []
        self._task_completed_hooks : List = []
        self._teammate_idle_hooks  : List = []

    def add_task_created_hook(self, fn):
        self._task_created_hooks.append(fn)

    def add_task_completed_hook(self, fn):
        self._task_completed_hooks.append(fn)

    def add_teammate_idle_hook(self, fn):
        self._teammate_idle_hooks.append(fn)

    def on_task_created(self, subtask: SubTask) -> bool:
        for hook in self._task_created_hooks:
            if not hook(subtask):
                print(f"    [hook] TaskCreated rejected: {subtask.task_id}")
                return False
        return True

    def on_task_completed(self, result: WorkerResult):
        for hook in self._task_completed_hooks:
            hook(result)

    def on_teammate_idle(self, worker_id: str) -> Optional[str]:
        """Returns feedback string to keep worker busy, or None."""
        for hook in self._teammate_idle_hooks:
            feedback = hook(worker_id)
            if feedback:
                return feedback
        return None
