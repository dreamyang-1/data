from decimal import Decimal

from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.domain.models import DataQueryResult, Dataset
from app.semantic_v2.canonical_execution_bridge import (
    build_canonical_analysis_request,
    validate_canonical_query_result,
)
from app.semantic_v2.persisted_scalar_api import PersistedScalarPlan
from app.semantic_v2.state_machine import ConversationState
from test_v2_authorized_catalog_bridge import IDENTITY, request
from test_v2_asl2_lowering import provider
from test_v2_context_completed_question_trial import (
    HEADERS,
    _display_for,
    _handler,
    _parse_sse,
    _settings,
)
from test_v2_persisted_scalar_api import NOW, planned_artifacts, prepared_for
from test_v2_limited_scalar_deployment import DeploymentRedis


def _canonical(prepared):
    state_artifact, plan_artifact = planned_artifacts(prepared)
    display = _display_for(prepared, state_artifact, plan_artifact)
    return build_canonical_analysis_request(
        chat=request(),
        identity=IDENTITY,
        plan=prepared.plan,
        display=display,
    ), display


def _outcome(prepared, *, value=Decimal("12.30"), quality="PASS"):
    metric = prepared.plan.payload.measures[0]
    alias = metric.display_name
    return DataQueryResult(
        asl={
            "subject": prepared.lowering.asl["subject"],
            "metrics": [{"name": metric.canonical_code, "alias": alias}],
        },
        sql=f"SELECT metric AS `{alias}`",
        data_source_id=str(prepared.sql_receipt["data_source_id"]),
        dataset=Dataset(
            columns=[alias],
            rows=[{alias: value}],
            snapshot_id="original-query-chain-snapshot",
            data_as_of=NOW,
            quality_status=quality,
            row_count=1,
            total_row_count=1,
            truncated=False,
        ),
    )


def test_final_v2_plan_maps_to_original_canonical_request_without_legacy_merge(provider):
    prepared = prepared_for(provider)
    canonical, display = _canonical(prepared)
    metric = prepared.plan.payload.measures[0]

    assert canonical.original_question == request().question
    assert canonical.rewritten_question == display.completed_question
    assert canonical.authorized_semantic_scope == request().authorized_semantic_scope
    assert canonical.turn_relation.value == "STANDALONE_NEW_TOPIC"
    assert canonical.intent_source == "V2_FINAL_TASK_STATE"
    assert canonical.missing_slots == []
    assert canonical.filters == []
    assert "V2_QUERY_SHAPE=SCALAR_AGGREGATE" in canonical.assumptions
    assert [(item.metric_id, item.canonical_name) for item in canonical.metrics] == [
        (f"81:{metric.canonical_code}", metric.display_name)
    ]
    assert "V2_FINAL_TASK_STATE_AUTHORITY" in canonical.assumptions
    assert any(
        item == f"COMPLETED_QUESTION_DIGEST={display.display_digest}"
        for item in canonical.assumptions
    )


@pytest.mark.parametrize(
    ("v2_relation", "legacy_relation"),
    [
        ("ADD", "CURRENT_TOPIC_MODIFICATION"),
        ("REPLACE", "CURRENT_TOPIC_MODIFICATION"),
        ("REMOVE", "CURRENT_TOPIC_MODIFICATION"),
        ("CLEAR", "CURRENT_TOPIC_MODIFICATION"),
        ("CORRECT", "CURRENT_TOPIC_MODIFICATION"),
        ("CONTINUE", "CURRENT_TOPIC_FOLLOWUP"),
        ("DRILL_DOWN", "CURRENT_TOPIC_FOLLOWUP"),
    ],
)
def test_v2_edit_and_followup_relations_map_without_reclassifying(
    provider, v2_relation, legacy_relation
):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    display = _display_for(prepared, state_artifact, plan_artifact).model_copy(
        update={"relation": v2_relation}
    )
    canonical = build_canonical_analysis_request(
        chat=request(), identity=IDENTITY, plan=prepared.plan, display=display
    )

    assert canonical.turn_relation.value == legacy_relation
    assert canonical.rewritten_question == display.completed_question


def test_original_query_result_is_rebound_to_v2_result_contract(provider):
    prepared = prepared_for(provider)
    canonical, _ = _canonical(prepared)
    result, proof, asl_digest, sql_digest = validate_canonical_query_result(
        prepared=prepared,
        request=canonical,
        outcome=_outcome(prepared),
    )

    assert result["data"][0][result["columns"][0]] == Decimal("12.30")
    assert proof.status.value == "PASS"
    assert len(asl_digest) == len(sql_digest) == 64


def test_original_query_result_mismatch_fails_closed(provider):
    prepared = prepared_for(provider)
    canonical, _ = _canonical(prepared)
    wrong = _outcome(prepared).model_copy(update={
        "asl": {
            "subject": prepared.lowering.asl["subject"],
            "metrics": [{"name": "different_metric", "alias": "value"}],
        }
    })
    with pytest.raises(ValueError, match="EXECUTION_RESULT_ASL_METRIC_MISMATCH"):
        validate_canonical_query_result(
            prepared=prepared,
            request=canonical,
            outcome=wrong,
        )


def test_duplicate_asl_metric_fails_closed(provider):
    prepared = prepared_for(provider)
    canonical, _ = _canonical(prepared)
    outcome = _outcome(prepared)
    duplicate = outcome.model_copy(update={
        "asl": {
            "subject": prepared.lowering.asl["subject"],
            "metrics": [*outcome.asl["metrics"], *outcome.asl["metrics"]],
        }
    })
    with pytest.raises(ValueError, match="EXECUTION_RESULT_ASL_METRIC_MISMATCH"):
        validate_canonical_query_result(
            prepared=prepared,
            request=canonical,
            outcome=duplicate,
        )


@pytest.mark.asyncio
async def test_persisted_handler_uses_original_query_adapter_and_keeps_same_display(provider):
    redis = DeploymentRedis()
    direct_transport_calls = 0
    query_calls = []
    prepared = prepared_for(provider)
    canonical, display = _canonical(prepared)
    state_artifact, plan_artifact = planned_artifacts(prepared)

    async def forbidden_transport(_request):
        nonlocal direct_transport_calls
        direct_transport_calls += 1
        raise AssertionError("typed direct transport must not run")

    handler, _, _ = _handler(provider, redis, forbidden_transport)

    async def planner(chat, identity, restored, plans):
        return PersistedScalarPlan(
            state_artifact,
            plan_artifact,
            prepared,
            display,
            canonical,
        )

    async def query_adapter(actual, identity):
        query_calls.append((actual, identity))
        return _outcome(prepared)

    handler.planner = planner
    handler.canonical_query = query_adapter
    response = await handler.handle(request(), IDENTITY)

    assert response.status == "COMPLETED"
    assert response.analysis_process[0].summary == display.public_message
    assert len(query_calls) == 1 and query_calls[0][0] == canonical
    assert direct_transport_calls == 0
    snapshot = await handler.store.load(
        prepared.plan.permission_requirement,
        handler._identity(request(), IDENTITY),
    )
    state = ConversationState.model_validate(snapshot.state.payload)
    attempt = next(iter(state.execution_attempts.values()))
    assert attempt.proof_chain.asl.status.value == "PASS"
    assert attempt.proof_chain.sql_plan.status.value == "PASS"
    assert attempt.asl_digest and attempt.sql_digest


def test_original_json_and_sse_replay_keep_one_canonical_request(provider):
    redis = DeploymentRedis()
    prepared = prepared_for(provider)
    canonical, display = _canonical(prepared)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    planner_calls = []
    query_calls = []

    async def forbidden_transport(_request):
        raise AssertionError("canonical query path must use the original adapter")

    handler, _, _ = _handler(provider, redis, forbidden_transport)

    async def planner(chat, identity, restored, plans):
        planner_calls.append(chat.message_id)
        return PersistedScalarPlan(
            state_artifact,
            plan_artifact,
            prepared,
            display,
            canonical,
        )

    async def query_adapter(actual, identity):
        query_calls.append(actual)
        return _outcome(prepared)

    handler.planner = planner
    handler.canonical_query = query_adapter
    app = create_app(_settings(), isolated_chat_handler=handler)
    payload = request().model_dump(mode="json")
    with TestClient(app, headers=HEADERS) as client:
        json_response = client.post("/agent_chat", json=payload)
        sse_response = client.post("/agent_chat/stream", json=payload)

    assert json_response.status_code == 200
    complete = next(
        event for event in _parse_sse(sse_response.text)
        if event["type"] == "complete"
    )
    assert complete["analysis_process"] == json_response.json()["analysis_process"]
    assert planner_calls == [request().message_id]
    assert query_calls == [canonical]
