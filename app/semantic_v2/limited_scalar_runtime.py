"""Startup wiring for the explicitly enabled V2 limited-scalar candidate.

The default application remains V1.  This module only builds the candidate
when ``runtime_mode=V2_LIMITED_SCALAR`` and every governed dependency pin is
present.  It never reads evaluation captures or constructs synthetic tasks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import unquote, urlparse

from redis.asyncio import Redis

from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity

from .authorized_contract import contract_digest
from .catalog_bridge import ScopedPlanSession
from .isolated_execution import TypedExecutionRequest, prepare_execution
from .persisted_scalar_api import (
    PersistedScalarApiHandler,
    PersistedScalarPlan,
    RedisScalarSessionStore,
)
from .pipeline import AuthorizedLogicalPlan
from .recognition import RawTurnPlanner
from .recognition_client import RecognitionModelClient
from .time_storage import TimeStorageContract


OAGNET_RUNTIME_FILES = (
    "catalog_generation.py", "catalog_publication.py", "catalog_registry.py",
    "catalog_release.py", "catalog_store.py", "catalog_value_candidates.py",
    "catalog_value_sources.py", "config.py", "mysql_tool.py", "vector_store.py",
)
SQL_RUNTIME_FILES = (
    "bound_sql.py", "pinned_catalog.py", "semantic_scope.py",
    "sql_translator_prod.py",
)


def source_bundle_digest(root: Path, names: tuple[str, ...]) -> str:
    root = Path(root).resolve()
    payload = []
    for name in names:
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise RuntimeError(f"LIMITED_SCALAR_RUNTIME_MODULE_MISSING:{name}")
        payload.append({"path": name, "sha256": sha256(path.read_bytes()).hexdigest()})
    return contract_digest(payload)


def validate_limited_scalar_settings(settings: Settings) -> dict[str, Any]:
    if settings.runtime_mode != "V2_LIMITED_SCALAR":
        raise RuntimeError("LIMITED_SCALAR_RUNTIME_NOT_SELECTED")
    if settings.session_store_mode != "redis" or not settings.effective_redis_url():
        raise RuntimeError("LIMITED_SCALAR_REDIS_REQUIRED")
    if not settings.intent_model_enabled or not settings.intent_model_api_key:
        raise RuntimeError("LIMITED_SCALAR_MODEL_REQUIRED")
    if settings.intent_model_enable_thinking is not False:
        raise RuntimeError("LIMITED_SCALAR_THINKING_MUST_BE_DISABLED")
    if settings.intent_model_max_retries != 0:
        raise RuntimeError("LIMITED_SCALAR_MODEL_RETRY_MUST_BE_ZERO")
    domains = settings.limited_scalar_business_domain_ids
    if (not domains or any(type(value) is not int or value <= 0 for value in domains)
            or len(domains) != len(set(domains))):
        raise RuntimeError("LIMITED_SCALAR_EXPLICIT_SCOPE_REQUIRED")
    required_text = {
        "store_namespace": settings.limited_scalar_store_namespace,
        "deployment_id": settings.limited_scalar_deployment_id,
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "catalog_target_identity_hash": settings.limited_scalar_catalog_target_identity_hash,
        "oagnet_source_digest": settings.limited_scalar_oagnet_source_digest,
        "sql_source_digest": settings.limited_scalar_sql_source_digest,
        "time_field_canonical_id": settings.limited_scalar_time_field_canonical_id,
        "time_field_mapping": settings.limited_scalar_time_field_mapping,
        "time_evidence_version": settings.limited_scalar_time_evidence_version,
    }
    missing = sorted(name for name, value in required_text.items() if not str(value).strip())
    if missing:
        raise RuntimeError("LIMITED_SCALAR_CONFIGURATION_MISSING:" + ",".join(missing))
    if settings.limited_scalar_idempotency_ttl_seconds < settings.limited_scalar_session_ttl_seconds:
        raise RuntimeError("LIMITED_SCALAR_IDEMPOTENCY_WINDOW_TOO_SHORT")
    oagnet_digest = source_bundle_digest(settings.limited_scalar_oagnet_root, OAGNET_RUNTIME_FILES)
    sql_digest = source_bundle_digest(settings.limited_scalar_sql_translator_root, SQL_RUNTIME_FILES)
    if oagnet_digest != settings.limited_scalar_oagnet_source_digest:
        raise RuntimeError("LIMITED_SCALAR_OAGNET_SOURCE_DRIFT")
    if sql_digest != settings.limited_scalar_sql_source_digest:
        raise RuntimeError("LIMITED_SCALAR_SQL_SOURCE_DRIFT")
    return {
        "scope": {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": list(domains),
        },
        "data_source_id": settings.limited_scalar_data_source_id,
        "catalog_version": settings.limited_scalar_catalog_version,
        "vector_index_version": settings.limited_scalar_vector_index_version,
        "oagnet_source_digest": oagnet_digest,
        "sql_source_digest": sql_digest,
    }


def _prepend_runtime_roots(settings: Settings) -> None:
    for root in (settings.limited_scalar_sql_translator_root,
                 settings.limited_scalar_oagnet_root):
        value = str(Path(root).resolve())
        if value not in sys.path:
            sys.path.insert(0, value)


def _redis_connection_config(settings: Settings) -> dict[str, Any]:
    parsed = urlparse(settings.effective_redis_url() or "")
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
        raise RuntimeError("LIMITED_SCALAR_REDIS_URL_INVALID")
    database = int((parsed.path or "/0").lstrip("/") or "0")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 6379,
        "db": database,
        "password": unquote(parsed.password) if parsed.password else None,
        "decode_responses": True,
        "socket_timeout": 35,
        "socket_connect_timeout": 10,
        "protocol": 2,
        "ssl": parsed.scheme == "rediss",
    }


@dataclass(frozen=True)
class LimitedScalarExternalDependencies:
    """Test seam limited to external systems; all orchestration stays native."""

    publication: Any
    model: Any
    sql_planner: Any
    transport: Any
    redis: Any


class LimitedScalarRuntimeHandler(PersistedScalarApiHandler):
    def __init__(self, *args, startup_receipt: dict, **kwargs):
        super().__init__(*args, **kwargs)
        self.startup_receipt = startup_receipt

    async def readiness(self) -> dict[str, bool]:
        try:
            redis_ready = bool(await self.store.redis.ping())
        except Exception:
            redis_ready = False
        return {
            "v2_limited_scalar_redis": redis_ready,
            "v2_limited_scalar_catalog_pin": bool(self.startup_receipt.get("catalog_pin_verified")),
            "v2_limited_scalar_runtime_sources": bool(self.startup_receipt.get("source_pins_verified")),
            "v2_limited_scalar_read_only_transport": bool(self.startup_receipt.get("transport_verified")),
        }


def _build_external_dependencies(settings: Settings) -> LimitedScalarExternalDependencies:
    _prepend_runtime_roots(settings)
    CatalogPublication = importlib.import_module("catalog_publication").CatalogPublication
    RedisCatalogReleaseRegistry = importlib.import_module("catalog_registry").RedisCatalogReleaseRegistry
    open_catalog_store = importlib.import_module("catalog_store").open_catalog_store
    translate_pinned_catalog = importlib.import_module("pinned_catalog").translate_pinned_catalog
    RequestScope = importlib.import_module("semantic_scope").RequestScope
    SQLTranslatorProd = importlib.import_module("sql_translator_prod").SQLTranslatorProd

    store = open_catalog_store(
        initialize=False,
        expected_target_identity_hash=settings.limited_scalar_catalog_target_identity_hash,
    )
    publication = CatalogPublication(
        store,
        RedisCatalogReleaseRegistry(store.catalog_target_identity),
    )
    redis_config = _redis_connection_config(settings)
    translator = SQLTranslatorProd(redis_config)
    source = translator.fetch_data_source(
        str(settings.limited_scalar_semantic_model_id),
        str(settings.limited_scalar_data_source_id),
    )
    if (not source
            or str(source.get("id")) != str(settings.limited_scalar_data_source_id)
            or str(source.get("semantic_model_id")) != str(settings.limited_scalar_semantic_model_id)):
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_SCOPE_MISMATCH")

    def sql_planner(pin, scope, asl, **policy):
        request_scope = RequestScope.from_request({
            "authorized_semantic_scope": scope.model_dump(mode="json")
        })
        return translate_pinned_catalog(pin, request_scope, asl, **policy)

    async def transport(request: TypedExecutionRequest):
        if (request.provenance != "LIVE_READ_ONLY"
                or request.data_source_id != str(settings.limited_scalar_data_source_id)
                or request.context.authorized_scope.semantic_model_id != settings.limited_scalar_semantic_model_id
                or list(request.context.authorized_scope.business_domain_ids)
                    != settings.limited_scalar_business_domain_ids):
            raise ValueError("EXECUTION_SCOPE_PIN_MISMATCH")
        result = await asyncio.to_thread(
            SQLTranslatorProd.execute_sql_on_data_source,
            ds_config=source,
            **request.executor_arguments(),
        )
        return {
            "request_fingerprint": request.fingerprint,
            "prepared_fingerprint": request.prepared_fingerprint,
            "context_fingerprint": request.context.fingerprint(),
            "data_source_id": request.data_source_id,
            "provenance": "LIVE_READ_ONLY",
            "submitted": result.get("business_query_submitted") is True,
            "result": result,
        }

    return LimitedScalarExternalDependencies(
        publication=publication,
        model=RecognitionModelClient(settings),
        sql_planner=sql_planner,
        transport=transport,
        redis=Redis.from_url(settings.effective_redis_url(), decode_responses=True,
                             socket_connect_timeout=10, socket_timeout=35),
    )


def build_limited_scalar_handler(
    settings: Settings,
    *,
    external: LimitedScalarExternalDependencies | None = None,
) -> LimitedScalarRuntimeHandler:
    receipt = validate_limited_scalar_settings(settings)
    dependencies = external or _build_external_dependencies(settings)
    expected_scope = receipt["scope"]

    # Pin and finish once at startup.  This checks metadata/index identity but
    # neither executes a business query nor writes a session key.
    pin = dependencies.publication.pin(
        expected_scope["semantic_model_id"], expected_scope["business_domain_ids"]
    )
    identity = pin.identity
    if (identity.get("catalog_version") != receipt["catalog_version"]
            or identity.get("vector_index_version") != receipt["vector_index_version"]
            or identity.get("target_identity_hash")
                != settings.limited_scalar_catalog_target_identity_hash):
        raise RuntimeError("LIMITED_SCALAR_CATALOG_PIN_MISMATCH")
    pin.finish()

    engine = RawTurnPlanner(dependencies.model, dependencies.publication,
                            clock=lambda: datetime.now(timezone.utc).astimezone())

    def require_scope(chat: ChatRequest) -> None:
        if (chat.semantic_model_id != expected_scope["semantic_model_id"]
                or list(chat.business_domain_ids) != expected_scope["business_domain_ids"]):
            raise ValueError("EXECUTION_SCOPE_PIN_MISMATCH")

    def context_resolver(chat: ChatRequest, identity: TrustedIdentity):
        require_scope(chat)
        session = ScopedPlanSession(chat, identity, dependencies.publication)
        context = session.context
        pin_identity = context.catalog_pin
        if (pin_identity.catalog_version != receipt["catalog_version"]
                or pin_identity.vector_index_version != receipt["vector_index_version"]):
            raise ValueError("EXECUTION_SCOPE_PIN_MISMATCH")
        session.accept_catalog()
        return context

    async def planner(chat, identity, state, plans):
        require_scope(chat)
        result = await engine.run(chat, identity, state=state, plans=plans)
        if result.plan is None:
            raise ValueError("V2_PLAN_REQUIRED")
        session = ScopedPlanSession(chat, identity, dependencies.publication)
        session.restore(result.next_state, kind="CONVERSATION")
        plan = AuthorizedLogicalPlan.model_validate(
            session.restore(result.plan_state, kind="LAST_REQUEST")
        )
        options = {}
        if plan.payload.time is not None:
            physical = session._pin.snapshot.get("physical_catalog", {})
            fields = [
                field
                for table in physical.get("tables", [])
                for field in table.get("fields", [])
                if field.get("field_id") == settings.limited_scalar_time_field_id
                and field.get("table_id") == settings.limited_scalar_time_table_id
                and field.get("data_source_id") == settings.limited_scalar_data_source_id
            ]
            if len(fields) != 1:
                raise ValueError("ASL2_TIME_PHYSICAL_EVIDENCE_MISMATCH")
            if (plan.payload.time.anchor.canonical_id
                    != settings.limited_scalar_time_field_canonical_id):
                raise ValueError("ASL2_TIME_EVIDENCE_SCOPE_FIELD_MISMATCH")
            evidence = TimeStorageContract(
                evidence_version=settings.limited_scalar_time_evidence_version,
                provenance="DECLARED",
                evidence_reference="USER_DECLARATION_ROUND59_BEIJING",
                context=session.context,
                field_canonical_id=plan.payload.time.anchor.canonical_id,
                field_mapping=settings.limited_scalar_time_field_mapping,
                physical_field_id=settings.limited_scalar_time_field_id,
                table_id=settings.limited_scalar_time_table_id,
                data_source_id=settings.limited_scalar_data_source_id,
                physical_field_digest=contract_digest(fields[0]),
                storage_timezone=settings.limited_scalar_time_storage_timezone,
                storage_semantics="LOCAL_WALL_DATETIME",
                fractional_seconds_precision=0,
                applicability=plan.payload.time.range,
            )
            options = {"time_storage": evidence,
                       "time_evidence_digest": evidence.fingerprint}
        prepared = prepare_execution(
            session, plan, sql_planner=dependencies.sql_planner, **options
        )
        if str(prepared.sql_receipt.get("data_source_id")) != str(
                settings.limited_scalar_data_source_id):
            raise ValueError("EXECUTION_SCOPE_PIN_MISMATCH")
        return PersistedScalarPlan(result.next_state, result.plan_state, prepared)

    store = RedisScalarSessionStore(
        dependencies.redis,
        prefix=settings.limited_scalar_store_namespace,
        deployment_id=settings.limited_scalar_deployment_id,
        ttl_seconds=settings.limited_scalar_session_ttl_seconds,
        idempotency_ttl_seconds=settings.limited_scalar_idempotency_ttl_seconds,
        max_messages=settings.limited_scalar_max_messages_per_session,
        max_envelope_bytes=settings.limited_scalar_max_envelope_bytes,
        production_prefix=settings.session_key_prefix,
    )
    startup_receipt = {
        **receipt,
        "catalog_pin_verified": True,
        "source_pins_verified": True,
        "transport_verified": True,
        "business_sql_executed": False,
        "store_schema": store.schema_version,
        "store_namespace_mode": store.namespace_mode,
    }
    return LimitedScalarRuntimeHandler(
        store=store,
        context_resolver=context_resolver,
        planner=planner,
        transport=dependencies.transport,
        clock=lambda: datetime.now(timezone.utc).astimezone(),
        running_review_seconds=settings.limited_scalar_running_review_seconds,
        startup_receipt=startup_receipt,
    )
