"""
配置 — API / 模型 / HTTP Session / 正则 / 线程池 / 模型轮询
"""

import re
import threading

import requests
from concurrent.futures import ThreadPoolExecutor

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

BASE_API_URL = "http://192.168.2.1:1234/api/v1/chat"

MODEL_INSTANCES = [
    "qwen3.5-27b-claude-4.6-opus-reasoning-distilled",
    "qwen/qwen3-4b-2507",
    "qwen/qwen3-4b-2507:2",
    "qwen/qwen3-4b-2507:3",
    "qwen/qwen3-4b-2507:4",
    "qwen/qwen3-4b-2507:5",
    "qwen/qwen3-4b-2507:6",
    "qwen/qwen3-4b-2507:7",
]

ORCHESTRATOR_MODEL  = MODEL_INSTANCES[0]
_ALL_WORKER_MODELS  = [m for i, m in enumerate(MODEL_INSTANCES) if i != 0]
SUB_ORCHESTRATOR_MODELS = _ALL_WORKER_MODELS[:1]
WORKER_MODELS           = _ALL_WORKER_MODELS[1:]

# ─────────────────────────────────────────────────────────────────────────────
# HTTP Session（连接池复用）
# ─────────────────────────────────────────────────────────────────────────────

_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=8,
    pool_maxsize=8,
    max_retries=requests.adapters.Retry(
        total=2, backoff_factor=0.5, status_forcelist=[502, 503, 504],
    ),
)
_session.mount("http://", _adapter)
_session.mount("https://", _adapter)

# ─────────────────────────────────────────────────────────────────────────────
# 正则 & 线程池
# ─────────────────────────────────────────────────────────────────────────────

RE_THINK       = re.compile(r"<think>.*?</think>", re.DOTALL)
RE_CODE_OPEN   = re.compile(r"```[a-zA-Z]*\s*")
RE_CODE_CLOSE  = re.compile(r"```")
RE_PYTHON_BLOCK = re.compile(r"```python\s*\n(.*?)```", re.DOTALL)
RE_ANY_BLOCK    = re.compile(r"```\w*\s*\n(.*?)```", re.DOTALL)

pool = ThreadPoolExecutor(max_workers=6)

# ─────────────────────────────────────────────────────────────────────────────
# 模型轮询（round-robin）
# ─────────────────────────────────────────────────────────────────────────────

_worker_model_idx  = 0
_worker_model_lock = threading.Lock()

def next_worker_model() -> str:
    global _worker_model_idx
    with _worker_model_lock:
        model = WORKER_MODELS[_worker_model_idx % len(WORKER_MODELS)]
        _worker_model_idx += 1
        return model

_sub_orch_model_idx  = 0
_sub_orch_model_lock = threading.Lock()

def next_sub_orch_model() -> str:
    global _sub_orch_model_idx
    with _sub_orch_model_lock:
        model = SUB_ORCHESTRATOR_MODELS[_sub_orch_model_idx % len(SUB_ORCHESTRATOR_MODELS)]
        _sub_orch_model_idx += 1
        return model
