from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ENV = PROJECT_ROOT.parent / ".env"
SHARED_PROJECT_ENV = PROJECT_ROOT.parent / "New_Agent" / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DATA_AGENT_",
        # Later files override earlier files: platform defaults -> shared agent -> this service.
        env_file=(PLATFORM_ENV, SHARED_PROJECT_ENV, PROJECT_ROOT / ".env"),
        extra="ignore",
        populate_by_name=True,
    )

    env: Literal["development", "test", "production"] = "development"
    adapter_mode: Literal["mock", "http"] = "mock"
    redis_url: str | None = None
    session_store_mode: Literal["memory", "redis"] = "redis"
    # v2 separates fingerprint-aware idempotency records from pre-upgrade Redis
    # values that cannot prove which request payload produced a cached response.
    session_key_prefix: str = "youo:data-analysis:v2"
    redis_host: str | None = Field(default=None, validation_alias=AliasChoices("DATA_AGENT_REDIS_HOST", "REDIS_HOST"))
    redis_port: int = Field(default=6379, validation_alias=AliasChoices("DATA_AGENT_REDIS_PORT", "REDIS_PORT"))
    redis_db: int = Field(default=3, validation_alias=AliasChoices("DATA_AGENT_REDIS_DB", "REDIS_DB"))
    redis_password: SecretStr | None = Field(default=None, validation_alias=AliasChoices("DATA_AGENT_REDIS_PASSWORD", "REDIS_PASSWORD"))
    session_ttl_seconds: int = Field(default=7200, ge=300, le=86400)
    # Keep the idempotency record at least as long as the pending conversation.
    # Session stores enforce max(response_cache_ttl, session_ttl) as a second guard.
    response_cache_ttl_seconds: int = Field(default=7200, ge=300, le=86400)
    allow_missing_trusted_identity_headers: bool = False
    require_trusted_application_header: bool = False
    max_clarification_rounds: int = Field(default=3, ge=1, le=10)
    # End-to-end budget. Oagnet performs one constrained LLM generation and can
    # legitimately take longer than an ordinary HTTP dependency. Keep its own
    # budget below this value so orchestration still has time to terminate and
    # persist an idempotent response.
    request_timeout_seconds: float = Field(default=120, gt=0, le=300)
    business_question_collection_enabled: bool = True
    business_question_document_path: Path = PROJECT_ROOT / "实际业务问题.md"
    dynamic_skills_enabled: bool = True
    autonomous_tool_selection_enabled: bool = True
    autonomous_tool_selection_max_tools: int = Field(default=3, ge=1, le=5)
    bocha_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_BOCHA_KEY", "BOCHA_KEY"),
    )
    bocha_search_url: str = Field(
        default="https://api.bochaai.com/v1/web-search",
        validation_alias=AliasChoices(
            "DATA_AGENT_BOCHA_SEARCH_URL",
            "BOCHA_SEARCH_URL",
        ),
    )
    bocha_search_timeout_seconds: int = Field(default=10, ge=1, le=60)
    skills_root: Path = PROJECT_ROOT.parent / "NL_Agent" / "skills"
    skill_max_bytes: int = Field(default=128 * 1024, ge=1024, le=1024 * 1024)
    # Metric definition and lineage are exposed by the same semantic SQL
    # service that translates/executes ASL; keep one platform dependency rather
    # than pointing metadata intents at a non-existent standalone service.
    semantic_base_url: str = "http://192.168.1.49:48000"
    policy_base_url: str = "http://localhost:8102"
    policy_authorize_path: str = "/v1/authorize"
    analysis_base_url: str = "http://localhost:8104"
    analysis_run_path: str = "/v1/analysis:run"
    # Oagnet owns natural-language -> ASL. SQL translation and execution are
    # deliberately separate contracts so generated SQL can be validated before
    # it is sent to the data source.
    asl_generator_base_url: str = "http://192.168.1.27:8021"
    asl_generator_path: str = "/agent/query"
    asl_generation_timeout_seconds: float = Field(default=90, gt=0, le=180)
    asl_plan_cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    asl_plan_cache_max_items: int = Field(default=256, ge=0, le=2048)
    question_rewrite_enabled: bool = True
    entity_attribute_search_path: str = "/vector/entity-attributes/search"
    question_rewrite_timeout_seconds: float = Field(default=3.0, gt=0, le=15)
    question_rewrite_top_k: int = Field(default=5, ge=1, le=20)
    question_rewrite_search_threshold: float = Field(default=0.70, ge=0, le=1)
    question_rewrite_auto_threshold: float = Field(default=0.88, ge=0, le=1)
    question_rewrite_candidate_gap: float = Field(default=0.05, ge=0, le=0.5)
    question_rewrite_typo_threshold: float = Field(default=0.84, ge=0.5, le=1)
    sql_translator_base_url: str = "http://192.168.1.49:48000"
    sql_translate_path: str = "/api/translate"
    sql_execute_path: str = "/api/execute"
    knowledge_base_url: str = "http://127.0.0.1:7873"
    knowledge_base_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DATA_AGENT_KNOWLEDGE_BASE_API_KEY",
            "KB_API_KEY",
        ),
    )
    knowledge_base_search_path: str = "/knowledge_base/search_docs"
    knowledge_base_timeout_seconds: float = Field(default=30, gt=0, le=300)
    platform_api_key: SecretStr | None = None
    http_max_retries: int = Field(default=2, ge=0, le=5)
    http_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=5)
    semantic_resolve_path: str = "/v1/semantic/metrics/resolve"
    semantic_definition_path: str = "/v1/semantic/metrics/{metric_id}/versions/{version}"
    semantic_lineage_path: str = "/v1/semantic/metrics/{metric_id}/lineage"
    knowledge_base_names: list[str] = Field(default_factory=list)
    knowledge_retrieval_method: Literal["semantic", "fulltext", "hybrid"] = "hybrid"
    knowledge_top_k: int = Field(default=5, ge=1, le=20)
    knowledge_recall_top_k: int = Field(default=20, ge=5, le=50)
    analysis_knowledge_top_k: int = Field(default=5, ge=1, le=10)
    analysis_knowledge_excerpt_chars: int = Field(default=500, ge=100, le=2000)
    knowledge_score_threshold: float = Field(default=1.0, ge=0, le=2)
    knowledge_score_type: Literal["distance", "similarity"] = "distance"
    knowledge_min_normalized_relevance: float = Field(default=0.50, ge=0, le=1)
    knowledge_max_per_source: int = Field(default=2, ge=1, le=5)
    knowledge_cache_enabled: bool = True
    knowledge_cache_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    knowledge_index_version: str = Field(default="unversioned", min_length=1, max_length=128)
    knowledge_cache_lock_seconds: int = Field(default=10, ge=2, le=60)
    knowledge_cache_wait_seconds: float = Field(default=3.0, ge=0, le=10)
    data_query_max_rows: int = Field(default=1000, ge=1, le=10000)
    intent_model_enabled: bool = True
    intent_model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    intent_model_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_INTENT_MODEL_API_KEY", "API_KEY"),
    )
    intent_model_name: str = "qwen3.6-plus"
    intent_model_response_format: Literal["json_schema", "json_object"] = "json_object"
    intent_model_timeout_seconds: float = Field(default=30, gt=0, le=60)
    intent_model_max_retries: int = Field(default=1, ge=0, le=2)
    intent_model_enable_thinking: bool = False
    intent_model_min_confidence: float = Field(default=0.80, ge=0.5, le=1)
    multi_question_enabled: bool = True
    multi_question_model_enabled: bool = True
    multi_question_max_tasks: int = Field(default=5, ge=2, le=5)
    multi_question_model_timeout_seconds: float = Field(default=15, gt=0, le=30)
    # Qwen only verbalizes facts already produced by the deterministic analysis
    # engine. Its output is evidence-validated and safely falls back to the
    # deterministic answer when unavailable or ungrounded.
    analysis_synthesis_enabled: bool = True
    analysis_synthesis_model_name: str = "qwen3.6-plus"
    analysis_synthesis_timeout_seconds: float = Field(default=8, gt=0, le=20)
    analysis_synthesis_max_retries: int = Field(default=0, ge=0, le=1)
    chat_model_enabled: bool = True
    chat_model_name: str = "qwen3.6-plus"
    chat_model_timeout_seconds: float = Field(default=8, gt=0, le=20)
    chat_model_max_retries: int = Field(default=0, ge=0, le=1)
    long_term_memory_mode: Literal["disabled", "mysql"] = "mysql"
    long_term_memory_max_items: int = Field(default=20, ge=1, le=100)
    mysql_host: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MYSQL_HOST", "MYSQL_HOST"),
    )
    mysql_port: int = Field(
        default=3306,
        validation_alias=AliasChoices("DATA_AGENT_MYSQL_PORT", "MYSQL_PORT"),
    )
    mysql_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MYSQL_USER", "MYSQL_USER"),
    )
    mysql_password: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MYSQL_PASSWORD", "MYSQL_PASSWORD"),
    )
    mysql_database: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MYSQL_DATABASE", "MYSQL_DATABASE"),
    )
    mysql_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)
    mysql_read_timeout_seconds: int = Field(default=10, ge=1, le=60)
    mysql_write_timeout_seconds: int = Field(default=10, ge=1, le=60)
    minio_dataset_enabled: bool = False
    minio_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DATA_AGENT_MINIO_ENDPOINT", "MINIO_ENDPOINT", "MINIO_HOST"
        ),
    )
    minio_access_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MINIO_ACCESS_KEY", "MINIO_ACCESS_KEY"),
    )
    minio_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("DATA_AGENT_MINIO_SECRET_KEY", "MINIO_SECRET_KEY"),
    )
    minio_bucket: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DATA_AGENT_MINIO_BUCKET", "AGENT_MINIO_BUCKET", "MINIO_BUCKET"
        ),
    )
    minio_secure: bool = False
    dataset_small_max_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    dataset_small_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    dataset_medium_max_rows: int = Field(default=1_000_000, ge=10_000)
    dataset_medium_max_bytes: int = Field(default=256 * 1024 * 1024, ge=1024 * 1024)
    dataset_parquet_part_rows: int = Field(default=50_000, ge=1_000, le=500_000)
    dataset_ttl_seconds: int = Field(default=7200, ge=60, le=604800)
    dataset_recent_limit: int = Field(default=10, ge=1, le=50)
    dataset_cleanup_interval_seconds: int = Field(default=300, ge=30, le=3600)
    dataset_cleanup_batch_size: int = Field(default=100, ge=1, le=1000)
    report_object_ttl_seconds: int = Field(default=86_400, ge=3_600, le=604_800)
    dataset_max_object_bytes: int = Field(
        default=256 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024
    )
    uploaded_file_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    uploaded_file_max_rows: int = Field(default=200_000, ge=1, le=1_000_000)
    uploaded_file_max_sheets: int = Field(default=50, ge=1, le=200)
    uploaded_file_max_columns: int = Field(default=500, ge=1, le=10_000)
    uploaded_file_max_cells: int = Field(default=5_000_000, ge=1_000, le=100_000_000)
    uploaded_file_max_cell_chars: int = Field(default=32_767, ge=128, le=1_000_000)

    def effective_redis_url(self) -> str | None:
        if self.redis_url:
            return self.redis_url
        if not self.redis_host:
            return None
        password = ""
        if self.redis_password:
            password = f":{quote(self.redis_password.get_secret_value(), safe='')}@"
        return f"redis://{password}{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
