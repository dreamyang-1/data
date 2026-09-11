from __future__ import annotations

import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.domain.models import ChatRequest
from app.main import create_app
from app.semantic_v2 import models as m
from app.semantic_v2.completed_question import build_completed_question_display
from app.semantic_v2.enums import CatalogType, SemanticRole
from app.semantic_v2.persisted_scalar_api import (
    PersistedScalarApiHandler,
    PersistedScalarPlan,
)
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from app.semantic_v2.state_machine import ConversationState, TaskVersion
from test_v2_authorized_catalog_bridge import IDENTITY, request
from test_v2_asl2_lowering import provider
from test_v2_limited_scalar_deployment import (
    DeploymentRedis,
    deployment_store,
    valid_transport,
)
from test_v2_persisted_scalar_api import NOW, planned_artifacts, prepared_for


HEADERS = {
    "Authorization": "Bearer test-token",
    "X-Tenant-Id": IDENTITY.tenant_id,
    "X-User-Id": IDENTITY.user_id,
}


def _settings(*, timeout: float = 5) -> Settings:
    return Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        business_question_collection_enabled=True,
        trusted_backend_token="test-token",
        request_timeout_seconds=timeout,
    )


def _parse_sse(text: str) -> list[dict]:
    return [
        json.loads(block.removeprefix("data: "))
        for block in text.strip().split("\n\n")
    ]


def _display_for(prepared, state_artifact, plan_artifact, relation="NEW_TASK"):
    state = ConversationState.model_validate(state_artifact.payload)
    plan = AuthorizedLogicalPlan.model_validate(plan_artifact.payload)
    return build_completed_question_display(
        message_id="message",
        plan=plan,
        previous_state=None,
        next_state=state,
        context_trace={"FINAL_RELATION": relation},
    )


def _handler(provider, redis, transport, *, planner_override=None):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    display = _display_for(prepared, state_artifact, plan_artifact)

    async def planner(chat, identity, restored, plans):
        if planner_override is not None:
            return await planner_override(chat, identity, restored, plans)
        return PersistedScalarPlan(
            state_artifact, plan_artifact, prepared, display
        )

    handler = PersistedScalarApiHandler(
        store=deployment_store(redis),
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
        running_review_seconds=60,
        cancellation_cleanup_seconds=1,
    )
    return handler, prepared, display


def _state_with_semantics(state, plan, semantics, *, version=2, clear_barriers=()):
    task = state.tasks[plan.task_id]
    next_version = TaskVersion(
        version=version,
        status=task.status,
        plan_id=plan.plan_id,
        semantics=semantics,
        current_turn_ref="message-2",
        current_turn_digest="turn-2",
        created_at=NOW,
    )
    next_task = task.model_copy(update={
        "active_version": version,
        "versions": [*task.versions, next_version],
        "clear_barriers": list(clear_barriers),
    })
    return state.model_copy(update={
        "state_version": state.state_version + 1,
        "tasks": {**state.tasks, plan.task_id: next_task},
        "recent_turn_ids": [*state.recent_turn_ids, "message-2"],
    })


def _plan_with_semantics(plan, semantics, *, version=2):
    payload = plan.payload.model_copy(update={
        "measures": semantics.metrics,
        "group_by": semantics.dimensions,
        "filters": semantics.filter_expression,
        "time": semantics.time_spec,
        "projection_spec": semantics.projection_spec,
    })
    return plan.model_copy(update={"task_version": version, "payload": payload})


def test_completed_question_uses_final_state_for_add_remove_clear_time_and_boolean_filters(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    previous = ConversationState.model_validate(state_artifact.payload)
    plan = AuthorizedLogicalPlan.model_validate(plan_artifact.payload)
    original = previous.tasks[plan.task_id].versions[0].semantics
    first_metric = original.metrics[0]
    second_metric = first_metric.model_copy(update={
        "canonical_id": first_metric.canonical_id + ":quantity",
        "canonical_code": first_metric.canonical_code + "_quantity",
        "display_name": "销售总数量",
    })
    province = first_metric.model_copy(update={
        "catalog_type": CatalogType.ATTRIBUTE,
        "semantic_role": SemanticRole.FILTER_FIELD,
        "canonical_id": first_metric.canonical_id + ":province",
        "canonical_code": "province_name",
        "display_name": "省份",
    })
    filter_expression = m.BooleanFilterGroup(
        operator="AND",
        children=[
            m.Predicate(
                field_ref=province,
                operator="IN",
                value=m.ListValue(values=[
                    m.StringValue(value="江苏省"),
                    m.StringValue(value="浙江省"),
                ]),
                source="CURRENT_EXPLICIT",
                scope="CURRENT_TASK",
            ),
            m.BooleanFilterGroup(
                operator="NOT",
                children=[m.Predicate(
                    field_ref=province,
                    operator="EQ",
                    value=m.StringValue(value="上海市"),
                    source="CURRENT_EXPLICIT",
                    scope="CURRENT_TASK",
                )],
            ),
        ],
    )
    time_anchor = first_metric.model_copy(update={
        "catalog_type": CatalogType.ATTRIBUTE,
        "semantic_role": SemanticRole.TIME_FIELD,
        "canonical_id": first_metric.canonical_id + ":created-date",
        "canonical_code": "created_date",
        "display_name": "订单日期",
    })
    time_spec = m.TimeSpec(
        anchor=time_anchor,
        range=m.TimeRange(
            start=datetime.fromisoformat("2026-01-01T00:00:00+08:00"),
            end_exclusive=datetime.fromisoformat("2027-01-01T00:00:00+08:00"),
        ),
        grain="NONE",
        timezone="Asia/Shanghai",
        source="USER_EXPLICIT",
        as_of=NOW,
    )
    added = original.model_copy(update={
        "metrics": [first_metric, second_metric],
        "filter_expression": filter_expression,
        "time_spec": time_spec,
    })
    added_state = _state_with_semantics(previous, plan, added)
    added_plan = _plan_with_semantics(plan, added)
    display = build_completed_question_display(
        message_id="message-2",
        plan=added_plan,
        previous_state=previous,
        next_state=added_state,
        context_trace={"FINAL_RELATION": "MODIFY"},
    )
    assert "新增指标销售总数量" in display.understanding
    assert "销售总数量" in display.completed_question
    assert "2026年" in display.completed_question
    assert "江苏省、浙江省" in display.completed_question
    assert "非（省份等于上海市）" in display.completed_question

    removed = added.model_copy(update={"metrics": [second_metric]})
    removed_state = _state_with_semantics(added_state, added_plan, removed, version=3)
    removed_plan = _plan_with_semantics(added_plan, removed, version=3)
    removed_display = build_completed_question_display(
        message_id="message-3", plan=removed_plan,
        previous_state=added_state, next_state=removed_state,
        context_trace={"FINAL_RELATION": "MODIFY"},
    )
    assert "移除指标" in removed_display.understanding
    assert first_metric.display_name not in removed_display.completed_question

    cleared = removed.model_copy(update={"filter_expression": None})
    cleared_state = _state_with_semantics(
        removed_state, removed_plan, cleared, version=4,
        clear_barriers=("filter_expression",),
    )
    cleared_plan = _plan_with_semantics(removed_plan, cleared, version=4)
    cleared_display = build_completed_question_display(
        message_id="message-4", plan=cleared_plan,
        previous_state=removed_state, next_state=cleared_state,
        context_trace={"FINAL_RELATION": "MODIFY"},
    )
    assert "清除原筛选条件" in cleared_display.understanding
    assert "江苏省" not in cleared_display.completed_question
    assert "不再应用已清除的筛选条件" in cleared_display.completed_question


def test_completed_question_rejects_subject_drift(provider):
    prepared = prepared_for(provider)
    state_artifact, plan_artifact = planned_artifacts(prepared)
    state = ConversationState.model_validate(state_artifact.payload)
    plan = AuthorizedLogicalPlan.model_validate(plan_artifact.payload)
    subject = plan.payload.measures[0].model_copy(update={
        "catalog_type": CatalogType.ENTITY,
        "semantic_role": SemanticRole.SUBJECT_ENTITY,
        "canonical_id": "entity:one",
        "canonical_code": "entity_one",
        "display_name": "实体一",
    })
    raw_state = state.model_dump(mode="json")
    task = raw_state["tasks"][plan.task_id]
    active = next(
        version for version in task["versions"]
        if version["version"] == task["active_version"]
    )
    active["semantics"]["subject"] = subject.model_dump(mode="json")
    state = ConversationState.model_validate(raw_state)
    changed_subject = subject.model_copy(
        update={"canonical_id": "entity:two", "canonical_code": "entity_two"}
    )
    changed_plan = plan.model_copy(
        update={"payload": plan.payload.model_copy(update={"subject": changed_subject})}
    )
    with pytest.raises(ValueError, match="PLAN_STATE_MISMATCH"):
        build_completed_question_display(
            message_id="message",
            plan=changed_plan,
            previous_state=None,
            next_state=state,
            context_trace={"FINAL_RELATION": "NEW_TASK"},
        )


def test_original_json_sse_and_replay_share_one_completed_question_without_reexecution(provider):
    redis = DeploymentRedis()
    calls = []
    prepared = prepared_for(provider)
    handler, prepared, display = _handler(
        provider, redis, valid_transport(prepared, calls)
    )
    handler.transport = valid_transport(prepared, calls)
    app = create_app(_settings(), isolated_chat_handler=handler)
    payload = request().model_dump(mode="json")
    with TestClient(app, headers=HEADERS) as client:
        first = client.post("/agent_chat", json=payload)
        replay = client.post("/agent_chat/stream", json=payload)

    assert first.status_code == 200
    body = first.json()
    summary = body["analysis_process"][0]["summary"]
    assert summary == display.public_message
    assert "本轮理解：" in summary and "补全后的完整问题：" in summary
    events = _parse_sse(replay.text)
    complete = next(item for item in events if item["type"] == "complete")
    assert complete["analysis_process"][0]["summary"] == summary
    thinking = "".join(
        item.get("content", "") for item in events
        if item["type"] == "message_chunk" and item.get("step") != "output"
    )
    assert summary in thinking.replace("  \n", "\n")
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["/agent_chat", "/agent_chat/stream"])
def test_outer_api_timeout_after_submission_records_unknown_and_replay_never_resubmits(provider, path):
    redis = DeploymentRedis()
    entered = Event()
    release = Event()
    calls = 0
    prepared = prepared_for(provider)

    async def blocked_transport(execution_request):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.to_thread(release.wait, 2)
        return await valid_transport(prepared, [])(execution_request)

    handler, prepared, _display = _handler(provider, redis, blocked_transport)
    app = create_app(_settings(timeout=0.05), isolated_chat_handler=handler)
    payload = request().model_dump(mode="json")
    try:
        with TestClient(app, headers=HEADERS, raise_server_exceptions=False) as client:
            response = client.post(path, json=payload)
            assert entered.wait(1)
            if path.endswith("stream"):
                assert response.status_code == 200
                assert any(item["type"] == "error" for item in _parse_sse(response.text))
            else:
                assert response.status_code == 504
            state_key = next(key for key in redis.values if ":conversation:" in key)
            record = json.loads(redis.values[state_key])["messages"]["message"]
            assert record["status"] == "UNKNOWN"
            assert record["last_confirmed_execution_stage"] == "SUBMISSION_ATTEMPTED"
            repeated = client.post("/agent_chat", json=payload)
            assert repeated.status_code == 200
            assert repeated.json()["error_code"] == "EXECUTION_OUTCOME_REQUIRES_OPERATOR_REVIEW"
            assert calls == 1
    finally:
        release.set()


def test_outer_timeout_before_reservation_leaves_no_execution_record(provider):
    redis = DeploymentRedis()
    release = Event()
    prepared = prepared_for(provider)

    async def blocked_planner(chat, identity, restored, plans):
        await asyncio.to_thread(release.wait, 2)
        raise AssertionError("cancelled planner must not reach execution")

    async def forbidden_transport(_request):
        raise AssertionError("pre-reservation cancellation must not execute")

    handler, _prepared, _display = _handler(
        provider, redis, forbidden_transport, planner_override=blocked_planner
    )
    app = create_app(_settings(timeout=0.05), isolated_chat_handler=handler)
    try:
        with TestClient(app, headers=HEADERS, raise_server_exceptions=False) as client:
            response = client.post(
                "/agent_chat", json=request().model_dump(mode="json")
            )
        assert response.status_code == 504
        assert not any(":conversation:" in key for key in redis.values)
    finally:
        release.set()


def test_v2_rejects_enabled_legacy_features_before_collector_or_file_import(provider):
    redis = DeploymentRedis()
    prepared = prepared_for(provider)
    calls = []
    handler, prepared, _display = _handler(
        provider, redis, valid_transport(prepared, calls)
    )
    collector_calls = []
    importer_calls = []

    class Collector:
        def record(self, **kwargs):
            collector_calls.append(kwargs)

    class Importer:
        async def import_object(self, **kwargs):
            importer_calls.append(kwargs)
            raise AssertionError("V1 importer must not run")

    app = create_app(_settings(), isolated_chat_handler=handler)
    payload = request().model_dump(mode="json")
    payload["temp_file_paths"] = ["source.xlsx"]
    with TestClient(app, headers=HEADERS) as client:
        object.__setattr__(app.state.container, "business_question_collector", Collector())
        object.__setattr__(app.state.container, "file_importer", Importer())
        response = client.post("/agent_chat", json=payload)
    assert response.status_code == 200
    assert response.json()["error_code"] == "EXECUTION_REQUEST_FEATURE_UNSUPPORTED"
    assert collector_calls == importer_calls == calls == []


def test_empty_platform_defaults_are_not_misclassified_as_enabled_features(provider):
    redis = DeploymentRedis()
    calls = []
    prepared = prepared_for(provider)
    handler, prepared, _display = _handler(
        provider, redis, valid_transport(prepared, calls)
    )
    handler.transport = valid_transport(prepared, calls)
    chat = request().model_copy(update={"knowledge_base_names": []})
    response = asyncio.run(handler.handle(chat, IDENTITY))
    assert response.status == "COMPLETED" and len(calls) == 1


def test_isolated_v2_lifespan_does_not_start_v1_dataset_cleaner(monkeypatch):
    calls = []

    class Cleaner:
        def start(self):
            calls.append("start")

        async def stop(self):
            calls.append("stop")

    class Handler:
        async def aclose(self):
            calls.append("handler-close")

    container = SimpleNamespace(
        settings=_settings(),
        dataset_cleaner=Cleaner(),
        sessions=SimpleNamespace(redis=None),
    )
    monkeypatch.setattr("app.main.build_container", lambda _settings: container)
    app = create_app(_settings(), isolated_chat_handler=Handler())
    with TestClient(app):
        pass
    assert calls == ["handler-close"]


@pytest.mark.parametrize(
    ("failed_call", "last_stage"),
    [(4, "RESULT_RETURNED"), (5, "RESULT_VALIDATED")],
)
@pytest.mark.asyncio
async def test_publish_failure_after_return_or_validation_is_unknown(provider, failed_call, last_stage):
    redis = DeploymentRedis()
    prepared = prepared_for(provider)
    calls = []
    handler, prepared, _display = _handler(
        provider, redis, valid_transport(prepared, calls)
    )
    handler.transport = valid_transport(prepared, calls)
    redis.fail_call = failed_call
    chat = request()
    response = await handler.handle(chat, IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert response.error_code == "EXECUTION_RECEIPT_STATE_CONFLICT"
    snapshot = await handler.store.load(
        prepared.plan.permission_requirement, handler._identity(chat, IDENTITY)
    )
    record = snapshot.message(chat.message_id)
    assert record["status"] == "UNKNOWN"
    assert record["last_confirmed_execution_stage"] == last_stage
    assert len(calls) == 1
