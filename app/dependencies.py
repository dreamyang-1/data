from dataclasses import dataclass
import logging

from minio_followup_store import (
    HybridMinioFollowupStore,
    MinioFollowupStore,
    ParquetMinioFollowupStore,
)

from app.adapters import AdapterBundle, build_http_adapters, build_mock_adapters
from app.config import Settings
from app.graph import build_workflow
from app.intent import HybridIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.services.dataset_followup import DatasetLifecycleCleaner
from app.services.file_ingestion import SpreadsheetFileImporter
from app.services.report_export import DatasetReportExporter
from app.services.question_rewriter import HttpEntityAttributeSearcher, QuestionRewriter
from app.services.entity_extraction import build_gliner_extractor_from_environment
from app.planning import MultiQuestionPlanner
from app.services.knowledge_retrieval import RedisKnowledgeSearchCache
from app.stores import InMemorySessionStore, RedisSessionStore, SessionStore
from app.stores.long_memory import (
    InMemoryLongTermMemoryStore,
    LongTermMemoryStore,
    MySQLLongTermMemoryStore,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Container:
    settings: Settings
    adapters: AdapterBundle
    sessions: SessionStore
    memories: LongTermMemoryStore | None
    dataset_store: HybridMinioFollowupStore | None
    dataset_cleaner: DatasetLifecycleCleaner | None
    file_importer: SpreadsheetFileImporter | None
    report_exporter: DatasetReportExporter | None
    workflow: object


def build_container(settings: Settings) -> Container:
    if settings.env == "production" and settings.adapter_mode == "mock":
        raise RuntimeError("production environment refuses mock adapters")
    store_mode = "memory" if settings.env == "test" else settings.session_store_mode
    if settings.env == "production" and store_mode != "redis":
        raise RuntimeError("production environment requires Redis short-term memory")
    if store_mode == "redis":
        redis_url = settings.effective_redis_url()
        if not redis_url:
            raise RuntimeError("Redis session store requires DATA_AGENT_REDIS_URL")
        sessions: SessionStore = RedisSessionStore.from_url(
            redis_url,
            ttl_seconds=settings.session_ttl_seconds,
            response_ttl_seconds=settings.response_cache_ttl_seconds,
            prefix=settings.session_key_prefix,
        )
    else:
        sessions = InMemorySessionStore(
            settings.session_ttl_seconds, settings.response_cache_ttl_seconds
        )
    if settings.env == "test":
        memories: LongTermMemoryStore | None = InMemoryLongTermMemoryStore()
    elif settings.long_term_memory_mode == "disabled":
        memories = None
    else:
        if not all(
            (
                settings.mysql_host,
                settings.mysql_user,
                settings.mysql_password,
                settings.mysql_database,
            )
        ):
            raise RuntimeError(
                "MySQL long-term memory requires host, user, password and database"
            )
        memories = MySQLLongTermMemoryStore.from_parameters(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            database=settings.mysql_database,
            connect_timeout=settings.mysql_connect_timeout_seconds,
            read_timeout=settings.mysql_read_timeout_seconds,
            write_timeout=settings.mysql_write_timeout_seconds,
        )
    knowledge_cache = None
    if (
        settings.adapter_mode == "http"
        and settings.knowledge_cache_enabled
        and isinstance(sessions, RedisSessionStore)
    ):
        knowledge_cache = RedisKnowledgeSearchCache(
            sessions.redis,
            ttl_seconds=settings.knowledge_cache_ttl_seconds,
            prefix=settings.session_key_prefix,
            lock_seconds=settings.knowledge_cache_lock_seconds,
            wait_seconds=settings.knowledge_cache_wait_seconds,
        )
    adapters = (
        build_mock_adapters()
        if settings.adapter_mode == "mock"
        else build_http_adapters(settings, knowledge_cache=knowledge_cache)
    )
    dataset_store: HybridMinioFollowupStore | None = None
    dataset_cleaner: DatasetLifecycleCleaner | None = None
    file_importer: SpreadsheetFileImporter | None = None
    report_exporter: DatasetReportExporter | None = None
    if settings.minio_dataset_enabled and settings.env != "test":
        if not all(
            (
                settings.minio_endpoint,
                settings.minio_access_key,
                settings.minio_secret_key,
                settings.minio_bucket,
            )
        ):
            raise RuntimeError(
                "MinIO dataset storage requires endpoint, access key, secret key and bucket"
            )
        from minio import Minio

        minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key.get_secret_value(),
            secure=settings.minio_secure,
        )
        json_dataset_store = MinioFollowupStore(
            minio_client,
            bucket=settings.minio_bucket,
            max_object_bytes=settings.dataset_max_object_bytes,
        )
        parquet_dataset_store = ParquetMinioFollowupStore(
            minio_client,
            bucket=settings.minio_bucket,
            part_rows=settings.dataset_parquet_part_rows,
            max_dataset_bytes=settings.dataset_medium_max_bytes,
        )
        dataset_store = HybridMinioFollowupStore(
            json_dataset_store,
            parquet_dataset_store,
            small_max_rows=settings.dataset_small_max_rows,
            small_max_bytes=settings.dataset_small_max_bytes,
            medium_max_rows=settings.dataset_medium_max_rows,
            medium_max_bytes=settings.dataset_medium_max_bytes,
        )
        dataset_store.check_ready()
        dataset_cleaner = DatasetLifecycleCleaner(
            dataset_store,
            sessions,
            interval_seconds=settings.dataset_cleanup_interval_seconds,
            batch_size=settings.dataset_cleanup_batch_size,
            report_client=minio_client,
            report_bucket=settings.minio_bucket,
        )
        file_importer = SpreadsheetFileImporter(
            minio_client,
            dataset_store,
            sessions,
            bucket=settings.minio_bucket,
            max_bytes=settings.uploaded_file_max_bytes,
            max_rows=settings.uploaded_file_max_rows,
            max_sheets=settings.uploaded_file_max_sheets,
            max_columns=settings.uploaded_file_max_columns,
            max_cells=settings.uploaded_file_max_cells,
            max_cell_chars=settings.uploaded_file_max_cell_chars,
            ttl_seconds=settings.dataset_ttl_seconds,
            recent_limit=settings.dataset_recent_limit,
        )
        report_exporter = DatasetReportExporter(
            minio_client, dataset_store, bucket=settings.minio_bucket,
            object_ttl_seconds=settings.report_object_ttl_seconds,
        )
    entity_candidate_mode, entity_candidate_extractor = (
        build_gliner_extractor_from_environment()
    )
    logger.log(
        logging.WARNING if entity_candidate_mode != "off" else logging.INFO,
        "entity candidate extractor configured: mode=%s backend=%s",
        entity_candidate_mode,
        type(entity_candidate_extractor).__name__ if entity_candidate_extractor else "none",
    )
    orchestrator = DataAnalysisOrchestrator(
        settings=settings,
        classifier=HybridIntentClassifier(settings),
        adapters=adapters,
        sessions=sessions,
        memories=memories,
        dataset_store=dataset_store,
        question_rewriter=(
            QuestionRewriter(
                HttpEntityAttributeSearcher(
                    base_url=settings.asl_generator_base_url,
                    path=settings.entity_attribute_search_path,
                    timeout_seconds=settings.question_rewrite_timeout_seconds,
                    top_k=settings.question_rewrite_top_k,
                    score_threshold=settings.question_rewrite_search_threshold,
                ),
                candidate_extractor=entity_candidate_extractor,
                candidate_mode=entity_candidate_mode,
                auto_replace_threshold=settings.question_rewrite_auto_threshold,
                candidate_gap=settings.question_rewrite_candidate_gap,
                typo_similarity_threshold=settings.question_rewrite_typo_threshold,
            )
            if settings.question_rewrite_enabled and settings.adapter_mode == "http"
            else QuestionRewriter(
                None,
                candidate_extractor=entity_candidate_extractor,
                candidate_mode=entity_candidate_mode,
            )
        ),
        task_planner=MultiQuestionPlanner(settings),
        report_exporter=report_exporter,
        file_importer=file_importer,
    )
    return Container(
        settings=settings,
        adapters=adapters,
        sessions=sessions,
        memories=memories,
        dataset_store=dataset_store,
        dataset_cleaner=dataset_cleaner,
        file_importer=file_importer,
        report_exporter=report_exporter,
        workflow=build_workflow(orchestrator),
    )
