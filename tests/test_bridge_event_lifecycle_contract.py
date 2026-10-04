"""Independent lifecycle contract probes for V2_CONTEXT_V1_EXECUTION bridge.

SPRINT-20260917-C-BRIDGE-EVENT-PROBE (REVISION_1_TEST_FIXTURE_ONLY). These
tests exercise the real bridge and the real V1 orchestrator end to end with
offline stand-ins only: scripted recognition model, test catalog fixtures,
mock SQL adapters, in-memory stores. Both tests pass the same executor the
production wiring uses: ``DataAnalysisOrchestrator.execute_v1_from_completed_question``
(see ``app/main.py`` bridge construction); that is the real entry behind
``bridge.handle``, so nothing is hand-faked.

Known baseline state (recorded, not patched): the orchestrator currently
records TURN_ADMISSION/CONTEXT_MERGE/QUERY_RESOLUTION with
``trace_id=str(request_id)`` and does not emit USER_QUERY/INTENT_RESULT/
FINAL_INSIGHT/TRACE_SUMMARY, so lifecycle assertions may fail. Failure is a
valid delivery: it pins the exact gap for the A-side event rework. No
production code is modified and no events are fabricated.
"""

from dataclasses import replace
from uuid import UUID

import pytest

from app.adapters import build_mock_adapters
from app.adapters.base import AdapterError
from app.config import Settings
from app.intent import RuleBasedIntentClassifier
from app.planning import MultiQuestionPlanner
from app.semantic_v2.context_state_store import RedisContextStateStore
from app.semantic_v2.context_v1_execution import V2ContextV1ExecutionBridge
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.question_rewriter import QuestionRewriter
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEventType
from test_v2_authorized_catalog_bridge import IDENTITY, request
from test_v2_context_followup_critical_slice import context_case, context_catalog
from test_v2_limited_scalar_deployment import DeploymentRedis
from test_v2_persisted_scalar_api import NOW
from test_v2_raw_turn_recognition import planner as scripted_planner


LIFECYCLE = (
    SessionEventType.USER_QUERY,
    SessionEventType.INTENT_RESULT,
    SessionEventType.FINAL_INSIGHT,
    SessionEventType.TRACE_SUMMARY,
)


class _RecordingQueryTool:
    """Delegating semantic query stand-in counting real executions."""

    def __init__(self, delegate, *, fail_on_call: int | None = None):
        self.delegate = delegate
        self.requests = []
        self.fail_on_call = fail_on_call

    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if self.fail_on_call == len(self.requests):
            raise AdapterError(
                "DEPENDENCY_CONTRACT_REJECTED",
                "dependency rejected the semantic/query contract",
                retryable=False,
                upstream_code="DEPENDENCY_CONTRACT_REJECTED",
            )
        return await self.delegate.query(request, identity, **kwargs)

    async def discover_metrics(self, request, identity, **kwargs):
        return await self.delegate.discover_metrics(request, identity, **kwargs)

    async def discover_attribute_details(self, request, identity, **kwargs):
        return await self.delegate.discover_attribute_details(
            request, identity, **kwargs
        )


def _bridge_event_orchestrator(events: InMemorySessionEventStore, **query_kwargs):
    """Real V1 orchestrator with offline model/catalog/SQL stand-ins."""
    settings = Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        multi_question_enabled=True,
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    base = build_mock_adapters()
    query = _RecordingQueryTool(base.query, **query_kwargs)
    adapters = replace(base, semantic_query=query)
    orchestrator = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=adapters,
        sessions=InMemorySessionStore(),
        task_planner=MultiQuestionPlanner(settings),
        question_rewriter=QuestionRewriter(None),
        event_store=events,
    )
    return orchestrator, query


def _bridge_handler(catalog, redis, v1, *, model):
    return V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(
            redis,
            prefix="youo:data-analysis:v2-context-live:event-probe",
            ttl_seconds=3600,
            idempotency_ttl_seconds=7200,
        ),
        catalog=catalog,
        model=model,
        v1_executor=v1,
        clock=lambda: NOW,
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"},
        demo_mode=True,
    )


async def _assert_single_lifecycle(events, chat):
    """Structural lifecycle assertions; failures pin the A-side gap."""
    recorded = await events.list_events(
        IDENTITY.tenant_id, IDENTITY.user_id, chat.application_id,
        chat.conversation_id,
    )
    turn_events = [item for item in recorded if item.message_id == chat.message_id]
    types = [item.event_type for item in turn_events]
    for required in LIFECYCLE:
        assert types.count(required) == 1, (required, types)
    trace_ids = {item.trace_id for item in turn_events}
    assert len(trace_ids) == 1, trace_ids
    UUID(next(iter(trace_ids)))
    assert all(item.message_id == chat.message_id for item in turn_events)
    assert turn_events[-1].event_type == SessionEventType.TRACE_SUMMARY


@pytest.mark.asyncio
async def test_bridge_normal_request_records_full_lifecycle_once(context_catalog):
    """One external request through real bridge + real V1 orchestrator.

    The bridge parsing the scripted recognition model is expected behavior
    (offline means scripted model, not no model). The executor is the same
    production entry ``app.main`` wires: ``execute_v1_from_completed_question``.
    """
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    events = InMemorySessionEventStore()
    orchestrator, query = _bridge_event_orchestrator(events)
    executions = []

    async def v1(chat, identity):
        executions.append(chat.model_copy(deep=True))
        return await orchestrator.execute_v1_from_completed_question(chat, identity)

    bridge = _bridge_handler(
        context_catalog[0], redis, v1, model=scripted.model
    )
    chat = request(
        question=first_step[0],
        message_id="bridge-lifecycle-m1",
        conversation_id="bridge-lifecycle-normal",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert len(executions) == len(query.requests) == 1
    # The scripted recognition pipeline issues multiple model calls per turn
    # (parse + fact stages); assert real model traffic, not a fixed count.
    assert len(transport.calls) >= 1
    await _assert_single_lifecycle(events, chat)


@pytest.mark.asyncio
async def test_bridge_result_availability_retry_keeps_single_external_lifecycle(
    context_catalog,
):
    """Demo result-availability retry: two real executions, one terminal group."""
    first_step = context_case(4)[0]
    scripted, transport = scripted_planner(context_catalog, [first_step])
    redis = DeploymentRedis()
    events = InMemorySessionEventStore()
    # The first availability query of the retried turn has no usable result.
    orchestrator, query = _bridge_event_orchestrator(events, fail_on_call=2)
    executions = []

    async def v1(chat, identity):
        executions.append(chat.model_copy(deep=True))
        return await orchestrator.execute_v1_from_completed_question(chat, identity)

    bridge = _bridge_handler(
        context_catalog[0], redis, v1, model=scripted.model
    )
    conversation_id = "bridge-lifecycle-retry"
    await bridge.handle(request(
        question=first_step[0],
        message_id="bridge-retry-first",
        conversation_id=conversation_id,
    ), IDENTITY)

    retried_chat = request(
        question="按月看销售额。",
        message_id="bridge-retry-second",
        conversation_id=conversation_id,
    )
    result = await bridge.handle(retried_chat, IDENTITY)

    assert result.status == "COMPLETED"
    assert result.error_code is None
    # Two real executions happened for the retried external turn: the failed
    # availability query and the successful retry question.
    retried_executions = executions[1:]
    assert len(retried_executions) == 2
    assert retried_executions[0].question != retried_executions[1].question
    assert len(query.requests) == 3
    assert len(transport.calls) == 2

    # External view: still one lifecycle terminal group, TRACE_SUMMARY last.
    await _assert_single_lifecycle(events, retried_chat)
