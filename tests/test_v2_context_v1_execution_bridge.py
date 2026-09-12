import json
from datetime import datetime
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import AgentResponse, PrimaryIntent
from app.main import create_app
from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
from app.semantic_v2.context_proposal import ContextProposalFailure
from app.semantic_v2.context_v1_execution import (
    ContextV1ExternalDependencies,
    ResolvedContextTurn,
    V2ContextV1ExecutionBridge,
    build_context_v1_execution_handler,
    validate_context_v1_settings,
)
from app.semantic_v2.persisted_scalar_api import RedisScalarSessionStore
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.state_machine import ConversationState
from test_v2_authorized_catalog_bridge import IDENTITY, provider, request
from test_v2_limited_scalar_deployment import DeploymentRedis, candidate_settings
from test_v2_persisted_scalar_api import NOW
from app.semantic_v2.catalog_bridge import ScopedPlanSession


HEADERS = {
    "Authorization": "Bearer context-v1-test-token",
    "X-Tenant-Id": IDENTITY.tenant_id,
    "X-User-Id": IDENTITY.user_id,
}


def response(chat, answer="V1 result"):
    return AgentResponse(
        request_id=uuid4(),
        conversation_id=chat.conversation_id,
        status="COMPLETED",
        intent=PrimaryIntent.METRIC_QUERY,
        answer=answer,
        semantic_model_id=chat.semantic_model_id,
        requested_business_domain_ids=list(chat.business_domain_ids),
        business_domain_selection_mode="EXPLICIT",
    )


def state_artifact(context, chat, version):
    state = ConversationState(
        state_version=version,
        tenant_id=IDENTITY.tenant_id,
        user_id=IDENTITY.user_id,
        application_id=chat.application_id,
        conversation_id=chat.conversation_id,
    )
    payload = state.model_dump(mode="json")
    return ScopedArtifact(
        kind="CONVERSATION",
        context=context,
        payload=payload,
        payload_digest=contract_digest(payload),
    )


def make_handler(provider, planner, v1, redis=None):
    scoped = ScopedPlanSession(request(), IDENTITY, provider[0])
    context = scoped.context
    scoped.accept_catalog()
    redis = redis or DeploymentRedis()
    store = RedisScalarSessionStore(
        redis,
        prefix="youo:data-analysis:v2-context-v1-execution:test-demo",
        deployment_id="test-demo",
        ttl_seconds=3600,
        idempotency_ttl_seconds=7200,
    )
    handler = V2ContextV1ExecutionBridge(
        store=store,
        context_resolver=lambda chat, identity: context,
        context_planner=planner,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
        readiness_probe=lambda: _ready(),
    )
    return handler, context, redis


async def _ready():
    return {
        "v2_context_v1_redis": True,
        "v2_context_v1_catalog_pin": True,
        "v1_execution_bridge": True,
        "v2_execution_transport_disabled": True,
    }


def test_new_runtime_mode_is_opt_in_and_builder_has_no_v2_execution_transport(provider):
    assert Settings(_env_file=None).model_copy(update={"runtime_mode": "V1"}).runtime_mode == "V1"
    settings = candidate_settings(provider).model_copy(update={
        "runtime_mode": "V2_CONTEXT_V1_EXECUTION",
    })
    receipt = validate_context_v1_settings(settings)

    class NoCallModel:
        async def complete(self, **kwargs):
            raise AssertionError("startup must not call the model")

    class Workflow:
        async def ainvoke(self, value):
            raise AssertionError("startup must not call V1")

    handler = build_context_v1_execution_handler(
        settings,
        v1_workflow=Workflow(),
        external=ContextV1ExternalDependencies(
            publication=provider[0],
            model=NoCallModel(),
            redis=DeploymentRedis(),
        ),
    )

    assert receipt["runtime_mode"] == "V2_CONTEXT_V1_EXECUTION"
    assert handler.startup_receipt["v2_limited_scalar_used_for_execution"] is False
    assert ":v2-context-v1-execution:" in handler.store.prefix


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "completed"),
    [
        ("查询四川省含税销售总额", "查询四川省含税销售总额。"),
        ("那订单笔数呢", "查询四川省某产品订单笔数。"),
        ("按城市呢", "查询四川省某产品含税销售总额，按城市分组。"),
        ("查询北京医院数量", "查询北京医院数量。"),
        ("不限地区", "查询某产品含税销售总额，不再应用地区筛选。"),
    ],
)
async def test_resolved_context_passes_only_completed_question_to_v1(
    provider, question, completed
):
    chat = request(question=question, message_id="message-" + contract_digest(question)[:8])
    calls = []

    async def planner(current, identity, state, plans, pending):
        assert current.history == []
        return ResolvedContextTurn(
            completed_question=completed,
            next_state=state_artifact(context, chat, 1),
            plan_state=None,
        )

    async def v1(current, identity):
        calls.append(current)
        return response(current)

    handler, context, _redis = make_handler(provider, planner, v1)
    result = await handler.handle(chat, IDENTITY)

    assert result.answer == "V1 result"
    assert len(calls) == 1
    assert calls[0].question == completed
    assert calls[0].history == []
    assert calls[0]._completed_question_execution is True
    assert calls[0].semantic_model_id == chat.semantic_model_id
    assert calls[0].business_domain_ids == chat.business_domain_ids
    assert calls[0].model_dump(
        mode="json", exclude={"question", "history"}
    ) == chat.model_dump(mode="json", exclude={"question", "history"})


@pytest.mark.asyncio
async def test_complex_standalone_new_task_fallback_reuses_v1_without_v2_plan(provider):
    chat = request(
        message_id="complex",
        question="查询空心纤维血液透析器产品合作的经销商名单",
    )
    calls = []

    async def planner(current, identity, state, plans, pending):
        return ResolvedContextTurn(
            completed_question=current.question,
            next_state=None,
            plan_state=None,
            bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
        )

    async def v1(current, identity):
        calls.append(current)
        return response(current)

    handler, _context, redis = make_handler(provider, planner, v1)
    result = await handler.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert [call.question for call in calls] == [chat.question]
    envelope = json.loads(next(iter(redis.values.values())))
    assert envelope["state"] is None
    assert envelope["messages"]["complex"]["bridge_route"] == (
        "V1_EXECUTION_FALLBACK_NEW_TASK"
    )


@pytest.mark.asyncio
async def test_user_ambiguity_never_calls_v1(provider):
    chat = request(message_id="ambiguous", question="刚才那个呢")
    v1_calls = []

    async def planner(*args):
        raise ContextProposalFailure({
            "FINAL_STATUS": "AMBIGUOUS",
            "FINAL_RELATION": None,
            "FINAL_TARGET": None,
        })

    async def v1(*args):
        v1_calls.append(args)
        raise AssertionError("ambiguity must not reach V1")

    handler, _context, _redis = make_handler(provider, planner, v1)
    result = await handler.handle(chat, IDENTITY)

    assert result.status == "NEEDS_CLARIFICATION"
    assert result.error_code == "V2_CONTEXT_AMBIGUOUS"
    assert not v1_calls


@pytest.mark.asyncio
async def test_unresolved_short_followup_never_falls_back_to_v1(provider):
    chat = request(message_id="unresolved", question="那这个呢")
    v1_calls = []

    async def planner(*args):
        raise RecognitionFailure("V2_TURN_REFERENCE_UNRESOLVED")

    async def v1(*args):
        v1_calls.append(args)
        raise AssertionError("unresolved follow-up must not reach V1")

    handler, _context, _redis = make_handler(provider, planner, v1)
    result = await handler.handle(chat, IDENTITY)

    assert result.status == "SAFE_FALLBACK"
    assert result.error_code == "V2_TURN_REFERENCE_UNRESOLVED"
    assert not v1_calls


@pytest.mark.asyncio
async def test_v1_exception_is_preserved_and_not_rewritten_as_ambiguity(provider):
    chat = request(message_id="v1-error", question="查询四川省销售额")

    async def planner(current, identity, state, plans, pending):
        return ResolvedContextTurn(
            completed_question=current.question,
            next_state=state_artifact(context, chat, 1),
            plan_state=None,
        )

    class OriginalV1Error(RuntimeError):
        pass

    async def v1(*args):
        raise OriginalV1Error("original-v1-error")

    handler, context, _redis = make_handler(provider, planner, v1)
    with pytest.raises(OriginalV1Error, match="original-v1-error"):
        await handler.handle(chat, IDENTITY)


def test_json_and_sse_share_one_bridge_and_one_completed_question(provider):
    redis = DeploymentRedis()
    chat = request(
        conversation_id="api-conversation",
        message_id="api-message",
        question="换今年",
    )
    planner_calls = []
    v1_calls = []

    async def planner(current, identity, state, plans, pending):
        planner_calls.append(current.question)
        return ResolvedContextTurn(
            completed_question="查询2026年江苏省订单笔数。",
            next_state=state_artifact(context, chat, 1),
            plan_state=None,
        )

    async def v1(current, identity):
        v1_calls.append(current.question)
        return response(current, answer="done")

    handler, context, _redis = make_handler(provider, planner, v1, redis)
    settings = Settings(_env_file=None).model_copy(update={
        "env": "test",
        "adapter_mode": "mock",
        "session_store_mode": "memory",
        "business_question_collection_enabled": False,
        "long_term_memory_mode": "disabled",
        "langfuse_enabled": False,
        "trusted_backend_token": SecretStr("context-v1-test-token"),
    })
    app = create_app(settings, isolated_chat_handler=handler)
    payload = chat.model_dump(mode="json")
    with TestClient(app, headers=HEADERS) as client:
        first = client.post("/agent_chat", json=payload)
        replay = client.post("/agent_chat/stream", json=payload)

    assert first.status_code == 200, first.text
    assert first.json()["answer"] == "done"
    events = [
        json.loads(line.removeprefix("data: "))
        for line in replay.text.splitlines()
        if line.startswith("data: ")
    ]
    assert next(event for event in events if event["type"] == "complete")["answer"] == "done"
    assert planner_calls == ["换今年"]
    assert v1_calls == ["查询2026年江苏省订单笔数。"]
