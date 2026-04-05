"""
Worker 执行函数 — 各类 Worker 实现 + 统一分发入口
"""

import os
import time

from .config import next_worker_model, next_sub_orch_model, pool
from .llm import send_chat, parse_json, extract_code
from .models import SubTask, WorkerResult
from .prompts import (
    SEARCH_SYNTHESIS_PROMPT, CODER_FIX_PROMPT, VALID_WORKER_ROLES,
)
from .prompt_evolution import prompt_store
from .search import execute_web_search
from .sandbox import sandbox_exec
from .file_ops import (
    LOCAL_FILE_DIR, FILE_WORKSPACE,
    execute_file_op, execute_local_file_op, execute_doc_op,
)


# ─────────────────────────────────────────────────────────────────────────────
# Searcher Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_searcher(subtask: SubTask) -> WorkerResult:
    model_id = next_worker_model()
    kw_text  = send_chat(model_id, prompt_store.get("searcher"),
                         subtask.description, timeout=30)
    decision = parse_json(kw_text)

    queries = decision.get("queries", []) if isinstance(decision, dict) else []
    if not queries:
        queries = [subtask.description[:100]]

    # 并发搜索（最多 3 个关键词）
    search_futures = [pool.submit(execute_web_search, q) for q in queries[:3]]
    search_results = [f.result() for f in search_futures]
    valid = [r for r in search_results
             if not r.startswith("未找到") and not r.startswith("搜索执行出错")]

    if not valid:
        print("\n[Agent 关键词全部未命中，使用原始问题搜索]")
        fallback = execute_web_search(subtask.description[:100])
        if not fallback.startswith("未找到"):
            valid = [fallback]

    if not valid:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="searcher",
            result="抱歉，搜索未找到相关结果，无法回答该问题。", success=False,
        )

    combined   = "\n\n".join(valid)
    syn_prompt = SEARCH_SYNTHESIS_PROMPT.format(search_results=combined)
    print("\n[Agent 正在总结回答]...")
    answer = send_chat(next_worker_model(), syn_prompt, subtask.description, timeout=60)
    return WorkerResult(task_id=subtask.task_id, worker_role="searcher", result=answer)


# ─────────────────────────────────────────────────────────────────────────────
# Coder Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_coder(subtask: SubTask) -> WorkerResult:
    model_id   = next_worker_model()
    raw_output = send_chat(model_id, prompt_store.get("coder"),
                           subtask.description, timeout=120)
    code = extract_code(raw_output)

    if not code:
        return WorkerResult(task_id=subtask.task_id, worker_role="coder", result=raw_output)

    exec_ok, exec_output = sandbox_exec(code)
    if exec_ok:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="coder",
            result=f"```python\n{code}\n```\n\n执行输出:\n{exec_output}",
        )
    else:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="coder",
            result=f"```python\n{code}\n```",
            success=False, error_output=exec_output,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_reviewer(subtask: SubTask, code_to_review: str = "") -> WorkerResult:
    model_id     = next_worker_model()
    review_input = subtask.description
    if code_to_review:
        review_input += f"\n\n=== 待审查代码 ===\n{code_to_review}"

    raw    = send_chat(model_id, prompt_store.get("reviewer"), review_input, timeout=60)
    parsed = parse_json(raw)

    passed   = True
    feedback = ""
    if isinstance(parsed, dict):
        passed   = parsed.get("passed", True)
        issues   = parsed.get("issues", [])
        suggestions = parsed.get("suggestions", "")
        feedback = f"问题: {issues}\n建议: {suggestions}" if not passed else ""
    else:
        feedback = raw  # 非 dict 时把原文当 feedback

    return WorkerResult(
        task_id=subtask.task_id, worker_role="reviewer",
        result=raw,
        review_passed=passed,
        review_feedback=feedback if not passed else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Generic Worker (writer / analyst / etc.)
# ─────────────────────────────────────────────────────────────────────────────

def _execute_generic(subtask: SubTask) -> WorkerResult:
    role       = subtask.assigned_worker
    model_id   = next_worker_model()
    sys_prompt = prompt_store.get(role)
    result_text = send_chat(model_id, sys_prompt, subtask.description, timeout=120)

    # ── 自动保存 writer/analyst 成果到 files/ ──
    save_msg = ""
    if role in ("writer", "analyst") and len(result_text) > 50:
        try:
            os.makedirs(LOCAL_FILE_DIR, exist_ok=True)
            ext = ".md"
            filename = f"{subtask.task_id}_{role}{ext}"
            filepath = os.path.join(LOCAL_FILE_DIR, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(result_text)
            save_msg = f"\n\n[已自动保存到 files/{filename}]"
            print(f"     [自动保存] files/{filename}")
        except Exception as e:
            save_msg = f"\n\n[自动保存失败: {e}]"

    return WorkerResult(task_id=subtask.task_id, worker_role=role,
                        result=result_text + save_msg)


# ─────────────────────────────────────────────────────────────────────────────
# Doc Reader Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_doc_reader(subtask: SubTask) -> WorkerResult:
    """文档读写 Worker 执行入口"""
    model_id = next_worker_model()
    os.makedirs(LOCAL_FILE_DIR, exist_ok=True)

    raw = send_chat(model_id, prompt_store.get("doc_reader"),
                    subtask.description, timeout=60)
    parsed = parse_json(raw)

    if parsed is None:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="doc_reader",
            result=f"LLM 未返回合法 JSON:\n{raw[:500]}", success=False,
        )

    ops = parsed if isinstance(parsed, list) else [parsed]
    results = []
    all_ok = True

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            results.append(f"[{i}] 无效操作项（非字典）")
            all_ok = False
            continue
        try:
            r = execute_doc_op(op)
            results.append(r)
            if r.startswith("[error]") or "错误：" in r or "失败:" in r:
                all_ok = False
        except ValueError as e:
            results.append(f"[路径安全] {e}")
            all_ok = False
        except Exception as e:
            results.append(f"[异常] {type(e).__name__}: {e}")
            all_ok = False

    return WorkerResult(
        task_id=subtask.task_id, worker_role="doc_reader",
        result="\n".join(results), success=all_ok,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Local File RW Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_local_file_rw(subtask: SubTask) -> WorkerResult:
    model_id = next_worker_model()
    os.makedirs(LOCAL_FILE_DIR, exist_ok=True)

    raw = send_chat(model_id, prompt_store.get("local_file_rw"),
                    subtask.description, timeout=60)
    parsed = parse_json(raw)

    if parsed is None:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="local_file_rw",
            result=f"LLM 未返回合法 JSON:\n{raw[:500]}", success=False,
        )

    ops = parsed if isinstance(parsed, list) else [parsed]
    results = []
    all_ok = True

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            results.append(f"[{i}] 无效操作项（非字典）")
            all_ok = False
            continue
        try:
            r = execute_local_file_op(op)
            results.append(r)
            if r.startswith("[error]") or "错误：" in r:
                all_ok = False
        except ValueError as e:
            results.append(f"[路径安全] {e}")
            all_ok = False
        except Exception as e:
            results.append(f"[异常] {type(e).__name__}: {e}")
            all_ok = False

    return WorkerResult(
        task_id=subtask.task_id, worker_role="local_file_rw",
        result="\n".join(results), success=all_ok,
    )


# ─────────────────────────────────────────────────────────────────────────────
# File Manager Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_file_manager(subtask: SubTask) -> WorkerResult:
    model_id = next_worker_model()
    os.makedirs(FILE_WORKSPACE, exist_ok=True)

    raw    = send_chat(model_id, prompt_store.get("file_manager"),
                       subtask.description, timeout=60)
    parsed = parse_json(raw)

    if parsed is None:
        return WorkerResult(
            task_id=subtask.task_id, worker_role="file_manager",
            result=f"LLM 未返回合法 JSON:\n{raw[:500]}", success=False,
        )

    ops     = parsed if isinstance(parsed, list) else [parsed]
    results = []
    all_ok  = True

    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            results.append(f"[{i}] 无效操作项（非字典）")
            all_ok = False
            continue
        try:
            r = execute_file_op(op)
            results.append(r)
            if r.startswith("[error]") or "错误：" in r:
                all_ok = False
        except ValueError as e:
            results.append(f"[路径安全] {e}")
            all_ok = False
        except Exception as e:
            results.append(f"[异常] {type(e).__name__}: {e}")
            all_ok = False

    return WorkerResult(
        task_id=subtask.task_id, worker_role="file_manager",
        result="\n".join(results), success=all_ok,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Coder ↔ Reviewer 自动修复闭环
# ─────────────────────────────────────────────────────────────────────────────

MAX_FIX_ROUNDS = 3


def _coder_reviewer_loop(subtask: SubTask) -> WorkerResult:
    original_description    = subtask.description
    current_code            = ""
    current_error           = ""
    current_review_feedback = ""  # 初始化防止 UnboundLocalError

    for round_num in range(MAX_FIX_ROUNDS + 1):
        is_fix = round_num > 0

        if is_fix:
            print(f"     [修复轮 {round_num}/{MAX_FIX_ROUNDS}] Coder 重写中...")
            fix_prompt = CODER_FIX_PROMPT.format(
                original_task=original_description,
                previous_code=current_code,
                error_info=current_error,
                review_feedback=current_review_feedback,
            )
            model_id   = next_worker_model()
            raw_output = send_chat(model_id, prompt_store.get("coder"),
                                   fix_prompt, timeout=120)
        else:
            print(f"     [Coder] 初次生成...")
            coder_result = _execute_coder(subtask)
            raw_output   = coder_result.result

        code = extract_code(raw_output)
        if not code:
            return WorkerResult(task_id=subtask.task_id, worker_role="coder",
                                result=raw_output)
        current_code = code

        exec_ok, exec_output = sandbox_exec(code)
        current_error = "" if exec_ok else exec_output

        if exec_ok:
            print(f"     [沙箱] 执行成功 ✓")
        else:
            print(f"     [沙箱] 执行失败: {exec_output[:100]}...")

        review_input = f"任务目标: {original_description}"
        if current_error:
            review_input += f"\n\n执行错误:\n{current_error}"

        review_result = _execute_reviewer(
            SubTask(
                task_id=f"{subtask.task_id}_rv{round_num}",
                description=review_input,
                assigned_worker="reviewer",
            ),
            code_to_review=current_code,
        )

        if review_result.review_passed:
            print(f"     [Reviewer] 通过 ✓ (第 {round_num} 轮)")
            suffix = f" (修复后通过，共 {round_num} 轮)" if is_fix else " (一次通过)"

            # ── 自动保存代码到 files/ ──
            save_msg = ""
            try:
                os.makedirs(LOCAL_FILE_DIR, exist_ok=True)
                code_filename = f"{subtask.task_id}_code.py"
                code_path = os.path.join(LOCAL_FILE_DIR, code_filename)
                with open(code_path, "w", encoding="utf-8") as f:
                    f.write(current_code)
                save_msg = f"\n已自动保存到 files/{code_filename}"
                print(f"     [自动保存] files/{code_filename}")
            except Exception as e:
                save_msg = f"\n自动保存失败: {e}"

            return WorkerResult(
                task_id=subtask.task_id, worker_role="coder",
                result=(
                    f"```python\n{current_code}\n```\n\n"
                    f"执行输出: {exec_output if exec_ok else '(无输出)'}\n"
                    f"审查结果: 通过{suffix}{save_msg}"
                ),
            )
        else:
            current_review_feedback = review_result.review_feedback or ""
            print(f"     [Reviewer] 未通过: {current_review_feedback[:80]}...")
            if round_num >= MAX_FIX_ROUNDS:
                print(f"     [Coder] 达到最大修复轮数 ({MAX_FIX_ROUNDS})")
                return WorkerResult(
                    task_id=subtask.task_id, worker_role="coder",
                    result=(
                        f"```python\n{current_code}\n```\n\n"
                        f"历经 {MAX_FIX_ROUNDS} 轮修复仍未通过审查\n"
                        f"最后错误: {current_error[:300]}\n"
                        f"审查意见: {current_review_feedback[:300]}"
                    ),
                    success=False,
                    error_output=current_error,
                    review_feedback=current_review_feedback,
                )

    return WorkerResult(task_id=subtask.task_id, worker_role="coder",
                        result=current_code, success=False)


# ─────────────────────────────────────────────────────────────────────────────
# Sub-Orchestrator Worker
# ─────────────────────────────────────────────────────────────────────────────

def _execute_sub_orchestrator(subtask: SubTask) -> WorkerResult:
    model_id = next_sub_orch_model()
    raw      = send_chat(model_id, prompt_store.get("sub_orchestrator"),
                         subtask.description, timeout=60)
    parsed   = parse_json(raw)

    if isinstance(parsed, dict) and "micro_tasks" in parsed:
        micro_list = parsed["micro_tasks"]
        if isinstance(micro_list, list) and len(micro_list) > 0:
            return WorkerResult(task_id=subtask.task_id,
                                worker_role="sub_orchestrator",
                                result=raw, success=True)

    return WorkerResult(task_id=subtask.task_id,
                        worker_role="sub_orchestrator",
                        result=raw, success=False)


# ─────────────────────────────────────────────────────────────────────────────
# 统一任务分发入口
# ─────────────────────────────────────────────────────────────────────────────

def execute_subtask(subtask: SubTask) -> WorkerResult:
    desc_preview = subtask.description[:50].replace("\n", " ")
    print(f"  [Worker:{subtask.assigned_worker}] {subtask.task_id}: {desc_preview}...")

    t0 = time.perf_counter()
    try:
        role = subtask.assigned_worker
        if   role == "searcher":        result = _execute_searcher(subtask)
        elif role == "coder":           result = _coder_reviewer_loop(subtask)
        elif role == "reviewer":        result = _execute_reviewer(subtask)
        elif role == "file_manager":    result = _execute_file_manager(subtask)
        elif role == "local_file_rw":   result = _execute_local_file_rw(subtask)
        elif role == "doc_reader":      result = _execute_doc_reader(subtask)
        elif role == "sub_orchestrator":result = _execute_sub_orchestrator(subtask)
        else:                           result = _execute_generic(subtask)
    except Exception as e:
        result = WorkerResult(
            task_id=subtask.task_id, worker_role=subtask.assigned_worker,
            result=f"执行异常: {e}", success=False,
        )

    result.latency = time.perf_counter() - t0
    status = "OK" if result.success else "FAIL"
    print(f"  [Worker:{subtask.assigned_worker}] [{status}] {subtask.task_id}"
          f" ({result.latency:.1f}s)")
    return result
