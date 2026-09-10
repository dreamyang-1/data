from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from app.api import router
from app.config import Settings, get_settings
from app.dependencies import build_container
from app.observability.langfuse_client import (
    configure_langfuse,
    is_enabled as langfuse_is_enabled,
    shutdown_langfuse,
)


def create_app(settings: Settings | None = None, *, isolated_chat_handler=None,
               limited_scalar_external=None) -> FastAPI:
    effective_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = build_container(effective_settings)
        handler = isolated_chat_handler
        if handler is None and effective_settings.runtime_mode == "V2_LIMITED_SCALAR":
            from app.semantic_v2.limited_scalar_runtime import build_limited_scalar_handler
            handler = build_limited_scalar_handler(
                effective_settings, external=limited_scalar_external
            )
        app.state.isolated_chat_handler = handler
        configure_langfuse(effective_settings)
        if app.state.container.dataset_cleaner is not None:
            app.state.container.dataset_cleaner.start()
        try:
            yield
        finally:
            if app.state.container.dataset_cleaner is not None:
                await app.state.container.dataset_cleaner.stop()
            redis = getattr(app.state.container.sessions, "redis", None)
            if redis is not None:
                await redis.aclose()
            if handler is not None and hasattr(handler, "aclose"):
                await handler.aclose()
            await asyncio.to_thread(shutdown_langfuse)

    application = FastAPI(
        title="YouoAgent Data Analysis Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    # Explicit dependency seam for an isolated V2 application instance.  The
    # production app is constructed without it and remains on the V1 workflow.
    application.state.isolated_chat_handler = isolated_chat_handler
    application.include_router(router)

    @application.exception_handler(RequestValidationError)
    async def scope_validation_error(request, exc):
        scope_fields = {'semantic_model_id', 'business_domain_id', 'business_domain_ids', 'database_id', 'knowledge_base_names'}
        if any(scope_fields.intersection(str(v) for v in e['loc']) or 'business_domain_id conflicts' in e['msg'] for e in exc.errors()):
            return JSONResponse(status_code=422, content={'detail': {'code': 'REQUEST_SCOPE_INVALID', 'message': 'A strict positive semantic_model_id and a consistent authorized scope are required.'}})
        return await request_validation_exception_handler(request, exc)

    def custom_openapi() -> dict:
        """Keep trusted-header requirements accurate without changing 401 semantics."""
        if application.openapi_schema:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            routes=application.routes,
        )
        for path_item in schema.get("paths", {}).values():
            for method, operation in path_item.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if "data-analysis" not in operation.get("tags", []):
                    continue
                for parameter in operation.get("parameters", []):
                    name = str(parameter.get("name", "")).lower()
                    if name in {"x-tenant-id", "x-user-id"}:
                        parameter["required"] = True
                    elif name == "x-application-id":
                        parameter["required"] = bool(
                            effective_settings.env == "production"
                            or effective_settings.require_trusted_application_header
                        )
        application.openapi_schema = schema
        return schema

    application.openapi = custom_openapi

    @application.get("/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "UP"}

    @application.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        checks: dict[str, bool] = {}
        redis = getattr(application.state.container.sessions, "redis", None)
        if redis is not None:
            try:
                checks["redis_short_memory"] = bool(await redis.ping())
            except Exception:
                checks["redis_short_memory"] = False
        else:
            checks["redis_short_memory"] = (
                effective_settings.env == "test"
                or effective_settings.session_store_mode == "memory"
            )
        memories = application.state.container.memories
        if memories is None:
            checks["mysql_long_memory"] = effective_settings.long_term_memory_mode == "disabled"
        else:
            checks["mysql_long_memory"] = await memories.healthcheck()
        dataset_store = application.state.container.dataset_store
        if effective_settings.minio_dataset_enabled and effective_settings.env != "test":
            try:
                if dataset_store is None:
                    raise RuntimeError("MinIO dataset store is not initialized")
                await asyncio.to_thread(dataset_store.check_ready)
                checks["minio_dataset_store"] = True
            except Exception:
                checks["minio_dataset_store"] = False
        capability_checks: dict[str, bool] = {}
        if effective_settings.adapter_mode == "http":
            adapters = application.state.container.adapters
            query_ok, rewrite_ok, semantic_ok, knowledge_ok = await asyncio.gather(
                adapters.query.health(),
                adapters.query.rewrite_health(),
                adapters.semantic.health(),
                adapters.knowledge.health(),
            )
            capability_checks.update(
                {
                    "asl_sql_query_contract": bool(query_ok),
                    "entity_rewrite_contract": bool(rewrite_ok),
                    "semantic_metadata_contract": bool(semantic_ok),
                    "knowledge_service": bool(knowledge_ok),
                }
            )
        if effective_settings.runtime_mode == "V2_LIMITED_SCALAR":
            handler = getattr(application.state, "isolated_chat_handler", None)
            if handler is None or not hasattr(handler, "readiness"):
                checks["v2_limited_scalar_runtime"] = False
            else:
                try:
                    checks.update(await handler.readiness())
                except Exception:
                    checks["v2_limited_scalar_runtime"] = False
        # Query contract is a core readiness dependency in HTTP mode. Metadata
        # and knowledge are separately reported as optional/degraded features so
        # an outage does not remove basic metric-query capacity from the gateway.
        core_checks = dict(checks)
        if effective_settings.adapter_mode == "http":
            core_checks["asl_sql_query_contract"] = capability_checks[
                "asl_sql_query_contract"
            ]
        is_ready = all(core_checks.values())
        degraded_capabilities = sorted(
            name for name, available in capability_checks.items() if not available
        )
        query_pipeline_ready = bool(
            capability_checks.get("asl_sql_query_contract", True)
        )
        full_feature_ready = bool(is_ready and all(capability_checks.values()))
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "READY" if is_ready else "NOT_READY",
                "adapter_mode": effective_settings.adapter_mode,
                "runtime_mode": effective_settings.runtime_mode,
                "intent_classifier": (
                    "STRUCTURED_MODEL"
                    if effective_settings.intent_model_enabled
                    else "RULE"
                ),
                "checks": checks,
                "capabilities": capability_checks,
                "readiness_profiles": {
                    "core": is_ready,
                    "query_pipeline": is_ready and query_pipeline_ready,
                    "full_feature": full_feature_ready,
                },
                "degraded_capabilities": degraded_capabilities,
                "observability": {
                    "langfuse_enabled": langfuse_is_enabled(),
                    "content_capture": bool(
                        effective_settings.langfuse_capture_content
                    ),
                },
            },
        )

    return application


app = create_app()
