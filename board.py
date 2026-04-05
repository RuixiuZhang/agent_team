"""
SharedTaskBoard — 共享任务看板 (claim-based scheduling)
"""

import time
import threading
from typing import Dict, List, Optional

from .models import SubTask, WorkerResult, TaskStatus
from .hooks import HookManager


class _BoardEntry:
    __slots__ = ('subtask', 'status', 'claimed_by', 'claimed_at', 'created_at')
    def __init__(self, subtask: SubTask):
        self.subtask    = subtask
        self.status     = TaskStatus.PENDING
        self.claimed_by : Optional[str] = None
        self.claimed_at : Optional[float] = None
        self.created_at = time.monotonic()


class SharedTaskBoard:
    """
    Thread-safe shared task board (Claude Code agent-teams pattern).
    Workers claim tasks atomically via claim() instead of being pushed.
    Supports multi-dependency DAG, stale detection, and quality hooks.
    """

    def __init__(self, stale_timeout: float = 300):
        self._lock           = threading.Lock()
        self._tasks          : Dict[str, _BoardEntry]  = {}
        self._results        : Dict[str, WorkerResult]  = {}
        self._hooks          : Optional[HookManager]    = None
        self._stale_timeout  = stale_timeout
        self._on_unblock_cb  = None  # callback(subtask) when deps satisfied

    def set_hooks(self, hooks: HookManager):
        self._hooks = hooks

    def set_unblock_callback(self, cb):
        """Set callback invoked when a pending task becomes unblocked."""
        self._on_unblock_cb = cb

    def add(self, subtask: SubTask) -> bool:
        """Add task to board. Returns False if hook rejects it."""
        with self._lock:
            if self._hooks and not self._hooks.on_task_created(subtask):
                return False
            self._tasks[subtask.task_id] = _BoardEntry(subtask)
            return True

    def claim(self, worker_id: str) -> Optional[SubTask]:
        """Atomically claim next available unblocked task (FIFO order)."""
        with self._lock:
            for tid, entry in self._tasks.items():
                if entry.status != TaskStatus.PENDING:
                    continue
                if not self._deps_satisfied_locked(entry.subtask):
                    continue
                entry.status     = TaskStatus.RUNNING
                entry.claimed_by = worker_id
                entry.claimed_at = time.monotonic()
                return entry.subtask
            return None

    def complete(self, task_id: str, result: WorkerResult):
        """Mark task done, store result, notify unblocked dependents."""
        newly_unblocked = []
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id].status = (
                    TaskStatus.COMPLETED if result.success else TaskStatus.FAILED
                )
            self._results[task_id] = result
            if self._hooks:
                self._hooks.on_task_completed(result)
            # check which pending tasks just became unblocked
            for entry in self._tasks.values():
                if entry.status == TaskStatus.PENDING:
                    if self._deps_satisfied_locked(entry.subtask):
                        newly_unblocked.append(entry.subtask)
        # notify outside lock to avoid deadlock
        if self._on_unblock_cb:
            for st in newly_unblocked:
                self._on_unblock_cb(st)

    def get_result(self, task_id: str) -> Optional[WorkerResult]:
        with self._lock:
            return self._results.get(task_id)

    def get_all_results(self) -> Dict[str, WorkerResult]:
        with self._lock:
            return dict(self._results)

    def has_pending(self) -> bool:
        with self._lock:
            return any(
                e.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
                for e in self._tasks.values()
            )

    def reclaim_stale(self) -> int:
        """Detect tasks running too long and reset to PENDING."""
        now = time.monotonic()
        reclaimed = 0
        with self._lock:
            for entry in self._tasks.values():
                if (entry.status == TaskStatus.RUNNING and
                        entry.claimed_at and
                        now - entry.claimed_at > self._stale_timeout):
                    print(f"    [stale] {entry.subtask.task_id} stuck "
                          f"{now - entry.claimed_at:.0f}s, reclaiming")
                    entry.status     = TaskStatus.PENDING
                    entry.claimed_by = None
                    entry.claimed_at = None
                    reclaimed += 1
        return reclaimed

    def dashboard(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for e in self._tasks.values():
                counts[e.status.value] = counts.get(e.status.value, 0) + 1
            return counts

    def _deps_satisfied_locked(self, subtask: SubTask) -> bool:
        deps = subtask.depends_on or []
        return all(
            dep_id in self._results and self._results[dep_id].success
            for dep_id in deps
        )
