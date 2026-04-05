# Team — Autonomous Multi-Agent Orchestration System

A 9-agent collaboration framework built on a shared task board with claim-based scheduling. It automatically decomposes complex tasks, distributes them to specialized worker agents for parallel execution, and features dynamic prompt evolution, inter-worker messaging, and session persistence.

## Features

- **Shared Task Board Scheduling**: Workers proactively claim tasks (pull model) with multi-dependency DAG support
- **Specialized Workers**: Coder (code generation + sandbox execution), Reviewer (code review), Searcher (web search), Writer (document authoring), Analyst (data analysis), File Manager (file operations), Doc Reader (multi-format document I/O)
- **Coder-Reviewer Loop**: Code generation → sandbox execution → review feedback → auto-fix, up to 3 rounds
- **Dynamic Prompt Evolution**: Consecutive failures automatically trigger LLM-driven system prompt rewriting with version rollback support
- **Mailbox Communication**: Point-to-point messaging + broadcast between workers, with automatic dependency context injection
- **Session Persistence**: File / Redis / PostgreSQL backends for checkpoint-and-resume
- **Stale Task Recovery**: Automatic detection and reclamation of stuck tasks
- **Task Deduplication**: Hash-based dedup to prevent redundant submissions
- **Memory Compression**: Automatic history summarization when context budget is exceeded

## Architecture

```
orchestrator.py          ← Orchestrator: task planning + main loop (up to 50 iterations)
    ├─ engine.py         ← Async engine: 6-worker thread pool + stale scanner
    │   ├─ board.py      ← Shared task board: thread-safe claim/complete/dependency checks
    │   └─ mailbox.py    ← Mailbox: point-to-point & broadcast messaging
    ├─ workers.py        ← Worker dispatcher: routes by role to execution logic
    │   ├─ llm.py        ← LLM communication: API calls + JSON/code parsing
    │   ├─ sandbox.py    ← Sandbox: subprocess-based safe code execution
    │   ├─ search.py     ← Search: DuckDuckGo + caching + parallel queries
    │   ├─ file_ops.py   ← File operations: read/write + multi-format document support
    │   └─ prompt_evolution.py  ← Prompt evolution: failure tracking + LLM rewriting
    ├─ backends.py       ← Persistence: File / Redis / PostgreSQL
    ├─ hooks.py          ← Quality gates: task creation/completion hooks
    ├─ models.py         ← Data models: SubTask / WorkerResult / Plan
    ├─ config.py         ← Configuration: model routing + API endpoint
    ├─ prompts.py        ← Prompts: system instructions + role definitions
    └─ utils.py          ← Utilities: dedup / dependency injection / memory compression
```

## Quick Start

### Prerequisites

- Python 3.10+
- [LM Studio](https://lmstudio.ai/) running locally
- `requests`, `pydantic`

### Installation

```bash
pip install requests pydantic
```

Optional dependencies (install as needed):

```bash
pip install duckduckgo-search   # Web search
pip install pdfplumber           # PDF reading
pip install python-docx          # Word document generation
pip install openpyxl             # Excel file generation
pip install redis                # Redis backend
pip install psycopg2-binary      # PostgreSQL backend
```

### Configuration

Edit `config.py` to set the LM Studio endpoint and model assignments:

```python
BASE_API_URL = "http://192.168.2.1:1234/api/v1/chat"

MODEL_INSTANCES = [
    "qwen3.5-27b-reasoning-distilled",  # Orchestrator (large model for planning)
    "qwen/qwen3-4b-2507",               # Sub-orchestrator
    "qwen/qwen3-4b-2507:2",             # Workers ...
]
```

### Usage

```bash
# Default test task
python -m team

# Custom task
python -m team "Write a snake game for me"

# List saved sessions
python -m team --list

# Resume an interrupted session
python -m team --resume <session_id>

# View prompt evolution history for a role
python -m team --prompt-history coder

# Rollback a prompt to the previous version
python -m team --rollback coder
```

### Programmatic API

```python
from team import ask_team, list_sessions, resume_session

# Submit a task
answer = ask_team("Write a snake game", max_iterations=50)

# List all sessions
sessions = list_sessions()

# Resume a session
answer = resume_session(session_id)
```

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_iterations` | 50 | Max orchestrator iterations |
| `num_workers` | 6 | Concurrent worker threads |
| `_MAX_CONTEXT_CHARS` | 8000 | Orchestrator context budget |
| `_RESULT_EXCERPT_LEN` | 600 | Max result excerpt characters |
| `_MEMORY_COMPRESS_THRESHOLD` | 8 | Entries before memory compression triggers |
| `EVOLVE_FAIL_THRESHOLD` | 2 | Consecutive failures before prompt evolution |
| `MAX_FIX_ROUNDS` | 3 | Max coder-reviewer fix rounds |
| `stale_timeout` | 300s | Stale task reclamation timeout |

## Worker Roles

| Role | Function | Special Capability |
|------|----------|--------------------|
| `coder` | Code generation & execution | Sandbox execution + review-fix loop |
| `reviewer` | Code/content review | Structured JSON feedback |
| `searcher` | Web information retrieval | DuckDuckGo parallel search + caching |
| `writer` | Document/content generation | Auto-saves to files/ |
| `analyst` | Data analysis | Structured analysis output |
| `file_manager` | File operations | workspace_files/ read/write |
| `doc_reader` | Multi-format document processing | PDF/DOCX/XLSX/CSV |
| `sub_orchestrator` | Subtask decomposition | Splits into 2-4 micro-tasks |

## Dependencies

**Required**:
- `requests` — HTTP client
- `pydantic` — Data model validation

**Optional**:
- `duckduckgo-search` — Web search
- `pdfplumber` / `python-docx` / `openpyxl` — Document processing
- `redis` / `psycopg2` — Persistence backends

## License

MIT
