"""
Agent Team CLI 入口 — python -m team
"""

import sys
import time

from .config import (
    BASE_API_URL, ORCHESTRATOR_MODEL,
    SUB_ORCHESTRATOR_MODELS, WORKER_MODELS,
)
from .workers import MAX_FIX_ROUNDS
from .prompt_evolution import EVOLVE_FAIL_THRESHOLD
from .backends import get_default_backend
from . import (
    ask_team, list_sessions, resume_session,
    get_prompt_history, rollback_prompt,
)


def main():
    backend = get_default_backend()

    print(f"Agent Team (SharedTaskBoard + ClaimScheduling + Mailbox + Hooks)")
    print(f"API: {BASE_API_URL}")
    print(f"Brain: {ORCHESTRATOR_MODEL}")
    print(f"Sub-Orchestrators: {len(SUB_ORCHESTRATOR_MODELS)} x 4B")
    print(f"Workers: {len(WORKER_MODELS)} x 4B")
    print(f"Backend: {backend.__class__.__name__}")
    print(f"Coder self-fix rounds: {MAX_FIX_ROUNDS}  |  "
          f"Prompt evolve threshold: {EVOLVE_FAIL_THRESHOLD} fails")
    print(f"{'=' * 70}")

    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        sessions = list_sessions()
        print(f"Saved sessions ({len(sessions)}):")
        for s in sessions:
            print(f"  - {s}")

    elif len(sys.argv) > 2 and sys.argv[1] == "--resume":
        sid    = sys.argv[2]
        print(f"Resuming session: {sid}")
        answer = resume_session(sid)
        print(f"\nFinal:\n{answer}")

    elif len(sys.argv) > 2 and sys.argv[1] == "--prompt-history":
        role = sys.argv[2]
        hist = get_prompt_history(role)
        print(f"Prompt history for '{role}' ({len(hist)} versions):")
        for rec in hist:
            print(f"  v{rec['version']} @ iter {rec['evolved_at_iteration']}: {rec['reason']}")
            print(f"    {rec['prompt'][:120]}...")

    elif len(sys.argv) > 2 and sys.argv[1] == "--rollback":
        role = sys.argv[2]
        rollback_prompt(role)

    else:
        test_query = (
            " ".join(sys.argv[1:]) if len(sys.argv) > 1
            else "帮我调研并写一个用 Python 实现的简单贪吃蛇游戏，要求调用agent skills#下载成本地文件#，所有程序内语言使用英语"
        )
        print(f"\nUser: {test_query}")
        t0     = time.perf_counter()
        answer = ask_team(test_query)
        elapsed = time.perf_counter() - t0

        print(f"\n{'=' * 70}")
        print(f"  Final Answer ({elapsed:.1f}s):")
        print(f"{'=' * 70}")
        print(answer)


if __name__ == "__main__":
    main()
