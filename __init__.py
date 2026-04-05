"""
Agent Team — Async Queue + Sub-Orchestrator + Dynamic Prompt Evolution

公开 API:
    ask_team(question, max_iterations, session_id)
    list_sessions()
    resume_session(session_id)
    get_prompt_history(role)
    rollback_prompt(role)
    set_backend(backend)
"""

from typing import List, Optional

from .orchestrator import OrchestratorAgent
from .prompt_evolution import prompt_store
from .backends import get_default_backend, set_backend  # noqa: F401
from .backends import (  # noqa: F401 — 方便外部直接 import
    StateBackend, FileBackend, RedisBackend, PostgresBackend,
)
from .config import WORKER_MODELS  # noqa: F401
from .workers import MAX_FIX_ROUNDS  # noqa: F401
from .prompt_evolution import EVOLVE_FAIL_THRESHOLD  # noqa: F401


def ask_team(question: str, max_iterations: int = 50,
             session_id: Optional[str] = None) -> str:
    resume = session_id is not None
    orchestrator = OrchestratorAgent(session_id=session_id)
    return orchestrator.run(question, max_iterations=max_iterations, resume=resume)


def list_sessions() -> List[str]:
    return get_default_backend().list_sessions()


def resume_session(session_id: str) -> str:
    orchestrator = OrchestratorAgent(session_id=session_id)
    if not orchestrator._restore():
        return f"Session not found: {session_id}"
    return orchestrator.run(orchestrator.objective, max_iterations=50, resume=True)


def get_prompt_history(role: str) -> List[dict]:
    """查看某个 worker 的 prompt 版本历史"""
    return [r.model_dump() for r in prompt_store.get_history(role)]


def rollback_prompt(role: str) -> bool:
    """将某个 worker 的 prompt 回滚到上一版本"""
    success = prompt_store.rollback(role)
    if success:
        print(f"[rollback] {role} prompt 已回滚至上一版本")
    else:
        print(f"[rollback] {role} 无历史版本可回滚")
    return success
