"""Oagnet runtime configuration loaded from the project environment."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _positive_int(name: str, default: int) -> int:
    raw = _env(name, default=str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be positive")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = _env(name, default=str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must not be negative")
    return value


OAGNET_HOST = _env("OAGNET_HOST", default="0.0.0.0")
OAGNET_PORT = _positive_int("OAGNET_PORT", 8021)

LLM_MODEL = _env("OAGNET_LLM_MODEL", "LLM_MODEL_ID", default="qwen3.6-plus")
API_KEY = _env("OAGNET_API_KEY", "DASHSCOPE_API_KEY", "LLM_API_KEY", "API_KEY")
BASE_URL = _env(
    "OAGNET_BASE_URL",
    "DASHSCOPE_BASE_URL",
    "LLM_BASE_URL",
    "BASE_URL",
    default="https://dashscope.aliyuncs.com/compatible-mode/v1",
)
LLM_TIMEOUT_SECONDS = _positive_int("OAGNET_LLM_TIMEOUT_SECONDS", 45)
LLM_MAX_RETRIES = _nonnegative_int("OAGNET_LLM_MAX_RETRIES", 0)
LLM_MAX_CONCURRENCY = _positive_int("OAGNET_LLM_MAX_CONCURRENCY", 1)
# One report commonly fans out into three ASL requests. This is the queue ceiling;
# the API also shortens it when necessary to preserve its 85-second total budget.
LLM_QUEUE_TIMEOUT_SECONDS = _positive_int("OAGNET_LLM_QUEUE_TIMEOUT_SECONDS", 90)

EMBEDDING_MODEL = _env(
    "OAGNET_EMBEDDING_MODEL", "EMBEDDING_MODEL", default="text-embedding-v4"
)
EMBEDDING_MODEL_API_KEY = _env(
    "OAGNET_EMBEDDING_API_KEY", "DASHSCOPE_API_KEY", "API_KEY"
)
EMBEDDING_BATCH_SIZE = _positive_int("EMBEDDING_BATCH_SIZE", 6)
EMBEDDING_TIMEOUT_SECONDS = _positive_int("OAGNET_EMBEDDING_TIMEOUT_SECONDS", 60)
EMBEDDING_MAX_RETRIES = _nonnegative_int("OAGNET_EMBEDDING_MAX_RETRIES", 2)
EMBEDDING_MAX_CONCURRENCY = _positive_int("OAGNET_EMBEDDING_MAX_CONCURRENCY", 4)

MYSQL_HOST = _env("OAGNET_MYSQL_HOST", "MYSQL_HOST")
MYSQL_PORT = _positive_int(
    "OAGNET_MYSQL_PORT", int(_env("MYSQL_PORT", default="3306"))
)
MYSQL_USER = _env("OAGNET_MYSQL_USER", "MYSQL_USER")
MYSQL_PASSWORD = _env("OAGNET_MYSQL_PASSWORD", "MYSQL_PASSWORD")
MYSQL_DATABASE = _env("OAGNET_MYSQL_DATABASE", "MYSQL_DATABASE")
MYSQL_CHARSET = _env("OAGNET_MYSQL_CHARSET", default="utf8mb4")
MYSQL_CONNECT_TIMEOUT = _positive_int("OAGNET_MYSQL_CONNECT_TIMEOUT", 5)
MYSQL_READ_TIMEOUT = _positive_int("OAGNET_MYSQL_READ_TIMEOUT", 30)

REDIS_HOST = _env("OAGNET_REDIS_HOST", "REDIS_HOST")
REDIS_PORT = _positive_int("OAGNET_REDIS_PORT", int(_env("REDIS_PORT", default="6379")))
REDIS_DB = int(_env("OAGNET_REDIS_DB", "REDIS_DB", default="0").split()[0])
REDIS_PASSWORD = _env("OAGNET_REDIS_PASSWORD", "REDIS_PASSWORD")
REDIS_SOCKET_TIMEOUT = _positive_int("OAGNET_REDIS_SOCKET_TIMEOUT", 3)
DAILY_JOB_TTL_SECONDS = _positive_int("OAGNET_DAILY_JOB_TTL_SECONDS", 604800)
DAILY_JOB_LEASE_SECONDS = _positive_int("OAGNET_DAILY_JOB_LEASE_SECONDS", 90)
ENTITY_SYNC_WAIT_SECONDS = _positive_int("OAGNET_ENTITY_SYNC_WAIT_SECONDS", 300)

# Vector storage.  Milvus is the production default; Chroma remains available
# only as an explicit rollback backend while existing deployments migrate.
VECTOR_STORE_BACKEND = _env("VECTOR_STORE_BACKEND", default="milvus").lower()
MILVUS_HOST = _env("MILVUS_HOST", "MILVUS_DEFAULT_HOST", default="127.0.0.1")
MILVUS_PORT = _positive_int(
    "MILVUS_PORT", int(_env("MILVUS_DEFAULT_PORT", default="19530"))
)
MILVUS_USER = _env("MILVUS_USER", "MILVUS_DEFAULT_USER")
MILVUS_PASSWORD = _env("MILVUS_PASSWORD", "MILVUS_DEFAULT_PASSWORD")
MILVUS_DATABASE = _env(
    "MILVUS_DATABASE", "MILVUS_DEFAULT_DATABASE", default="default"
)
MILVUS_COLLECTION_PREFIX = _env(
    "MILVUS_COLLECTION", default="oagnet_vectors"
)
MILVUS_SEMANTIC_COLLECTION = _env(
    "MILVUS_SEMANTIC_COLLECTION",
    default=f"{MILVUS_COLLECTION_PREFIX}_semantic_catalog_v1",
)
MILVUS_ENTITY_VALUE_COLLECTION = _env(
    "MILVUS_ENTITY_VALUE_COLLECTION",
    default=f"{MILVUS_COLLECTION_PREFIX}_entity_values_v1",
)
MILVUS_PHYSICAL_COLLECTION = _env(
    "MILVUS_PHYSICAL_COLLECTION",
    default=f"{MILVUS_COLLECTION_PREFIX}_physical_catalog_v1",
)
MILVUS_DAILY_COLLECTION = _env(
    "MILVUS_DAILY_COLLECTION",
    default=f"{MILVUS_COLLECTION_PREFIX}_daily_business_v1",
)
EMBEDDING_DIM = _positive_int("EMBEDDING_DIM", 1024)
MILVUS_TIMEOUT_SECONDS = _positive_int("MILVUS_TIMEOUT_SECONDS", 30)


def require_runtime_secret(value: str, name: str) -> str:
    """Fail when a protected dependency is used, without breaking imports."""
    if not value:
        raise RuntimeError(f"{name} is not configured")
    return value
