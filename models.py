"""
数据模型 — TaskStatus / SubTask / OrchestratorPlan / WorkerResult / IterationMetrics
"""

from typing import List, Optional
from enum import Enum
from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING          = "pending"
    RUNNING          = "running"
    COMPLETED        = "completed"
    FAILED           = "failed"
    REVIEW_REJECTED  = "review_rejected"
    FIX_IN_PROGRESS  = "fix_in_progress"


class SubTask(BaseModel):
    task_id:          str                 = Field(description="全局唯一 ID，如 t1, t2")
    description:      str                 = Field(description="Worker 执行指令")
    assigned_worker:  str                 = Field(description="Worker 类型")
    depends_on:       Optional[List[str]] = Field(default=None, description="依赖的 task_id 列表")


class OrchestratorPlan(BaseModel):
    is_complete:  bool            = Field(description="是否已完成")
    subtasks:     List[SubTask]   = Field(default_factory=list)
    final_answer: Optional[str]   = Field(default=None)


class WorkerResult(BaseModel):
    task_id:         str
    worker_role:     str
    result:          str
    success:         bool          = True
    error_output:    Optional[str] = Field(default=None)
    review_passed:   Optional[bool]= Field(default=None)
    review_feedback: Optional[str] = Field(default=None)
    latency:         float         = Field(default=0.0)


class IterationMetrics(BaseModel):
    iteration:       int
    planned_tasks:   int
    completed_tasks: int
    failed_tasks:    int
    wall_time:       float
    success_rate:    float
