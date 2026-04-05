"""
动态 Prompt 进化系统 — DynamicPromptStore + 进化逻辑
"""

import threading
from typing import Dict, List, Optional

from pydantic import BaseModel

from .config import ORCHESTRATOR_MODEL, next_worker_model
from .llm import send_chat, parse_json
from .prompts import WORKER_PROMPTS, PROMPT_EVOLVER_SYSTEM


class PromptEvolutionRecord(BaseModel):
    worker_role:          str
    version:              int = 1
    prompt:               str
    evolved_at_iteration: int
    reason:               str = ""


class DynamicPromptStore:
    """线程安全的动态 prompt 仓库，支持版本追踪、回滚和持久化"""

    def __init__(self):
        self._lock    = threading.RLock()
        self._prompts: Dict[str, str]                         = dict(WORKER_PROMPTS)
        self._history: Dict[str, List[PromptEvolutionRecord]] = {}

    def get(self, role: str) -> str:
        with self._lock:
            return self._prompts.get(role, WORKER_PROMPTS.get(role, ""))

    def update(self, role: str, new_prompt: str,
               iteration: int, reason: str = "") -> PromptEvolutionRecord:
        with self._lock:
            version = len(self._history.get(role, [])) + 1
            rec = PromptEvolutionRecord(
                worker_role=role, version=version,
                prompt=new_prompt, evolved_at_iteration=iteration, reason=reason,
            )
            self._history.setdefault(role, []).append(rec)
            self._prompts[role] = new_prompt
            return rec

    def rollback(self, role: str) -> bool:
        with self._lock:
            hist = self._history.get(role, [])
            if len(hist) < 2:
                return False
            hist.pop()
            self._prompts[role] = hist[-1].prompt
            return True

    def get_history(self, role: str) -> List[PromptEvolutionRecord]:
        with self._lock:
            return list(self._history.get(role, []))

    def current_version(self, role: str) -> int:
        with self._lock:
            return len(self._history.get(role, []))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "prompts": dict(self._prompts),
                "history": {
                    role: [r.model_dump() for r in recs]
                    for role, recs in self._history.items()
                },
            }

    @classmethod
    def from_snapshot(cls, data: dict) -> "DynamicPromptStore":
        store = cls()
        store._prompts = data.get("prompts", dict(WORKER_PROMPTS))
        for role, recs in data.get("history", {}).items():
            store._history[role] = [PromptEvolutionRecord(**r) for r in recs]
        return store


# 全局 prompt 仓库（单例，各 OrchestratorAgent 共享）
prompt_store = DynamicPromptStore()

# 失败计数器（role -> 连续失败次数）
_role_fail_counts: Dict[str, int] = {}
_role_fail_lock = threading.Lock()

# 触发进化的连续失败阈值
EVOLVE_FAIL_THRESHOLD = 2


def record_worker_outcome(role: str, success: bool) -> None:
    with _role_fail_lock:
        if success:
            _role_fail_counts[role] = 0
        else:
            _role_fail_counts[role] = _role_fail_counts.get(role, 0) + 1


def should_evolve_prompt(role: str) -> bool:
    # sub_orchestrator 和 reviewer 的 prompt 格式要求很严，不自动进化
    if role in ("sub_orchestrator", "reviewer"):
        return False
    with _role_fail_lock:
        return _role_fail_counts.get(role, 0) >= EVOLVE_FAIL_THRESHOLD


def evolve_prompt(
    role: str,
    failure_samples: List[Dict],
    iteration: int,
) -> Optional[PromptEvolutionRecord]:
    """调用大模型分析失败样本，改写指定 worker 的 system prompt"""
    current_prompt = prompt_store.get(role)
    current_ver    = prompt_store.current_version(role)

    samples_text = "\n\n".join(
        f"【失败案例 {i+1}】\n任务: {s['task'][:200]}\n"
        f"输出片段: {s['result'][:300]}\n"
        f"错误/问题: {s.get('error', '输出质量不达标')[:200]}"
        for i, s in enumerate(failure_samples[-3:])
    )

    user_input = (
        f"Worker 角色: {role}\n"
        f"当前版本: v{current_ver}\n\n"
        f"当前 system prompt:\n{current_prompt}\n\n"
        f"失败案例（需要针对性修复）:\n{samples_text}\n\n"
        "请输出改进后的 system prompt。"
    )

    try:
        raw    = send_chat(ORCHESTRATOR_MODEL, PROMPT_EVOLVER_SYSTEM, user_input, timeout=60)
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            return None

        new_prompt = parsed.get("updated_prompt", "").strip()
        reason     = parsed.get("reason", "")
        if not new_prompt:
            return None

        rec = prompt_store.update(role, new_prompt, iteration, reason)
        print(f"\n  [PromptEvolve] ✨ {role} prompt 升级至 v{rec.version}: {reason[:80]}")
        return rec
    except Exception as e:
        print(f"\n  [PromptEvolve] ⚠ {role} 进化失败: {e}")
        return None


def reset_fail_count(role: str) -> None:
    with _role_fail_lock:
        _role_fail_counts[role] = 0
