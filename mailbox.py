"""
Mailbox — 队友间直接通信 (inter-worker messaging)
"""

import time
import threading
from typing import Dict, List, Optional


class Mailbox:
    """Thread-safe inter-worker messaging. Workers can send targeted or
    broadcast messages that are consumed by recipients on their next turn."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._boxes   : Dict[str, List[dict]] = {}  # recipient -> [msg]
        self._log     : List[dict]             = []  # all messages log

    def send(self, from_id: str, to_id: str, content: str):
        msg = {"from": from_id, "to": to_id, "content": content,
               "ts": time.monotonic()}
        with self._lock:
            self._boxes.setdefault(to_id, []).append(msg)
            self._log.append(msg)

    def broadcast(self, from_id: str, content: str, exclude: Optional[List[str]] = None):
        msg_base = {"from": from_id, "content": content, "ts": time.monotonic()}
        with self._lock:
            for recipient in list(self._boxes.keys()):
                if exclude and recipient in exclude:
                    continue
                self._boxes[recipient].append({**msg_base, "to": recipient})
            self._log.append({**msg_base, "to": "*broadcast*"})

    def receive(self, worker_id: str) -> List[dict]:
        """Consume all pending messages for worker_id."""
        with self._lock:
            msgs = self._boxes.pop(worker_id, [])
        return msgs

    def peek(self, worker_id: str) -> int:
        """Non-destructive count of pending messages."""
        with self._lock:
            return len(self._boxes.get(worker_id, []))

    def recent_log(self, n: int = 10) -> List[dict]:
        with self._lock:
            return list(self._log[-n:])

    def register(self, worker_id: str):
        with self._lock:
            self._boxes.setdefault(worker_id, [])
