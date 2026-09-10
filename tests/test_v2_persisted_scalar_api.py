import asyncio
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.stores import MessageIdReuseConflictError
from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
from app.semantic_v2 import models as m
from app.semantic_v2.isolated_execution import prepare_execution
from app.semantic_v2.persisted_scalar_api import (
    PersistedScalarApiHandler,
    PersistedScalarPlan,
    RedisScalarSessionStore,
    exact_result_payload,
    restore_exact_result,
    seal_exact_result,
)
from app.semantic_v2.state_machine import ConversationState, TaskState, TaskVersion, TopicState
from test_v2_asl2_lowering import args, current, payload, provider, sql
from test_v2_authorized_catalog_bridge import IDENTITY, request

SQL = Path(__file__).resolve().parents[2] / "sql-translator"
sys.path.append(str(SQL))
from sql_translator_prod import SQLTranslatorProd


NOW = datetime.fromisoformat("2026-09-09T09:00:00+08:00")


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.eval_calls = 0
        self.eval_ttls = []

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, key_count, key, expected, encoded, ttl):
        self.eval_calls += 1
        self.eval_ttls.append(int(ttl))
        current = json.loads(self.values[key])["state_version"] if key in self.values else 0
        if current != int(expected):
            return 0
        self.values[key] = encoded
        return 1

    async def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    async def aclose(self):
        return None


def prepared_for(provider):
    session = current(provider)
    plan = session._compile_logical(**args(session, payload(session))).logical_plan
    return prepare_execution(session, plan, sql_planner=sql)


def planned_artifacts(prepared):
    plan = prepared.plan
    state = ConversationState(
        **prepared.state_identity,
        state_version=1,
        active_topic_id=plan.topic_id,
        topic_stack=[plan.topic_id],
        recent_turn_ids=["message"],
        topics={
            plan.topic_id: TopicState(
                topic_id=plan.topic_id,
                title="TEST_ONLY",
                active_task_id=plan.task_id,
                task_ids=[plan.task_id],
                last_accessed_at=NOW,
            )
        },
        tasks={
            plan.task_id: TaskState(
                task_id=plan.task_id,
                topic_id=plan.topic_id,
                active_version=plan.task_version,
                status="RESOLVED",
                versions=[
                    TaskVersion(
                        version=plan.task_version,
                        status="RESOLVED",
                        plan_id=plan.plan_id,
                        created_at=NOW,
                        semantics=m.TaskSemanticState(metrics=plan.payload.measures),
                    )
                ],
            )
        },
    )
    state_payload = state.model_dump(mode="json")
    state_artifact = ScopedArtifact(
        kind="CONVERSATION",
        context=plan.permission_requirement,
        payload=state_payload,
        payload_digest=contract_digest(state_payload),
    )
    plan_payload = plan.model_dump(mode="json")
    plan_artifact = ScopedArtifact(
        kind="LAST_REQUEST",
        context=plan.permission_requirement,
        payload=plan_payload,
        payload_digest=contract_digest(plan_payload),
    )
    return state_artifact, plan_artifact


@pytest.mark.parametrize(
    "value",
    [
        Decimal("0.10"),
        Decimal("12345678901234567890.123456789012345678"),
        Decimal("-1.2300"),
        Decimal("0"),
        2**80,
        0,
        None,
        0.125,
    ],
)
def test_exact_scalar_encoding_round_trips_without_float_coercion(provider, value):
    prepared = prepared_for(provider)
    column = prepared.lowering.output_bindings[0].result_column_name
    result = {
        "columns": [column],
        "data": [{column: value}],
        "snapshot_id": "snapshot",
        "data_as_of": NOW.isoformat(),
        "quality_status": "PASS",
    }
    artifact = seal_exact_result(prepared.plan.permission_requirement, result)
    restored = restore_exact_result(artifact, prepared.plan.permission_requirement)
    assert restored["data"][0][column] == value
    assert type(restored["data"][0][column]) is type(value)
    assert seal_exact_result(prepared.plan.permission_requirement, restored).payload_digest == artifact.payload_digest


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf"), True, object()])
def test_exact_scalar_encoding_rejects_non_finite_or_unsupported_values(value):
    with pytest.raises(ValueError):
        exact_result_payload({"columns": ["v"], "data": [{"v": value}]})


def test_sql_decimal_preservation_is_opt_in_and_v1_default_is_unchanged():
    source = [{"amount": Decimal("9007199254740993.0100"), "count": 3}]
    legacy = SQLTranslatorProd._convert_types(source)
    exact = SQLTranslatorProd._convert_types(source, preserve_decimal=True)
    assert type(legacy[0]["amount"]) is float
    assert exact == source and type(exact[0]["amount"]) is Decimal
    assert type(exact[0]["count"]) is int


@pytest.mark.asyncio
async def test_persisted_handler_atomically_saves_exact_result_and_is_idempotent(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    redis = FakeRedis()
    store = RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2:isolated:round5-11:round511test",
        run_id="round511test",
        ttl_seconds=7200,
    )
    calls = []

    async def planner(chat, identity, state, plans):
        assert state is None and plans == ()
        return PersistedScalarPlan(state_artifact, plan_artifact, prepared)

    async def transport(execution_request):
        calls.append(execution_request)
        column = prepared.lowering.output_bindings[0].result_column_name
        result = {
            "success": True,
            "data": [{column: Decimal("9007199254740993.0100")}],
            "columns": [column],
            "row_count": 1,
            "snapshot_id": "snapshot",
            "data_as_of": NOW.isoformat(),
            "quality_status": "PASS",
            "quality_checks": {
                "consistent_snapshot": True,
                "read_only_transaction": True,
                "column_contract_valid": True,
                "row_contract_valid": True,
                "row_count_reconciled": True,
                "statement_timeout_enforced": True,
            },
        }
        return {
            "request_fingerprint": execution_request.fingerprint,
            "prepared_fingerprint": execution_request.prepared_fingerprint,
            "context_fingerprint": execution_request.context.fingerprint(),
            "data_source_id": execution_request.data_source_id,
            "provenance": "LIVE_READ_ONLY",
            "submitted": True,
            "result": result,
        }

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
    )
    chat = request()
    response = await handler.handle(chat, IDENTITY)
    repeated = await handler.handle(chat, IDENTITY)
    assert response.status == "COMPLETED" and repeated == response
    assert len(calls) == 1 and len(redis.values) == 1
    assert redis.eval_ttls == [7200, 7200]
    with pytest.raises(MessageIdReuseConflictError):
        await handler.handle(chat.model_copy(update={"question": "different"}), IDENTITY)
    snapshot = await store.load(prepared.plan.permission_requirement, {
        "conversation_id": chat.conversation_id,
        "tenant_id": IDENTITY.tenant_id,
        "user_id": IDENTITY.user_id,
        "application_id": chat.application_id,
    })
    message = snapshot.message(chat.message_id)
    restored = restore_exact_result(
        ScopedArtifact.model_validate(message["result"]), prepared.plan.permission_requirement
    )
    assert restored["data"][0][restored["columns"][0]] == Decimal("9007199254740993.0100")
    state = ConversationState.model_validate(snapshot.state.payload)
    assert state.tasks[prepared.plan.task_id].last_dataset_id is not None
    assert len(state.execution_attempts) == 1


@pytest.mark.asyncio
async def test_persisted_handler_records_failure_without_dataset_and_reuses_terminal_receipt(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    redis = FakeRedis()
    store = RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2:isolated:round5-11:round511failure",
        run_id="round511failure",
        ttl_seconds=7200,
    )
    calls = 0

    async def planner(chat, identity, state, plans):
        return PersistedScalarPlan(state_artifact, plan_artifact, prepared)

    async def transport(execution_request):
        nonlocal calls
        calls += 1
        raise TimeoutError("unknown execution outcome")

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
    )
    chat = request()
    response = await handler.handle(chat, IDENTITY)
    repeated = await handler.handle(chat, IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert response.error_code == "EXECUTION_TIMEOUT_OUTCOME_UNKNOWN"
    assert repeated == response and calls == 1
    snapshot = await store.load(prepared.plan.permission_requirement, {
        "conversation_id": chat.conversation_id,
        "tenant_id": IDENTITY.tenant_id,
        "user_id": IDENTITY.user_id,
        "application_id": chat.application_id,
    })
    assert snapshot.message(chat.message_id)["status"] == "FAILED"
    assert snapshot.message(chat.message_id)["result"] is None
    state = ConversationState.model_validate(snapshot.state.payload)
    assert len(state.execution_attempts) == 1
    assert next(iter(state.execution_attempts.values())).status == "FAILED"
    assert state.datasets == {}
    assert state.tasks[prepared.plan.task_id].last_dataset_id is None


@pytest.mark.asyncio
async def test_running_duplicate_does_not_start_a_second_transport(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    redis = FakeRedis()
    store = RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2:isolated:round5-11:round511running",
        run_id="round511running",
        ttl_seconds=7200,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def planner(chat, identity, state, plans):
        return PersistedScalarPlan(state_artifact, plan_artifact, prepared)

    async def transport(execution_request):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        column = prepared.lowering.output_bindings[0].result_column_name
        return {
            "request_fingerprint": execution_request.fingerprint,
            "prepared_fingerprint": execution_request.prepared_fingerprint,
            "context_fingerprint": execution_request.context.fingerprint(),
            "data_source_id": execution_request.data_source_id,
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
                    ["consistent_snapshot", "read_only_transaction", "column_contract_valid",
                     "row_contract_valid", "row_count_reconciled", "statement_timeout_enforced"],
                    True,
                ),
            },
        }

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
    )
    chat = request()
    first = asyncio.create_task(handler.handle(chat, IDENTITY))
    await entered.wait()
    duplicate = await handler.handle(chat, IDENTITY)
    assert duplicate.status == "SAFE_FALLBACK"
    assert duplicate.error_code == "EXECUTION_OUTCOME_PENDING_REVIEW"
    assert calls == 1
    release.set()
    assert (await first).status == "COMPLETED"
    assert calls == 1


@pytest.mark.asyncio
async def test_persisted_store_requires_explicit_isolated_namespace():
    redis = FakeRedis()
    with pytest.raises(ValueError, match="ISOLATED_REDIS_NAMESPACE_REQUIRED"):
        RedisScalarSessionStore(redis, prefix="youo:data-analysis:v2", run_id="round511test", ttl_seconds=7200)


def test_original_chat_route_can_use_explicit_isolated_handler_without_changing_default(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    redis = FakeRedis()
    store = RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2:isolated:round5-11:round511route",
        run_id="round511route",
        ttl_seconds=7200,
    )

    async def planner(chat, identity, state, plans):
        return PersistedScalarPlan(state_artifact, plan_artifact, prepared)

    async def transport(execution_request):
        column = prepared.lowering.output_bindings[0].result_column_name
        return {
            "request_fingerprint": execution_request.fingerprint,
            "prepared_fingerprint": execution_request.prepared_fingerprint,
            "context_fingerprint": execution_request.context.fingerprint(),
            "data_source_id": execution_request.data_source_id,
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
                    ["consistent_snapshot", "read_only_transaction", "column_contract_valid",
                     "row_contract_valid", "row_count_reconciled", "statement_timeout_enforced"],
                    True,
                ),
            },
        }

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
    )
    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        business_question_collection_enabled=False,
        trusted_backend_token="test-token",
    )
    app = create_app(settings, isolated_chat_handler=handler)
    with TestClient(app, headers={
        "Authorization": "Bearer test-token",
        "X-Tenant-Id": "tenant",
        "X-User-Id": "user",
    }) as client:
        response = client.post("/agent_chat", json=request().model_dump(mode="json"))
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"
    assert response.json()["intent_source"] == "V2_TYPED_SEMANTIC_RUNTIME"
