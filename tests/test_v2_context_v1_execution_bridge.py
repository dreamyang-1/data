import json
from datetime import datetime
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import AgentResponse, EvidenceItem, PrimaryIntent
from app.main import create_app
from app.semantic_v2.authorized_contract import ScopedArtifact, contract_digest
from app.semantic_v2.context_proposal import ContextProposalFailure
from app.semantic_v2.context_v1_execution import (
    ContextV1ExternalDependencies,
    ExecutionAnchorUpdate,
    ResolvedContextTurn,
    V2ContextV1ExecutionBridge,
    _resolve_context_v1_request_scope,
    _standalone_execution_display,
    build_context_v1_execution_handler,
    validate_context_v1_settings,
)
from app.semantic_v2.enums import DialogueAct, SemanticRole
from app.semantic_v2.models import Mention
from app.semantic_v2.pipeline import CurrentTurnSemanticParse, OperationMarker
from app.semantic_v2.completed_question import CompletedQuestionDisplay
from app.semantic_v2.persisted_scalar_api import RedisScalarSessionStore
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.state_machine import ConversationState
from test_v2_authorized_catalog_bridge import IDENTITY, provider, request
from test_catalog_publication import authority, publish, reseal, system
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


def executed_response(chat, answer="V1 result"):
    result = response(chat, answer)
    result.evidence = [EvidenceItem(
        evidence_id="query-result:" + chat.message_id,
        kind="QUERY_RESULT",
        source_ref="data-source:58",
        payload={"row_count": 1},
    )]
    return result


def time_parse(question, message_id, surface, operation="REPLACE", followup=True):
    start = question.index(surface)
    mention_id = "time:" + message_id
    return CurrentTurnSemanticParse(
        mentions=[Mention(
            mention_id=mention_id,
            surface=surface,
            normalized_surface=surface,
            start_char=start,
            end_char=start + len(surface),
            candidate_roles=[SemanticRole.TIME_RANGE],
            source_turn_id=message_id,
        )],
        dialogue_act_candidates=[
            DialogueAct.REPLACE if followup else DialogueAct.NEW_TASK
        ],
        operation_markers=[OperationMarker(
            mention_id=mention_id,
            operation_hint=operation,
            slot_name="time_spec",
        )],
        reference_signals=["ELLIPSIS"] if followup else [],
        followup_signals=["MODIFY"] if followup else [],
        temporal_expressions=[mention_id],
        explicit_slot_mentions={"time_spec": [mention_id]},
    )


def unresolved_with_parse(parsed):
    failure = ContextProposalFailure({
        "FINAL_STATUS": "UNRESOLVED",
        "FINAL_RELATION": None,
        "FINAL_TARGET": None,
    })
    failure.current_turn_parse = parsed
    return failure


def redis_envelope(redis):
    return next(
        value for value in (json.loads(raw) for raw in redis.values.values())
        if isinstance(value, dict) and "messages" in value
    )


@pytest.fixture
def context_scope_provider():
    service, store, registry, redis, _captures, overrides = system()
    model_wide = authority((), model=81)
    model_wide["documents"] = [
        document
        for document in model_wide["documents"]
        if document["business_domain"]["id"] == 205
    ]
    overrides[(81, tuple())] = reseal(model_wide)
    publish(service, model=81, domains=[205])
    publish(service, model=81, domains=[])
    return service, store, registry, redis, overrides


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


def test_new_runtime_mode_is_opt_in_and_builder_has_no_v2_execution_transport(
    context_scope_provider,
):
    assert Settings(_env_file=None).model_copy(update={"runtime_mode": "V1"}).runtime_mode == "V1"
    settings = candidate_settings(context_scope_provider).model_copy(update={
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
            publication=context_scope_provider[0],
            model=NoCallModel(),
            redis=DeploymentRedis(),
        ),
    )

    assert receipt["runtime_mode"] == "V2_CONTEXT_V1_EXECUTION"
    assert handler.startup_receipt["v2_limited_scalar_used_for_execution"] is False
    assert ":v2-context-v1-execution:" in handler.store.prefix


def _scope_contract_handler(context_scope_provider):
    settings = candidate_settings(context_scope_provider).model_copy(update={
        "runtime_mode": "V2_CONTEXT_V1_EXECUTION",
    })

    class NoCallModel:
        async def complete(self, **kwargs):
            raise AssertionError("scope validation must not call the model")

    class Workflow:
        async def ainvoke(self, value):
            raise AssertionError("scope validation must not call V1")

    handler = build_context_v1_execution_handler(
        settings,
        v1_workflow=Workflow(),
        external=ContextV1ExternalDependencies(
            publication=context_scope_provider[0],
            model=NoCallModel(),
            redis=DeploymentRedis(),
        ),
    )
    return settings, handler


def test_scope_contract_a_omitted_domains_are_model_wide_and_resolve_to_205(
    context_scope_provider,
):
    settings, handler = _scope_contract_handler(context_scope_provider)
    chat = request(domains=())

    resolved = _resolve_context_v1_request_scope(
        chat,
        {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": settings.limited_scalar_business_domain_ids,
        },
    )
    context = handler.context_resolver(chat, IDENTITY)

    assert resolved == {
        "semantic_model_id": 81,
        "requested_business_domain_ids": [],
        "resolved_business_domain_ids": [205],
        "selection_mode": "MODEL_WIDE",
    }
    assert context.authorized_scope.scope_mode == "MODEL_WIDE"
    assert context.authorized_scope.business_domain_ids == ()
    assert handler.startup_receipt["scope_contract"]["model_wide"] == {
        "selection_mode": "MODEL_WIDE",
        "requested_business_domain_ids": [],
        "resolved_business_domain_ids": [205],
    }


def test_scope_contract_b_explicit_205_remains_explicit(context_scope_provider):
    settings, handler = _scope_contract_handler(context_scope_provider)
    chat = request(domains=(205,))

    resolved = _resolve_context_v1_request_scope(
        chat,
        {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": settings.limited_scalar_business_domain_ids,
        },
    )
    context = handler.context_resolver(chat, IDENTITY)

    assert resolved["selection_mode"] == "EXPLICIT_DOMAINS"
    assert resolved["resolved_business_domain_ids"] == [205]
    assert context.authorized_scope.scope_mode == "EXPLICIT_DOMAINS"
    assert context.authorized_scope.business_domain_ids == (205,)


def test_scope_contract_c_explicit_foreign_domain_fails_closed(
    context_scope_provider,
):
    _settings, handler = _scope_contract_handler(context_scope_provider)

    with pytest.raises(ValueError, match="V2_CONTEXT_V1_SCOPE_PIN_MISMATCH"):
        handler.context_resolver(request(domains=(999,)), IDENTITY)


def test_scope_contract_d_department_is_not_a_business_domain(
    context_scope_provider,
):
    settings, handler = _scope_contract_handler(context_scope_provider)
    chat = request(domains=(), department="ORG_ADMIN")

    resolved = _resolve_context_v1_request_scope(
        chat,
        {
            "semantic_model_id": settings.limited_scalar_semantic_model_id,
            "business_domain_ids": settings.limited_scalar_business_domain_ids,
        },
    )
    context = handler.context_resolver(chat, IDENTITY)

    assert resolved["selection_mode"] == "MODEL_WIDE"
    assert resolved["resolved_business_domain_ids"] == [205]
    assert context.authorized_scope.scope_mode == "MODEL_WIDE"
    assert context.authorized_scope.business_domain_ids == ()


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

    handler, context, redis = make_handler(provider, planner, v1)
    result = await handler.handle(chat, IDENTITY)

    assert result.answer == "V1 result"
    assert result.analysis_process[0].title == "补全后的完整问题"
    assert completed in result.analysis_process[0].summary
    assert len(calls) == 1
    assert calls[0].question == completed
    assert calls[0].history == []
    assert calls[0]._completed_question_execution is True
    assert calls[0].semantic_model_id == chat.semantic_model_id
    assert calls[0].business_domain_ids == chat.business_domain_ids
    assert calls[0].model_dump(
        mode="json", exclude={"question", "history"}
    ) == chat.model_dump(mode="json", exclude={"question", "history"})
    assert redis_envelope(redis)["messages"][chat.message_id]["execution_anchor"] is None


def test_resolved_standalone_execution_keeps_original_question_for_v1():
    display = CompletedQuestionDisplay(
        message_id="message",
        task_id="task:message",
        task_version=1,
        plan_id="plan:message",
        semantic_fingerprint="semantic:message",
        relation="NEW_TASK",
        understanding="识别为独立新任务。",
        completed_question="查询筛选条件为省份名称等于江苏省的订单笔数。",
        display_digest="old-digest",
    )
    original = "查询去年江苏省订单笔数"

    updated = _standalone_execution_display(original, display)

    assert updated.completed_question == original
    assert updated.display_digest != display.display_digest
    assert original in updated.public_message


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
async def test_successful_fallback_builds_anchor_and_time_followup_preserves_opaque_base(
    provider,
):
    first = request(
        conversation_id="execution-anchor-a-b-f",
        message_id="anchor-first",
        question="查询空心纤维血液透析器产品合作的经销商名单。",
    )
    followup = request(
        conversation_id=first.conversation_id,
        message_id="anchor-followup",
        question="换今年",
    )
    calls = []

    async def planner(current, identity, state, plans, pending):
        if current.message_id == first.message_id:
            initial_parse = CurrentTurnSemanticParse(
                dialogue_act_candidates=[DialogueAct.NEW_TASK]
            )
            return ResolvedContextTurn(
                completed_question=current.question,
                next_state=state_artifact(context, current, 1),
                plan_state=None,
                bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
                anchor_update=ExecutionAnchorUpdate(
                    previous=None,
                    current_parse=initial_parse,
                    time_context=None,
                    resolved_business_domain_ids=(205,),
                ),
            )
        raise unresolved_with_parse(
            time_parse(current.question, current.message_id, "今年")
        )

    async def v1(current, identity):
        calls.append(current)
        return executed_response(current)

    handler, context, redis = make_handler(provider, planner, v1)
    first_result = await handler.handle(first, IDENTITY)
    first_envelope = redis_envelope(redis)
    first_anchor = first_envelope["messages"][first.message_id]["execution_anchor"]

    assert first_result.status == "COMPLETED"
    assert calls[0].question == first.question
    assert first_anchor["provenance"] == "V1_EXECUTION_ANCHOR"
    assert first_anchor["completeness"] == "PARTIAL"
    assert first_anchor["evidence_mode"] == "EXECUTION_BACKED"
    assert first_anchor["original_question"] == first.question
    assert first_anchor["actual_execution_question"] == first.question
    assert first_anchor["resolved_business_domain_ids"] == [205]
    assert first_envelope["state"]["payload"]["active_topic_id"] is None

    followup_result = await handler.handle(followup, IDENTITY)
    expected = "查询2026年空心纤维血液透析器产品合作的经销商名单。"
    envelope = redis_envelope(redis)
    record = envelope["messages"][followup.message_id]

    assert followup_result.status == "COMPLETED"
    assert calls[1].question == expected
    assert calls[1].history == []
    assert expected in followup_result.analysis_process[0].summary
    assert record["bridge_route"] == "V1_EXECUTION_ANCHOR_FOLLOWUP"
    assert record["execution_anchor"]["parent_anchor_id"] == first_anchor["anchor_id"]
    assert record["execution_anchor"]["revision"] == 2
    assert record["execution_anchor"]["time_context"]["applied_text"] == "2026年"
    assert envelope["state_version"] == 2


@pytest.mark.asyncio
async def test_failed_newer_fallback_invalidates_older_anchor_and_never_inherits_it(
    provider,
):
    chats = [
        request(conversation_id="anchor-order", message_id="old-success",
                question="查询旧的复杂关系问题。"),
        request(conversation_id="anchor-order", message_id="new-failure",
                question="查询新的复杂关系问题。"),
        request(conversation_id="anchor-order", message_id="short-followup",
                question="换今年"),
    ]
    v1_calls = []

    async def planner(current, identity, state, plans, pending):
        if current.message_id == chats[2].message_id:
            raise unresolved_with_parse(
                time_parse(current.question, current.message_id, "今年")
            )
        version = 1 if state is None else (
            ConversationState.model_validate(state.payload).state_version + 1
        )
        return ResolvedContextTurn(
            completed_question=current.question,
            next_state=state_artifact(context, current, version),
            plan_state=None,
            bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
            anchor_update=ExecutionAnchorUpdate(
                previous=None,
                current_parse=CurrentTurnSemanticParse(
                    dialogue_act_candidates=[DialogueAct.NEW_TASK]
                ),
                time_context=None,
                resolved_business_domain_ids=(205,),
            ),
        )

    async def v1(current, identity):
        v1_calls.append(current.question)
        if current.message_id == chats[1].message_id:
            failed = response(current)
            failed.status = "SAFE_FALLBACK"
            failed.error_code = "V1_EXECUTION_FAILED"
            return failed
        return executed_response(current)

    handler, context, redis = make_handler(provider, planner, v1)
    assert (await handler.handle(chats[0], IDENTITY)).status == "COMPLETED"
    assert (await handler.handle(chats[1], IDENTITY)).status == "SAFE_FALLBACK"
    result = await handler.handle(chats[2], IDENTITY)
    envelope = redis_envelope(redis)

    assert result.status == "SAFE_FALLBACK"
    assert result.error_code == "V2_CONTEXT_UNRESOLVED"
    assert v1_calls == [chats[0].question, chats[1].question]
    assert envelope["messages"][chats[1].message_id]["execution_anchor"] is None
    assert envelope["messages"][chats[2].message_id]["v1_execution_called"] is False


@pytest.mark.asyncio
async def test_current_explicit_time_replaces_execution_anchor_time(provider):
    first = request(
        conversation_id="anchor-current-explicit",
        message_id="anchor-2025",
        question="查询2025年空心纤维血液透析器产品合作的经销商名单。",
    )
    followup = request(
        conversation_id=first.conversation_id,
        message_id="anchor-2026",
        question="换今年",
    )
    calls = []

    async def planner(current, identity, state, plans, pending):
        if current.message_id == first.message_id:
            initial_parse = time_parse(
                current.question, current.message_id, "2025年",
                operation="SET", followup=False,
            )
            return ResolvedContextTurn(
                completed_question=current.question,
                next_state=state_artifact(context, current, 1),
                plan_state=None,
                bridge_route="V1_EXECUTION_FALLBACK_NEW_TASK",
                anchor_update=ExecutionAnchorUpdate(
                    previous=None,
                    current_parse=initial_parse,
                    time_context=None,
                    resolved_business_domain_ids=(205,),
                ),
            )
        raise unresolved_with_parse(
            time_parse(current.question, current.message_id, "今年")
        )

    async def v1(current, identity):
        calls.append(current.question)
        return executed_response(current)

    handler, context, _redis = make_handler(provider, planner, v1)
    await handler.handle(first, IDENTITY)
    result = await handler.handle(followup, IDENTITY)

    assert result.status == "COMPLETED"
    assert calls == [
        first.question,
        "查询2026年空心纤维血液透析器产品合作的经销商名单。",
    ]


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
