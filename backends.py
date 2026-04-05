"""
状态持久化后端 — File / Redis / Postgres
"""

import abc
import json
import os
import re
from typing import List, Optional


class StateBackend(abc.ABC):
    @abc.abstractmethod
    def save_session(self, session_id: str, data: dict) -> None: ...
    @abc.abstractmethod
    def load_session(self, session_id: str) -> Optional[dict]: ...
    @abc.abstractmethod
    def delete_session(self, session_id: str) -> None: ...
    @abc.abstractmethod
    def list_sessions(self) -> List[str]: ...


class FileBackend(StateBackend):
    def __init__(self, directory: str = ".agent_sessions"):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, session_id: str) -> str:
        safe_id = re.sub(r"[^\w\-]", "_", session_id)
        return os.path.join(self.directory, f"{safe_id}.json")

    def save_session(self, session_id: str, data: dict) -> None:
        with open(self._path(session_id), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def load_session(self, session_id: str) -> Optional[dict]:
        path = self._path(session_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete_session(self, session_id: str) -> None:
        path = self._path(session_id)
        if os.path.exists(path):
            os.unlink(path)

    def list_sessions(self) -> List[str]:
        return [
            f.replace(".json", "")
            for f in os.listdir(self.directory)
            if f.endswith(".json")
        ]


class RedisBackend(StateBackend):
    def __init__(self, url: str = "redis://localhost:6379/0", ttl: int = 86400):
        import redis
        self.client = redis.from_url(url, decode_responses=True)
        self.ttl    = ttl
        self.prefix = "agent_team:"

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}{session_id}"

    def save_session(self, session_id: str, data: dict) -> None:
        self.client.setex(
            self._key(session_id), self.ttl,
            json.dumps(data, ensure_ascii=False, default=str),
        )

    def load_session(self, session_id: str) -> Optional[dict]:
        raw = self.client.get(self._key(session_id))
        return json.loads(raw) if raw else None

    def delete_session(self, session_id: str) -> None:
        self.client.delete(self._key(session_id))

    def list_sessions(self) -> List[str]:
        keys = self.client.keys(f"{self.prefix}*")
        return [k.removeprefix(self.prefix) for k in keys]


class PostgresBackend(StateBackend):
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_sessions (
        session_id TEXT PRIMARY KEY,
        state JSONB NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_updated
        ON agent_sessions (updated_at DESC);
    """

    def __init__(self, dsn: str = "postgresql://localhost/agent_team"):
        import psycopg2
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute(self.DDL)

    def save_session(self, session_id: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str)
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_sessions (session_id, state, updated_at)
                   VALUES (%s, %s, NOW())
                   ON CONFLICT (session_id)
                   DO UPDATE SET state = EXCLUDED.state, updated_at = NOW()""",
                (session_id, payload),
            )

    def load_session(self, session_id: str) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM agent_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
            if row:
                return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return None

    def delete_session(self, session_id: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_sessions WHERE session_id = %s", (session_id,)
            )

    def list_sessions(self) -> List[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM agent_sessions ORDER BY updated_at DESC"
            )
            return [row[0] for row in cur.fetchall()]


_default_backend: StateBackend = FileBackend()


def set_backend(backend: StateBackend) -> None:
    global _default_backend
    _default_backend = backend


def get_default_backend() -> StateBackend:
    return _default_backend
