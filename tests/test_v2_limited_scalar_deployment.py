import json
from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app
from app.semantic_v2.limited_scalar_runtime import (
    LimitedScalarExternalDependencies,
    OAGNET_RUNTIME_FILES,
    SQL_RUNTIME_FILES,
    build_limited_scalar_handler,
    source_bundle_digest,
    validate_limited_scalar_settings,
)
from app.semantic_v2.persisted_scalar_api import RedisScalarSessionStore
from app.stores import MessageIdReuseConflictError
from app.semantic_v2.state_machine import ConversationState, StateTransitionError
from test_v2_authorized_catalog_bridge import IDENTITY, request
from test_v2_asl2_lowering import provider
from test_v2_persisted_scalar_api import NOW, planned_artifacts, prepared_for


class DeploymentRedis:
    def __init__(self):
        self.values = {}
        self.calls = 0
        self.fail_call = None

    async def ping(self):
        return True

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, key_count, *args):
        self.calls += 1
        if self.fail_call == self.calls:
            return 0
        if key_count == 1:
            key, expected, encoded, _ttl = args
            current = json.loads(self.values[key]).get("revision", 0) if key in self.values else 0
            if current != int(expected):
                return 0
            self.values[key] = encoded
            return 1
        assert key_count == 2
        state_key, guard_key, expected, encoded, _ttl, guard, _guard_ttl = args
        current = json.loads(self.values[state_key]).get("revision", 0) if state_key in self.values else 0
        if current != int(expected):
            return 0
        if guard_key in self.values:
            return -1
        self.values[state_key] = encoded
        self.values[guard_key] = guard
        return 1

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def aclose(self):
        return None


def candidate_settings(provider, **updates):
    pin = provider[0].pin(81, [205])
    identity = pin.identity
    pin.finish()
    project = Settings().limited_scalar_oagnet_root.parent
    values = {
        "env": "test",
        "adapter_mode": "mock",
        "runtime_mode": "V2_LIMITED_SCALAR",
        "session_store_mode": "redis",
        "redis_url": "redis://127.0.0.1:6379/15",
        "intent_model_enabled": True,
        "intent_model_api_key": SecretStr("fake"),
        "intent_model_max_retries": 0,
        "intent_model_enable_thinking": False,
        "business_question_collection_enabled": False,
        "long_term_memory_mode": "disabled",
        "langfuse_enabled": False,
        "limited_scalar_store_namespace": "youo:data-analysis:v2-limited-scalar:test-deploy",
        "limited_scalar_deployment_id": "test-deploy",
        "limited_scalar_catalog_version": identity["catalog_version"],
        "limited_scalar_vector_index_version": identity["vector_index_version"],
        "limited_scalar_catalog_target_identity_hash": identity["target_identity_hash"],
        "limited_scalar_oagnet_source_digest": source_bundle_digest(
            project / "Oagnet", OAGNET_RUNTIME_FILES
        ),
        "limited_scalar_sql_source_digest": source_bundle_digest(
            project / "sql-translator", SQL_RUNTIME_FILES
        ),
        "limited_scalar_time_field_canonical_id": "sm81_bd205:attr:sales_order.created_date",
    }
    values.update(updates)
    return Settings().model_copy(update=values)


def dependencies(provider, redis, transport, model=None):
    class NoCallModel:
        async def complete(self, **kwargs):
            raise AssertionError("startup must not call the model")

    return LimitedScalarExternalDependencies(
        publication=provider[0],
        model=model or NoCallModel(),
        sql_planner=lambda *args, **kwargs: None,
        transport=transport,
        redis=redis,
    )


def deployment_store(redis):
    return RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2-limited-scalar:test-deploy",
        deployment_id="test-deploy",
        ttl_seconds=3600,
        idempotency_ttl_seconds=7200,
    )


def handler_for(provider, redis, transport, *, clock=lambda: NOW):
    prepared = prepared_for(provider)
    state, plan = planned_artifacts(prepared)

    async def planner(chat, identity, restored, plans):
        return __import__(
            "app.semantic_v2.persisted_scalar_api", fromlist=["PersistedScalarPlan"]
        ).PersistedScalarPlan(state, plan, prepared)

    from app.semantic_v2.persisted_scalar_api import PersistedScalarApiHandler
    return PersistedScalarApiHandler(
        store=deployment_store(redis),
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=clock,
        running_review_seconds=60,
    ), prepared


def valid_transport(prepared, calls):
    async def transport(req):
        calls.append(req)
        column = prepared.lowering.output_bindings[0].result_column_name
        return {
            "request_fingerprint": req.fingerprint,
            "prepared_fingerprint": req.prepared_fingerprint,
            "context_fingerprint": req.context.fingerprint(),
            "data_source_id": req.data_source_id,
            "provenance": "LIVE_READ_ONLY",
            "submitted": True,
            "result": {
                "success": True,
                "data": [{column: 3}],
                "columns": [column],
                "row_count": 1,
                "snapshot_id": "snapshot",
                "data_as_of": NOW.isoformat(),
                "quality_status": "PASS",
                "quality_checks": dict.fromkeys(
                    ["consistent_snapshot", "read_only_transaction",
                     "column_contract_valid", "row_contract_valid",
                     "row_count_reconciled", "statement_timeout_enforced"], True
                ),
            },
        }
    return transport


def test_default_runtime_remains_v1_and_candidate_config_fails_closed(provider):
    assert Settings().runtime_mode == "V1"
    broken = Settings().model_copy(update={
        "runtime_mode": "V2_LIMITED_SCALAR",
        "intent_model_api_key": SecretStr("fake"),
        "intent_model_max_retries": 0,
    })
    with pytest.raises(RuntimeError, match="LIMITED_SCALAR_CONFIGURATION_MISSING"):
        validate_limited_scalar_settings(broken)


def test_candidate_requires_exact_time_anchor_pin(provider):
    settings = candidate_settings(provider, limited_scalar_time_field_canonical_id="")
    with pytest.raises(RuntimeError, match="time_field_canonical_id"):
        validate_limited_scalar_settings(settings)


def test_explicit_mode_builds_native_handler_without_model_or_sql(provider):
    redis = DeploymentRedis()

    async def forbidden(_request):
        raise AssertionError("startup must not execute SQL")

    settings = candidate_settings(provider)
    handler = build_limited_scalar_handler(
        settings, external=dependencies(provider, redis, forbidden)
    )
    assert handler.startup_receipt["business_sql_executed"] is False
    assert handler.store.namespace_mode == "STABLE_DEPLOYMENT"
    assert redis.values == {}


def test_application_factory_selects_candidate_only_from_startup_config(provider):
    redis = DeploymentRedis()

    async def forbidden(_request):
        raise AssertionError

    settings = candidate_settings(provider, trusted_backend_token=SecretStr("token"))
    app = create_app(
        settings,
        limited_scalar_external=dependencies(provider, redis, forbidden),
    )
    with TestClient(app) as client:
        assert app.state.isolated_chat_handler is not None
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["runtime_mode"] == "V2_LIMITED_SCALAR"


@pytest.mark.asyncio
async def test_stable_namespace_survives_new_store_instance_and_replay_is_idempotent(provider):
    redis = DeploymentRedis()
    calls = []
    placeholder = prepared_for(provider)
    handler, prepared = handler_for(provider, redis, valid_transport(placeholder, calls))
    # Rebuild transport against the actual prepared object returned by helper.
    handler.transport = valid_transport(prepared, calls)
    chat = request()
    first = await handler.handle(chat, IDENTITY)
    second_store = deployment_store(redis)
    handler.store = second_store
    repeated = await handler.handle(chat, IDENTITY)
    assert first.status == "COMPLETED" and repeated == first and len(calls) == 1
    assert len(redis.values) == 2  # one atomic envelope and one idempotency guard


@pytest.mark.asyncio
async def test_expired_envelope_guard_prevents_blind_message_reexecution(provider):
    redis = DeploymentRedis()
    calls = []
    prepared = prepared_for(provider)
    handler, prepared = handler_for(provider, redis, valid_transport(prepared, calls))
    chat = request()
    assert (await handler.handle(chat, IDENTITY)).status == "COMPLETED"
    state_key = next(key for key in redis.values if ":conversation:" in key)
    redis.values.pop(state_key)
    response = await handler.handle(chat, IDENTITY)
    assert response.error_code == "EXECUTION_SESSION_EXPIRED_REUSE_REJECTED"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pre_stream_conflict_check_reads_v2_claim_without_execution(provider):
    redis = DeploymentRedis()
    calls = []
    prepared = prepared_for(provider)
    handler, prepared = handler_for(provider, redis, valid_transport(prepared, calls))
    chat = request()
    assert (await handler.handle(chat, IDENTITY)).status == "COMPLETED"
    conflicting = chat.model_copy(update={"question": chat.question + " different"})
    with pytest.raises(MessageIdReuseConflictError):
        await handler.check_message_conflict(conflicting, IDENTITY)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_becomes_unknown_and_operator_review_never_reexecutes(provider):
    redis = DeploymentRedis()
    calls = 0

    async def timeout(_request):
        nonlocal calls
        calls += 1
        raise TimeoutError

    handler, prepared = handler_for(provider, redis, timeout)
    chat = request()
    response = await handler.handle(chat, IDENTITY)
    assert response.error_code == "EXECUTION_TIMEOUT_OUTCOME_UNKNOWN"
    identity = handler._identity(chat, IDENTITY)
    snapshot = await handler.store.load(prepared.plan.permission_requirement, identity)
    record = snapshot.message(chat.message_id)
    assert record["status"] == "UNKNOWN"
    assert record.get("result") is None
    reviewed = await handler.store.require_operator_review(
        snapshot,
        message_id=chat.message_id,
        request_fingerprint=record["request_fingerprint"],
        reason="database outcome cannot be proven",
        operator="test-operator",
        context=prepared.plan.permission_requirement,
        state_identity=identity,
        observed_at=NOW + timedelta(minutes=10),
    )
    assert reviewed.message(chat.message_id)["status"] == "REVIEW_REQUIRED"
    repeated = await handler.handle(chat, IDENTITY)
    assert repeated.error_code == "EXECUTION_OUTCOME_REQUIRES_OPERATOR_REVIEW"
    assert calls == 1


@pytest.mark.asyncio
async def test_incompatible_envelope_and_publish_conflict_fail_closed(provider):
    redis = DeploymentRedis()
    store = deployment_store(redis)
    prepared = prepared_for(provider)
    chat = request()
    identity = {
        "conversation_id": chat.conversation_id,
        "tenant_id": IDENTITY.tenant_id,
        "user_id": IDENTITY.user_id,
        "application_id": chat.application_id,
    }
    key = store._key(prepared.plan.permission_requirement, identity)
    redis.values[key] = json.dumps({"schema_version": "future-v9", "state_version": 0})
    with pytest.raises(ValueError, match="PERSISTED_SESSION_CONTRACT_INVALID"):
        await store.load(prepared.plan.permission_requirement, identity)


@pytest.mark.asyncio
async def test_stale_stage_snapshot_cannot_overwrite_newer_envelope_revision(provider):
    redis = DeploymentRedis()
    store = deployment_store(redis)
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    state = ConversationState.model_validate(state_artifact.payload)
    task_id = next(iter(state.tasks))
    attempt = __import__("app.semantic_v2.models", fromlist=["ExecutionAttemptRecord"]).ExecutionAttemptRecord(
        execution_id="test:cas", task_id=task_id, task_version=1,
        attempt_number=1, status="RUNNING", started_at=NOW,
        execution_backend="SEMANTIC_QUERY", snapshot_id="test:pending",
        catalog_version=prepared.plan.permission_requirement.catalog_pin.catalog_version,
        vector_index_version=prepared.plan.permission_requirement.catalog_pin.vector_index_version,
        semantic_model_version=prepared.plan.snapshot_requirement.semantic_model_version,
        policy_version=prepared.plan.version_metadata.policy_version,
        asl_digest="a", sql_digest="b",
    )
    identity = {
        "conversation_id": state.conversation_id,
        "tenant_id": state.tenant_id,
        "user_id": state.user_id,
        "application_id": state.application_id,
    }
    empty = await store.load(prepared.plan.permission_requirement, identity)
    running = await store.begin(
        empty, planned_state=state_artifact, plan_state=plan_artifact,
        attempt=attempt, message_id="message", request_fingerprint="fingerprint",
        context=prepared.plan.permission_requirement, state_identity=identity,
        started_at=NOW,
    )
    newer = await store.mark_stage(
        running, message_id="message", request_fingerprint="fingerprint",
        stage="SUBMISSION_ATTEMPTED", context=prepared.plan.permission_requirement,
        state_identity=identity, observed_at=NOW,
    )
    assert newer.revision == running.revision + 1
    with pytest.raises(StateTransitionError, match="changed concurrently"):
        await store.mark_stage(
            running, message_id="message", request_fingerprint="fingerprint",
            stage="RESULT_RETURNED", context=prepared.plan.permission_requirement,
            state_identity=identity, observed_at=NOW,
        )


@pytest.mark.asyncio
async def test_clear_barrier_and_removed_metric_survive_store_restore(provider):
    redis = DeploymentRedis()
    store = deployment_store(redis)
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    state = ConversationState.model_validate(state_artifact.payload)
    task_id = next(iter(state.tasks))
    raw = state.model_dump(mode="json")
    raw["tasks"][task_id]["clear_barriers"] = ["filter_expression"]
    raw["tasks"][task_id]["versions"][0]["semantics"]["metrics"] = []
    changed = ConversationState.model_validate(raw)
    payload = changed.model_dump(mode="json")
    from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
    state_artifact = ScopedArtifact(
        kind="CONVERSATION", context=state_artifact.context, payload=payload,
        payload_digest=contract_digest(payload),
    )
    attempt = __import__("app.semantic_v2.models", fromlist=["ExecutionAttemptRecord"]).ExecutionAttemptRecord(
        execution_id="test:running", task_id=task_id, task_version=1,
        attempt_number=1, status="RUNNING", started_at=NOW,
        execution_backend="SEMANTIC_QUERY", snapshot_id="test:pending",
        catalog_version=prepared.plan.permission_requirement.catalog_pin.catalog_version,
        vector_index_version=prepared.plan.permission_requirement.catalog_pin.vector_index_version,
        semantic_model_version=prepared.plan.snapshot_requirement.semantic_model_version,
        policy_version=prepared.plan.version_metadata.policy_version,
        asl_digest="a", sql_digest="b",
    )
    previous = await store.load(prepared.plan.permission_requirement, {
        "conversation_id": "conversation", "tenant_id": "tenant",
        "user_id": "user", "application_id": "app",
    })
    saved = await store.begin(
        previous, planned_state=state_artifact, plan_state=plan_artifact,
        attempt=attempt, message_id="message", request_fingerprint="fingerprint",
        context=prepared.plan.permission_requirement,
        state_identity={"conversation_id": "conversation", "tenant_id": "tenant",
                        "user_id": "user", "application_id": "app"},
        started_at=NOW,
    )
    restored = ConversationState.model_validate(saved.state.payload)
    task = restored.tasks[task_id]
    assert task.clear_barriers == ["filter_expression"]
    assert task.versions[0].semantics.metrics == []
