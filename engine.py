"""
AsyncTaskEngine — claim-based 异步任务引擎（集成 SharedTaskBoard + Mailbox）
"""

import time
import queue
import threading
from typing import Dict, List, Optional

from .models import SubTask, WorkerResult
from .board import SharedTaskBoard
from .mailbox import Mailbox
from .workers import execute_subtask
from .utils import inject_dependency_context


class AsyncTaskEngine:

    def __init__(self, num_workers: int = 6,
                 board: Optional[SharedTaskBoard] = None,
                 mailbox: Optional[Mailbox] = None):
        self.board       = board or SharedTaskBoard()
        self.mailbox     = mailbox or Mailbox()
        self.results     : queue.Queue = queue.Queue()
        self._threads    : List[threading.Thread] = []
        self._running    = False
        self._stop_sent  = False
        self.num_workers = num_workers
        # wake event: 当新任务被解锁/添加时唤醒空闲 worker
        self._wake       = threading.Event()
        # stale 扫描间隔
        self._stale_interval = 60

    def start(self):
        if self._running:
            return
        self._running   = True
        self._stop_sent = False
        # register worker mailboxes
        for i in range(self.num_workers):
            self.mailbox.register(f"worker_{i}")
        # set unblock callback to wake sleeping workers
        self.board.set_unblock_callback(lambda _st: self._wake.set())
        for i in range(self.num_workers):
            t = threading.Thread(target=self._worker_loop, args=(i,), daemon=True)
            t.start()
            self._threads.append(t)
        # stale scanner thread
        t = threading.Thread(target=self._stale_scanner, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self):
        if not self._running or self._stop_sent:
            return
        self._running   = False
        self._stop_sent = True
        self._wake.set()  # wake all waiting workers
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    def submit(self, subtask: SubTask,
               result_map: Optional[Dict[str, WorkerResult]] = None):
        """Add task to SharedTaskBoard. Deps are checked at claim time."""
        # inject dep context if all deps already resolved
        if subtask.depends_on and result_map:
            deps = subtask.depends_on
            if all(d in result_map for d in deps):
                subtask = inject_dependency_context(subtask, result_map)
        self.board.add(subtask)
        self._wake.set()  # wake idle workers

    def collect(self, min_results: int = 1,
                timeout: float = 30) -> List[WorkerResult]:
        """Harvest results. Wait for min_results, then drain remaining."""
        collected = []
        deadline  = time.monotonic() + timeout

        while len(collected) < min_results:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r = self.results.get(timeout=min(remaining, 0.5))
                collected.append(r)
            except queue.Empty:
                if not self.board.has_pending():
                    break
                continue

        # drain remaining ready results
        while True:
            try:
                r = self.results.get_nowait()
                collected.append(r)
            except queue.Empty:
                break
        return collected

    @property
    def has_pending_work(self) -> bool:
        return self.board.has_pending()

    def _worker_loop(self, worker_idx: int):
        worker_id = f"worker_{worker_idx}"
        while self._running:
            # try to claim a task
            subtask = self.board.claim(worker_id)
            if subtask is None:
                # idle — wait for wake signal
                self._wake.wait(timeout=2.0)
                self._wake.clear()
                continue

            # inject mailbox messages as additional context
            msgs = self.mailbox.receive(worker_id)
            if msgs:
                msg_text = "\n".join(
                    f"[来自 {m['from']}]: {m['content']}" for m in msgs
                )
                subtask = SubTask(
                    task_id=subtask.task_id,
                    description=(
                        subtask.description +
                        f"\n\n=== 队友消息 ===\n{msg_text}"
                    ),
                    assigned_worker=subtask.assigned_worker,
                    depends_on=subtask.depends_on,
                )

            # inject resolved dep context at claim time
            result_map = self.board.get_all_results()
            if subtask.depends_on:
                subtask = inject_dependency_context(subtask, result_map)

            result = execute_subtask(subtask)
            self.board.complete(subtask.task_id, result)
            self.results.put(result)

    def _stale_scanner(self):
        while self._running:
            time.sleep(self._stale_interval)
            reclaimed = self.board.reclaim_stale()
            if reclaimed > 0:
                self._wake.set()
