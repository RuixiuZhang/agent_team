"""
OrchestratorAgent — 主编排器 + orchestrator 输入构建
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from .config import (
    ORCHESTRATOR_MODEL, WORKER_MODELS, SUB_ORCHESTRATOR_MODELS,
)
from .llm import send_chat, parse_json
from .models import (
    SubTask, OrchestratorPlan, WorkerResult,
    TaskStatus, IterationMetrics,
)
from .prompts import ORCHESTRATOR_SYSTEM_PROMPT, VALID_WORKER_ROLES
from .prompt_evolution import (
    prompt_store, DynamicPromptStore,
    record_worker_outcome, should_evolve_prompt,
    evolve_prompt, reset_fail_count,
)
from .hooks import HookManager
from .board import SharedTaskBoard
from .mailbox import Mailbox
from .engine import AsyncTaskEngine
from .utils import TaskDedup, compress_memory
from .backends import StateBackend, get_default_backend


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator 输入构建常量
# ─────────────────────────────────────────────────────────────────────────────

_MAX_CONTEXT_CHARS         = 8000
_RESULT_EXCERPT_LEN        = 600
_MEMORY_COMPRESS_THRESHOLD = 8
_CONSECUTIVE_SUCCESS_EXIT  = 2
_MAX_TASK_RETRIES          = 2
_MAX_EMPTY_BATCHES         = 5


def _build_orchestrator_input(
    objective: str,
    memory: List[Dict[str, Any]],
    task_counter: int,
    metrics_history: Optional[List[IterationMetrics]] = None,
    prompt_versions: Optional[Dict[str, int]] = None,
    board_status: Optional[Dict[str, int]] = None,
    mailbox_recent: Optional[List[dict]] = None,
) -> str:
    header = f"=== 用户目标 ===\n{objective}"

    dashboard = ""
    if metrics_history:
        total_ok   = sum(m.completed_tasks for m in metrics_history)
        total_fail = sum(m.failed_tasks for m in metrics_history)
        avg_rate   = sum(m.success_rate for m in metrics_history) / len(metrics_history)
        dashboard  = (
            f"\n=== 进度仪表盘 ===\n"
            f"轮次: {len(metrics_history)}  "
            f"成功: {total_ok}  失败: {total_fail}  "
            f"均率: {avg_rate:.0%}"
        )

    # 展示任务看板状态
    board_info = ""
    if board_status:
        board_info = "\n=== 任务看板 ===\n" + "  ".join(
            f"{k}: {v}" for k, v in board_status.items()
        )

    # 展示队友间最近消息
    mail_info = ""
    if mailbox_recent:
        lines = [
            f"  [{m.get('from','?')}→{m.get('to','?')}]: {m.get('content','')[:80]}"
            for m in mailbox_recent[-5:]
        ]
        if lines:
            mail_info = "\n=== 队友消息 ===\n" + "\n".join(lines)

    # 展示已进化的 prompt 版本
    evolve_info = ""
    if prompt_versions:
        evolved = {r: v for r, v in prompt_versions.items() if v > 0}
        if evolved:
            evolve_info = "\n=== Prompt 进化状态 ===\n" + "\n".join(
                f"  {role}: v{ver}" for role, ver in evolved.items()
            )

    footer = (
        f"\n下一个 task_id 从: t{task_counter}\n"
        "请输出 JSON 规划："
    )

    budget = (_MAX_CONTEXT_CHARS
              - len(header) - len(dashboard) - len(board_info)
              - len(mail_info) - len(evolve_info) - len(footer))
    history_parts = []
    used = 0

    for entry in reversed(memory):
        role    = entry.get("role", "")
        content = entry.get("content", "")
        if len(content) > _RESULT_EXCERPT_LEN:
            content = content[:_RESULT_EXCERPT_LEN] + "...(截断)"
        line = f"[{role}] {content}"
        if used + len(line) + 2 > budget:
            break
        history_parts.append(line)
        used += len(line) + 2

    history_parts.reverse()

    parts = [header]
    if dashboard:
        parts.append(dashboard)
    if board_info:
        parts.append(board_info)
    if mail_info:
        parts.append(mail_info)
    if evolve_info:
        parts.append(evolve_info)
    if history_parts:
        parts.append("\n=== 执行历史 ===")
        parts.extend(history_parts)
    parts.append(footer)

    return "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# OrchestratorAgent
# ─────────────────────────────────────────────────────────────────────────────

class OrchestratorAgent:

    def __init__(self, session_id: Optional[str] = None,
                 backend: Optional[StateBackend] = None):
        self.session_id   = session_id or str(uuid.uuid4())[:8]
        self.backend      = backend or get_default_backend()
        self.memory       : List[Dict[str, Any]]     = []
        self.task_counter = 0
        self.iteration    = 0
        self.status       : TaskStatus               = TaskStatus.PENDING
        self.objective    = ""
        self.result_map   : Dict[str, WorkerResult]  = {}
        self.metrics_history : List[IterationMetrics] = []
        self.dedup        = TaskDedup()
        self._consecutive_success_rounds = 0
        self._engine      : Optional[AsyncTaskEngine] = None

        # Agent-teams 基础设施
        self._board       : Optional[SharedTaskBoard] = None
        self._mailbox     = Mailbox()
        self._hooks       = HookManager()

        # Prompt 进化相关
        self._prompt_store = prompt_store
        self._fail_samples : Dict[str, List[Dict]] = {}  # role -> 失败样本列表

        # 注册默认 quality hooks
        self._register_default_hooks()

    # ── 默认 Hooks ────────────────────────────────────────────────────────

    def _register_default_hooks(self):
        """Install default quality gate hooks."""
        def _reject_empty(subtask: SubTask) -> bool:
            if not subtask.description.strip():
                print(f"    [hook] rejected {subtask.task_id}: empty description")
                return False
            return True
        self._hooks.add_task_created_hook(_reject_empty)

        def _log_completion(result: WorkerResult):
            if not result.success:
                print(f"    [hook] TaskCompleted FAIL: {result.task_id} "
                      f"({result.worker_role})")
        self._hooks.add_task_completed_hook(_log_completion)

    # ── 持久化 ──────────────────────────────────────────────────────────────

    def _snapshot(self) -> dict:
        return {
            "session_id":      self.session_id,
            "objective":       self.objective,
            "status":          self.status.value,
            "iteration":       self.iteration,
            "task_counter":    self.task_counter,
            "memory":          self.memory,
            "metrics_history": [m.model_dump() for m in self.metrics_history],
            "prompt_store":    self._prompt_store.snapshot(),
            "timestamp":       time.time(),
        }

    def _persist(self) -> None:
        self.backend.save_session(self.session_id, self._snapshot())

    def _restore(self) -> bool:
        data = self.backend.load_session(self.session_id)
        if not data:
            return False
        self.objective    = data.get("objective", "")
        self.status       = TaskStatus(data.get("status", "pending"))
        self.iteration    = data.get("iteration", 0)
        self.task_counter = data.get("task_counter", 0)
        self.memory       = data.get("memory", [])
        self.metrics_history = [
            IterationMetrics(**m) for m in data.get("metrics_history", [])
        ]
        if "prompt_store" in data:
            restored = DynamicPromptStore.from_snapshot(data["prompt_store"])
            # 更新全局 prompt_store 的内部状态
            prompt_store._prompts = restored._prompts
            prompt_store._history = restored._history
            self._prompt_store = prompt_store
        print(f"  [restore] session {self.session_id} (iteration {self.iteration})")
        return True

    # ── 规划 ────────────────────────────────────────────────────────────────

    def plan(self, objective: str) -> OrchestratorPlan:
        prompt_versions = {
            role: self._prompt_store.current_version(role)
            for role in VALID_WORKER_ROLES
        }
        board_status = self._board.dashboard() if self._board else None
        mailbox_recent = self._mailbox.recent_log(5) if self._mailbox else None
        context = _build_orchestrator_input(
            objective, self.memory, self.task_counter,
            self.metrics_history, prompt_versions,
            board_status, mailbox_recent,
        )
        raw    = send_chat(ORCHESTRATOR_MODEL, ORCHESTRATOR_SYSTEM_PROMPT,
                           context, timeout=180)
        parsed = parse_json(raw)

        if parsed is None:
            return OrchestratorPlan(is_complete=True, final_answer=raw)

        try:
            plan = OrchestratorPlan(**parsed)
        except Exception:
            final_answer = (
                parsed.get("final_answer", raw)
                if isinstance(parsed, dict) else raw
            )
            return OrchestratorPlan(is_complete=True, final_answer=final_answer)

        for st in plan.subtasks:
            self.task_counter += 1
            st.task_id = f"t{self.task_counter}"
            if isinstance(st.depends_on, str):
                st.depends_on = [st.depends_on]
            elif st.depends_on is None:
                st.depends_on = None

        return plan

    # ── Sub-Orchestrator 展开 ────────────────────────────────────────────────

    def _expand_sub_orchestrator_result(self, res: WorkerResult):
        parsed = parse_json(res.result)
        if not isinstance(parsed, dict) or "micro_tasks" not in parsed:
            return
        micro_list = parsed["micro_tasks"]
        if not isinstance(micro_list, list):
            return
        spawned = 0
        for mt in micro_list:
            if not isinstance(mt, dict):
                continue
            role = mt.get("assigned_worker", "analyst")
            if role not in VALID_WORKER_ROLES or role == "sub_orchestrator":
                role = "analyst"
            desc = mt.get("description", "")
            if not desc:
                continue
            self.task_counter += 1
            raw_dep = mt.get("depends_on")
            if isinstance(raw_dep, str):
                raw_dep = [raw_dep]
            st = SubTask(
                task_id=f"t{self.task_counter}", description=desc,
                assigned_worker=role, depends_on=raw_dep,
            )
            self._engine.submit(st, self.result_map)
            spawned += 1
        if spawned > 0:
            print(f"    [sub_orch] {res.task_id} -> {spawned} 微任务已派发")

    # ── 结果记录 & Prompt 进化触发 ──────────────────────────────────────────

    def _record_result(self, res: WorkerResult):
        self.result_map[res.task_id] = res

        if res.worker_role == "sub_orchestrator" and res.success:
            self._expand_sub_orchestrator_result(res)

        record_worker_outcome(res.worker_role, res.success)
        if not res.success:
            self._fail_samples.setdefault(res.worker_role, []).append({
                "task":   res.task_id,
                "result": res.result,
                "error":  res.error_output or res.review_feedback or "",
            })

        status_tag    = "OK" if res.success else "FAIL"
        entry_content = f"[{res.task_id}] {status_tag} ({res.latency:.1f}s): {res.result}"
        if res.error_output:
            entry_content += f"\nError: {res.error_output[:300]}"
        if res.review_feedback:
            entry_content += f"\nReview: {res.review_feedback[:300]}"
        self.memory.append({"role": f"worker:{res.worker_role}", "content": entry_content})

        preview = res.result[:80].replace("\n", " ")
        print(f"    [{status_tag}] {res.task_id} ({res.worker_role},"
              f" {res.latency:.1f}s): {preview}...")

    def _maybe_evolve_prompts(self):
        """每轮结束后检查是否触发 prompt 进化"""
        for role in list(self._fail_samples.keys()):
            if not should_evolve_prompt(role):
                continue
            samples = self._fail_samples[role]
            print(f"\n  [PromptEvolve] 🔧 {role} 触发进化（连续失败 {len(samples)} 次）...")
            rec = evolve_prompt(role, samples, self.iteration)
            if rec:
                reset_fail_count(role)
                self._fail_samples[role] = []
                self.memory.append({
                    "role": "system",
                    "content": (
                        f"[Prompt进化] {role} prompt 已升级至 v{rec.version}。"
                        f"原因: {rec.reason}"
                    ),
                })

    # ── 收割所有进行中任务 ──────────────────────────────────────────────────

    def _collect_all_pending(self, timeout_per_batch: float = 60) -> List[WorkerResult]:
        all_results  = []
        empty_streak = 0

        while self._engine.has_pending_work:
            batch = self._engine.collect(min_results=1, timeout=timeout_per_batch)
            if not batch:
                empty_streak += 1
                if empty_streak >= _MAX_EMPTY_BATCHES:
                    print(f"  [collect] ⚠ 连续 {_MAX_EMPTY_BATCHES} 次空 batch，强制退出")
                    break
                continue
            empty_streak = 0
            for res in batch:
                self._record_result(res)
                all_results.append(res)

        return all_results

    # ── 主循环 ──────────────────────────────────────────────────────────────

    def run(self, objective: str, max_iterations: int = 50,
            resume: bool = False) -> str:

        if resume and self._restore():
            if self.status == TaskStatus.COMPLETED:
                last = [m for m in self.memory if m.get("role") == "final_answer"]
                return last[-1]["content"] if last else ""
        else:
            self.objective = objective
            self.memory.append({"role": "user", "content": objective})

        self.status = TaskStatus.RUNNING
        self._persist()

        print(f"\n{'=' * 70}")
        print(f"  Orchestrator Start  [session: {self.session_id}]")
        print(f"  Brain: {ORCHESTRATOR_MODEL}")
        print(f"  Sub-Orchestrators: {len(SUB_ORCHESTRATOR_MODELS)} x 4B")
        print(f"  Workers: {len(WORKER_MODELS)} x 4B")
        print(f"  Backend: {self.backend.__class__.__name__}")
        print(f"  Features: SharedTaskBoard | ClaimScheduling | DAG")
        print(f"           | Mailbox | QualityHooks | PromptEvolve | StaleDetect")
        print(f"  Task: {objective}")
        print(f"{'=' * 70}")

        self._board  = SharedTaskBoard(stale_timeout=300)
        self._board.set_hooks(self._hooks)
        self._engine = AsyncTaskEngine(
            num_workers=len(WORKER_MODELS),
            board=self._board,
            mailbox=self._mailbox,
        )
        self._engine.start()
        start = time.perf_counter()

        try:
            while self.iteration < max_iterations:
                self.iteration += 1
                iter_start = time.perf_counter()

                print(f"\n{'-' * 50}")
                print(f"  Iteration {self.iteration}")
                print(f"{'-' * 50}")

                # 内存压缩
                worker_entries = [
                    e for e in self.memory if e.get("role", "").startswith("worker:")
                ]
                if len(worker_entries) > _MEMORY_COMPRESS_THRESHOLD:
                    print(f"  [compress] {len(worker_entries)} entries -> summary")
                    non_worker    = [e for e in self.memory
                                     if not e.get("role", "").startswith("worker:")]
                    compressed    = compress_memory(worker_entries)
                    self.memory   = non_worker + compressed

                # 规划
                print("  [Orchestrator] planning...")
                plan = self.plan(objective)

                if plan.is_complete:
                    elapsed = time.perf_counter() - start
                    answer  = plan.final_answer or ""
                    self.status = TaskStatus.COMPLETED
                    self.memory.append({"role": "final_answer", "content": answer})
                    self._persist()
                    print(f"\n  ✅ DONE [session {self.session_id}]"
                          f" ({self.iteration} iters, {elapsed:.1f}s)")
                    return answer

                # 过滤无效/重复任务
                valid_subtasks = []
                for st in plan.subtasks:
                    if st.assigned_worker not in VALID_WORKER_ROLES:
                        print(f"    unknown role '{st.assigned_worker}' -> analyst")
                        st.assigned_worker = "analyst"
                    is_dup, retry_count = self.dedup.check_and_record(st)
                    if is_dup and retry_count >= _MAX_TASK_RETRIES:
                        print(f"    skip dup {st.task_id} (retried {retry_count}x)")
                        self.memory.append({
                            "role": "system",
                            "content": f"Task {st.task_id} duplicated ({retry_count}x), skipped.",
                        })
                        continue
                    valid_subtasks.append(st)

                if not valid_subtasks:
                    self.memory.append({
                        "role": "system",
                        "content": "No valid subtasks this round. Re-plan or mark complete.",
                    })
                    self._persist()
                    continue

                sub_orch_count = sum(
                    1 for s in valid_subtasks if s.assigned_worker == "sub_orchestrator"
                )
                print(f"  Dispatching {len(valid_subtasks)} tasks "
                      f"({sub_orch_count} sub_orch + "
                      f"{len(valid_subtasks)-sub_orch_count} direct) -> AsyncQueue")

                for st in valid_subtasks:
                    self._engine.submit(st, self.result_map)

                all_results = self._collect_all_pending(timeout_per_batch=60)

                self._maybe_evolve_prompts()

                completed_count = sum(1 for r in all_results if r.success)
                failed_count    = sum(1 for r in all_results if not r.success)
                total_planned   = len(all_results) if all_results else len(valid_subtasks)
                iter_wall       = time.perf_counter() - iter_start
                success_rate    = completed_count / total_planned if total_planned else 0

                metrics = IterationMetrics(
                    iteration=self.iteration,
                    planned_tasks=total_planned,
                    completed_tasks=completed_count,
                    failed_tasks=failed_count,
                    wall_time=round(iter_wall, 2),
                    success_rate=round(success_rate, 4),
                )
                self.metrics_history.append(metrics)
                print(f"  Stats: {completed_count} ok / {failed_count} fail "
                      f"({success_rate:.0%}) in {iter_wall:.1f}s"
                      f"  board: {self._board.dashboard() if self._board else {}}")

                if failed_count == 0 and completed_count > 0:
                    self._consecutive_success_rounds += 1
                else:
                    self._consecutive_success_rounds = 0

                if self._consecutive_success_rounds >= _CONSECUTIVE_SUCCESS_EXIT:
                    print(f"  [info] {self._consecutive_success_rounds} 轮连续成功，等待 Orchestrator 自行判断是否完成")

                self._persist()

            # 安全兜底：超出最大轮次
            print(f"\n  Safety limit ({max_iterations}) reached, forcing final answer...")
            self.memory.append({
                "role": "system",
                "content": "Safety iteration limit reached. You MUST set is_complete=true now and provide the best answer with all files saved.",
            })
            final  = self.plan(objective)
            answer = final.final_answer or ""
            self.status = TaskStatus.COMPLETED
            self.memory.append({"role": "final_answer", "content": answer})
            self._persist()
            return answer

        finally:
            self._engine.stop()
            self._engine = None
