"""Startup wiring for the explicitly enabled V2 limited-scalar candidate.

The default application remains V1.  This module only builds the candidate
when ``runtime_mode=V2_LIMITED_SCALAR`` and every governed dependency pin is
present.  It never reads evaluation captures or constructs synthetic tasks.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from copy import deepcopy
from hashlib import sha256
import importlib
import json
from pathlib import Path
import sys
import time
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlparse
from uuid import uuid5, NAMESPACE_URL

from redis.asyncio import Redis

from app.config import Settings
from app.domain.models import ChatRequest, TrustedIdentity

from .authorized_contract import contract_digest
from .catalog_bridge import ScopedPlanSession
from .completed_question import build_completed_question_display
from .canonical_execution_bridge import build_canonical_analysis_request
from .isolated_execution import TypedExecutionRequest, prepare_execution
from .persisted_scalar_api import (
    PersistedScalarApiHandler,
    PersistedScalarPlan,
    RedisScalarSessionStore,
)
from .pipeline import AuthorizedLogicalPlan
from .recognition import RawTurnPlanner
from .recognition_client import RecognitionModelClient
from .state_machine import ConversationState
from .time_storage import TimeStorageContract


OAGNET_RUNTIME_FILES = (
    "catalog_generation.py", "catalog_publication.py", "catalog_registry.py",
    "catalog_release.py", "catalog_store.py", "catalog_value_candidates.py",
    "catalog_value_sources.py", "config.py", "dimension_scope.py", "mysql_tool.py",
    "vector_store.py",
)
SQL_RUNTIME_FILES = (
    "bound_sql.py", "pinned_catalog.py", "semantic_scope.py",
    "runtime_config.py", "sql_translator_prod.py",
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


def data_source_target_identity(source: dict[str, Any]) -> dict[str, Any]:
    """Return the connection-affecting identity without any credential secret."""

    raw_host = str(source.get("host") or "").strip()
    material = {
        "data_source_id": str(source.get("id") or "").strip(),
        "semantic_model_id": str(source.get("semantic_model_id") or "").strip(),
        "driver": str(source.get("db_type") or "").strip().casefold(),
        "host": raw_host.split(":", 1)[0].strip().casefold(),
        # Match SQLTranslatorProd exactly: a null/zero-ish source value uses
        # the executor's MySQL default rather than becoming an invalid target.
        "port": source.get("port") or 3306,
        "database": str(source.get("db_name") or "").strip(),
        "schema": str(source.get("db_schema") or "").strip(),
        "account": str(source.get("username") or "").strip(),
    }
    try:
        material["port"] = int(material["port"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_TARGET_INVALID:port") from exc
    required = ("data_source_id", "semantic_model_id", "driver", "host", "database", "account")
    missing = [name for name in required if not material[name]]
    if missing or not 1 <= material["port"] <= 65535:
        suffix = ",".join(missing or ["port"])
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_TARGET_INVALID:" + suffix)
    return material


def data_source_target_digest(source: dict[str, Any]) -> str:
    return contract_digest(data_source_target_identity(source))


def _limited_scalar_payload(payload):
    """Reject non-scalar plans before scalar-only lowering reads their fields."""

    if getattr(payload, "payload_type", None) != "SCALAR_AGGREGATE":
        raise ValueError("EXECUTION_PAYLOAD_UNSUPPORTED")
    return payload


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
        "data_source_target_digest": settings.limited_scalar_data_source_target_digest,
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
        "data_source_target_digest": settings.limited_scalar_data_source_target_digest,
    }


def _prepend_runtime_roots(settings: Settings) -> None:
    for root in (settings.limited_scalar_sql_translator_root,
                 settings.limited_scalar_oagnet_root):
        value = str(Path(root).resolve())
        if value not in sys.path:
            sys.path.insert(0, value)


def _import_runtime_module(name: str, root: Path):
    expected = (Path(root).resolve() / f"{name}.py").resolve()
    if not expected.is_file():
        raise RuntimeError(f"LIMITED_SCALAR_RUNTIME_MODULE_MISSING:{name}.py")
    module = importlib.import_module(name)
    actual_file = getattr(module, "__file__", None)
    if actual_file is None or Path(actual_file).resolve() != expected:
        raise RuntimeError(f"LIMITED_SCALAR_RUNTIME_MODULE_ORIGIN_MISMATCH:{name}")
    return module


def _catalog_where_matches(metadata: dict[str, Any], where: dict[str, Any]) -> bool:
    if "$and" in where:
        values = where["$and"]
        return isinstance(values, list) and all(
            _catalog_where_matches(metadata, value) for value in values
        )
    if "$or" in where:
        values = where["$or"]
        return isinstance(values, list) and any(
            _catalog_where_matches(metadata, value) for value in values
        )
    for key, expected in where.items():
        actual = metadata.get(key)
        if isinstance(expected, dict):
            if set(expected) != {"$in"} or not isinstance(
                expected["$in"], (list, tuple, set)
            ):
                raise RuntimeError("LIMITED_SCALAR_CATALOG_FILTER_UNSUPPORTED")
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class _ProcessMemoryCatalogStore:
    """Exact catalog lookup for the internal trial; it has no vector writes."""

    embedding_dim = 2

    def __init__(self, target_identity: dict[str, Any], search_result_type: type):
        self.catalog_target_identity = deepcopy(target_identity)
        self._search_result_type = search_result_type
        self._records: dict[str, Any] = {}

    def add(self, records) -> None:
        for record in records:
            if record.id in self._records:
                raise RuntimeError("LIMITED_SCALAR_CATALOG_RECORD_DUPLICATE")
            self._records[record.id] = deepcopy(record)

    def _rows(self, where: dict[str, Any]):
        return [
            deepcopy(record)
            for record in self._records.values()
            if _catalog_where_matches(record.metadata, where)
        ]

    def get_catalog_inventory(self, where: dict[str, Any]):
        return self._rows(where)

    def get_by_where(self, where: dict[str, Any]):
        return [
            self._search_result_type(
                record.id, 1.0, record.text, deepcopy(record.metadata)
            )
            for record in self._rows(where)
        ]

    find_exact = get_by_where

    def search(self, *_args, **_kwargs):
        # V2 context binding uses exact governed candidates. The retained
        # downstream Oagnet adapter continues to own existing vector retrieval.
        raise RuntimeError("LIMITED_SCALAR_CONTEXT_VECTOR_SEARCH_UNSUPPORTED")


class _ProcessMemoryCatalogRegistry:
    """Process-local activation state with deterministic restart identity."""

    def __init__(self, target_identity: dict[str, Any], deployment_id: str,
                 digest_fn: Callable[[Any], str]):
        self.target_identity_hash = digest_fn(target_identity)
        self._deployment_id = deployment_id
        self._digest = digest_fn
        self._active: dict | None = None
        self._manifest: dict | None = None

    def active(self, _scope):
        return deepcopy(self._active)

    def reserve(self, manifest) -> None:
        if self._manifest is not None:
            raise RuntimeError("LIMITED_SCALAR_CATALOG_ALREADY_RESERVED")
        self._manifest = deepcopy(manifest)

    def manifest(self, scope, version):
        value = self._manifest
        if (value is None or value.get("scope") != scope
                or value.get("vector_index_version") != version):
            raise RuntimeError("LIMITED_SCALAR_CATALOG_MANIFEST_MISMATCH")
        return deepcopy(value)

    def activate_verified(self, manifest, expected_active):
        if expected_active != self._active or manifest != self._manifest:
            raise RuntimeError("LIMITED_SCALAR_CATALOG_ACTIVATION_CONFLICT")
        activation_id = uuid5(
            NAMESPACE_URL,
            self._digest({
                "deployment_id": self._deployment_id,
                "vector_index_version": manifest["vector_index_version"],
            }),
        ).hex
        self._active = {
            "state": "PUBLISHED",
            "vector_index_version": manifest["vector_index_version"],
            "activation_id": activation_id,
        }
        return deepcopy(self._active)


def _open_live_read_only_catalog(settings: Settings, modules: dict[str, Any]):
    """Pin current catalog authority without writing Redis or Milvus.

    This is deliberately limited to the internal context replacement trial.
    The snapshot is captured again by every pin/finish through Oagnet's existing
    publication checks, so an authority change fails the request closed.
    """

    capture = modules["catalog_publication"].capture_catalog
    snapshot = capture(
        settings.limited_scalar_semantic_model_id,
        settings.limited_scalar_business_domain_ids,
    )
    expected_scope = {
        "semantic_model_id": settings.limited_scalar_semantic_model_id,
        "business_domain_ids": list(settings.limited_scalar_business_domain_ids),
        "scope_mode": "EXPLICIT_DOMAINS",
    }
    if (snapshot.get("scope") != expected_scope
            or snapshot.get("catalog_version")
                != settings.limited_scalar_catalog_version):
        raise RuntimeError("LIMITED_SCALAR_CATALOG_PIN_MISMATCH")
    target_identity = {
        "backend": "process-memory-read-only-catalog",
        "source_identity_hash": snapshot.get("source_identity_hash"),
        "scope": expected_scope,
    }
    digest_fn = modules["catalog_publication"].digest
    store = _ProcessMemoryCatalogStore(
        target_identity,
        modules["vector_store"].SearchResult,
    )
    registry = _ProcessMemoryCatalogRegistry(
        target_identity,
        settings.limited_scalar_deployment_id,
        digest_fn,
    )
    if registry.target_identity_hash != settings.limited_scalar_catalog_target_identity_hash:
        raise RuntimeError("LIMITED_SCALAR_CATALOG_TARGET_MISMATCH")
    publication = modules["catalog_publication"].CatalogPublication(
        store, registry, capture
    )
    receipt = publication.publish(
        expected_scope["semantic_model_id"],
        expected_scope["business_domain_ids"],
        embed_fn=lambda texts: [[0.1, 0.2] for _ in texts],
        publication_id="internal-context-replacement-trial",
        producer_revision=settings.limited_scalar_oagnet_source_digest,
        embedding_contract="process-memory-exact-catalog-v1",
        expected_catalog_version=settings.limited_scalar_catalog_version,
    )
    if receipt.get("vector_index_version") != settings.limited_scalar_vector_index_version:
        raise RuntimeError("LIMITED_SCALAR_CATALOG_PIN_MISMATCH")
    return publication


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
    data_source_target_digest: str = ""
    module_origins: dict[str, str] = field(default_factory=dict)


class LimitedScalarRuntimeHandler(PersistedScalarApiHandler):
    def __init__(self, *args, startup_receipt: dict,
                 readiness_probe: Callable[[], Awaitable[dict[str, bool]]],
                 readiness_cache_seconds: float, readiness_timeout_seconds: float,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.startup_receipt = startup_receipt
        self._readiness_probe = readiness_probe
        self._readiness_cache_seconds = readiness_cache_seconds
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._readiness_cached_at = 0.0
        self._readiness_cached: dict[str, bool] | None = None
        self._readiness_lock = asyncio.Lock()

    async def readiness(self) -> dict[str, bool]:
        now = time.monotonic()
        if (self._readiness_cached is not None
                and now - self._readiness_cached_at < self._readiness_cache_seconds):
            return dict(self._readiness_cached)
        async with self._readiness_lock:
            now = time.monotonic()
            if (self._readiness_cached is not None
                    and now - self._readiness_cached_at < self._readiness_cache_seconds):
                return dict(self._readiness_cached)
            try:
                checks = await asyncio.wait_for(
                    self._readiness_probe(), timeout=self._readiness_timeout_seconds
                )
            except Exception:
                checks = {
                    "v2_limited_scalar_redis": False,
                    "v2_limited_scalar_catalog_pin": False,
                    "v2_limited_scalar_data_source_target": False,
                    "v2_limited_scalar_runtime_sources": False,
                    "v2_limited_scalar_read_only_transport": False,
                }
            self._readiness_cached = {name: bool(value) for name, value in checks.items()}
            self._readiness_cached_at = time.monotonic()
            return dict(self._readiness_cached)


def _build_external_dependencies(settings: Settings) -> LimitedScalarExternalDependencies:
    _prepend_runtime_roots(settings)
    roots = {
        "catalog_publication": settings.limited_scalar_oagnet_root,
        "catalog_registry": settings.limited_scalar_oagnet_root,
        "catalog_store": settings.limited_scalar_oagnet_root,
        "pinned_catalog": settings.limited_scalar_sql_translator_root,
        "semantic_scope": settings.limited_scalar_sql_translator_root,
        "sql_translator_prod": settings.limited_scalar_sql_translator_root,
        "vector_store": settings.limited_scalar_oagnet_root,
    }
    modules = {name: _import_runtime_module(name, root) for name, root in roots.items()}
    CatalogPublication = modules["catalog_publication"].CatalogPublication
    RedisCatalogReleaseRegistry = modules["catalog_registry"].RedisCatalogReleaseRegistry
    open_catalog_store = modules["catalog_store"].open_catalog_store
    translate_pinned_catalog = modules["pinned_catalog"].translate_pinned_catalog
    RequestScope = modules["semantic_scope"].RequestScope
    SQLTranslatorProd = modules["sql_translator_prod"].SQLTranslatorProd

    if settings.limited_scalar_catalog_access == "LIVE_READ_ONLY_SNAPSHOT":
        publication = _open_live_read_only_catalog(settings, modules)
    else:
        store = open_catalog_store(
            initialize=False,
            expected_target_identity_hash=settings.limited_scalar_catalog_target_identity_hash,
        )
        publication = CatalogPublication(
            store,
            RedisCatalogReleaseRegistry(store.catalog_target_identity),
        )
    # SQLTranslatorProd owns the semantic registry connection contract.  The
    # Agent Redis URL is the V2 session store and can legitimately use another
    # database; using it here would read the wrong registry namespace.
    translator = SQLTranslatorProd()
    source = translator.fetch_data_source(
        str(settings.limited_scalar_semantic_model_id),
        str(settings.limited_scalar_data_source_id),
    )
    if (not source
            or str(source.get("id")) != str(settings.limited_scalar_data_source_id)
            or str(source.get("semantic_model_id")) != str(settings.limited_scalar_semantic_model_id)):
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_SCOPE_MISMATCH")
    target_digest = data_source_target_digest(source)
    if target_digest != settings.limited_scalar_data_source_target_digest:
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_TARGET_MISMATCH")

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
        data_source_target_digest=target_digest,
        module_origins={
            name: str(Path(module.__file__).resolve())
            for name, module in modules.items()
        },
    )


def build_limited_scalar_handler(
    settings: Settings,
    *,
    external: LimitedScalarExternalDependencies | None = None,
    query_adapter: Any | None = None,
) -> LimitedScalarRuntimeHandler:
    receipt = validate_limited_scalar_settings(settings)
    dependencies = external or _build_external_dependencies(settings)
    expected_scope = receipt["scope"]
    if dependencies.data_source_target_digest != receipt["data_source_target_digest"]:
        raise RuntimeError("LIMITED_SCALAR_DATA_SOURCE_TARGET_MISMATCH")

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

    async def readiness_probe() -> dict[str, bool]:
        try:
            redis_ready = bool(await dependencies.redis.ping())
        except Exception:
            redis_ready = False

        def verify_catalog() -> bool:
            current = dependencies.publication.pin(
                expected_scope["semantic_model_id"], expected_scope["business_domain_ids"]
            )
            try:
                current_identity = current.identity
                return bool(
                    current_identity.get("catalog_version") == receipt["catalog_version"]
                    and current_identity.get("vector_index_version") == receipt["vector_index_version"]
                    and current_identity.get("target_identity_hash")
                        == settings.limited_scalar_catalog_target_identity_hash
                )
            finally:
                current.finish()

        try:
            catalog_ready = bool(await asyncio.to_thread(verify_catalog))
        except Exception:
            catalog_ready = False
        return {
            "v2_limited_scalar_redis": redis_ready,
            "v2_limited_scalar_catalog_pin": catalog_ready,
            "v2_limited_scalar_data_source_target": (
                dependencies.data_source_target_digest == receipt["data_source_target_digest"]
            ),
            "v2_limited_scalar_runtime_sources": bool(
                receipt["oagnet_source_digest"] and receipt["sql_source_digest"]
            ),
            "v2_limited_scalar_read_only_transport": True,
        }

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
        previous_state = (
            ConversationState.model_validate(state.payload)
            if state is not None else None
        )
        result = await engine.run(chat, identity, state=state, plans=plans)
        if result.plan is None:
            raise ValueError("V2_PLAN_REQUIRED")
        session = ScopedPlanSession(chat, identity, dependencies.publication)
        session.restore(result.next_state, kind="CONVERSATION")
        plan = AuthorizedLogicalPlan.model_validate(
            session.restore(result.plan_state, kind="LAST_REQUEST")
        )
        payload = _limited_scalar_payload(plan.payload)
        options = {}
        if payload.time is not None:
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
            if (payload.time.anchor.canonical_id
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
                applicability=payload.time.range,
            )
            options = {"time_storage": evidence,
                       "time_evidence_digest": evidence.fingerprint}
        prepared = prepare_execution(
            session, plan, sql_planner=dependencies.sql_planner, **options
        )
        if str(prepared.sql_receipt.get("data_source_id")) != str(
                settings.limited_scalar_data_source_id):
            raise ValueError("EXECUTION_SCOPE_PIN_MISMATCH")
        next_state = ConversationState.model_validate(result.next_state.payload)
        display = build_completed_question_display(
            message_id=chat.message_id,
            plan=plan,
            previous_state=previous_state,
            next_state=next_state,
            context_trace=result.context_trace,
        )
        canonical_request = build_canonical_analysis_request(
            chat=chat,
            identity=identity,
            plan=plan,
            display=display,
        )
        return PersistedScalarPlan(
            result.next_state,
            result.plan_state,
            prepared,
            display,
            canonical_request,
        )

    async def canonical_query(request, identity):
        if query_adapter is None:
            raise ValueError("V2_CANONICAL_QUERY_ADAPTER_REQUIRED")
        domains = list(request.business_domain_ids)
        if len(domains) > 1:
            raise ValueError("EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED")
        return await query_adapter.query(
            request,
            identity,
            semantic_model_id=request.semantic_model_id,
            business_domain_id=(domains[0] if domains else None),
        )

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
        "data_source_target_verified": True,
        "runtime_module_origins_verified": external is None,
        "business_sql_executed": False,
        "store_schema": store.schema_version,
        "store_namespace_mode": store.namespace_mode,
    }
    return LimitedScalarRuntimeHandler(
        store=store,
        context_resolver=context_resolver,
        planner=planner,
        transport=dependencies.transport,
        canonical_query=(canonical_query if query_adapter is not None else None),
        clock=lambda: datetime.now(timezone.utc).astimezone(),
        running_review_seconds=settings.limited_scalar_running_review_seconds,
        cancellation_cleanup_seconds=settings.limited_scalar_cancellation_cleanup_seconds,
        startup_receipt=startup_receipt,
        readiness_probe=readiness_probe,
        readiness_cache_seconds=settings.limited_scalar_readiness_cache_seconds,
        readiness_timeout_seconds=settings.limited_scalar_readiness_timeout_seconds,
    )
