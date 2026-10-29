"""Offline lifecycle-wiring tests for the official bridge factory entry.

These tests construct the V2_CONTEXT_V1_EXECUTION bridge through the same
factory used by ``app/main.py`` (``build_context_v1_execution_handler``)
with the orchestrator lifecycle callbacks wired exactly as the official
ingress does.  They prove that one external request records exactly one
replayable lifecycle under one request-UUID trace id, including the
demo-only internal V1 retry.
"""
from types import MethodType
import uuid

import pytest
from pydantic import SecretStr

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import EvidenceItem
from app.intent import RuleBasedIntentClassifier
from app.semantic_v2.context_v1_execution import (
    ContextV1ExternalDependencies,
    build_context_v1_execution_handler,
)
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEventType
from test_v2_authorized_catalog_bridge import IDENTITY, provider, request
from test_v2_context_v1_execution_bridge import install_resolution
from test_v2_limited_scalar_deployment import DeploymentRedis


class NoModel:
    async def complete(self, **_kwargs):
        raise AssertionError("test installs an explicit context result")


def bridge_settings(*, demo_mode: bool) -> Settings:
    return Settings(
        _env_file=None,
        env="test",
        adapter_mode="mock",
        runtime_mode="V2_CONTEXT_V1_EXECUTION",
        session_store_mode="redis",
        redis_url="redis://localhost:6379/0",
        intent_model_enabled=True,
        intent_model_api_key=SecretStr("test-key"),
        intent_model_enable_thinking=False,
        intent_model_max_retries=0,
        demo_mode=demo_mode,
    )


def wired_bridge(provider_value, *, event_store, demo_mode=False):
    """Build the bridge through the official factory with lifecycle wiring."""
    orchestrator = DataAnalysisOrchestrator(
        settings=Settings(
            _env_file=None,
            env="test",
            adapter_mode="mock",
            intent_model_enabled=False,
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
        event_store=event_store,
    )
    bridge = build_context_v1_execution_handler(
        bridge_settings(demo_mode=demo_mode),
        v1_executor=orchestrator.execute_v1_from_completed_question,
        v1_lifecycle_open=orchestrator.open_external_turn,
        v1_lifecycle_close=orchestrator.close_external_turn,
        external=ContextV1ExternalDependencies(
            catalog=provider_value[0],
            model=NoModel(),
            redis=DeploymentRedis(),
        ),
    )
    return orchestrator, bridge


async def replayed_trace(events, conversation_id, message_id):
    """Discover the canonical trace exactly like the replay contract."""
    items = await events.list_events(
        "tenant", "user", "app", conversation_id, limit=500
    )
    mine = [item for item in items if item.message_id == message_id]
    assert mine, "a wired bridge request must append lifecycle events"
    trace_ids = {item.trace_id for item in mine}
    assert len(trace_ids) == 1, (
        f"one external request must own one canonical trace, got {trace_ids}"
    )
    canonical_trace = next(iter(trace_ids))
    uuid.UUID(canonical_trace)
    assert canonical_trace != message_id
    recorded = await events.list_events(
        "tenant", "user", "app", conversation_id, trace_id=canonical_trace
    )
    assert len(recorded) == len(mine)
    return recorded


def assert_single_replayable_lifecycle(recorded, *, message_id):
    assert all(item.message_id == message_id for item in recorded)
    types = [item.event_type for item in recorded]
    # Structural admission/merge stay in the replayable sequence.
    assert types[0] == SessionEventType.TURN_ADMISSION
    assert types.count(SessionEventType.TURN_ADMISSION) == 1
    assert types.count(SessionEventType.CONTEXT_MERGE) == 1
    # Every required lifecycle event appears exactly once per external
    # request and the trace summary is always the last event.
    for required in (
        SessionEventType.USER_QUERY,
        SessionEventType.INTENT_RESULT,
        SessionEventType.FINAL_INSIGHT,
        SessionEventType.TRACE_SUMMARY,
    ):
        assert types.count(required) == 1, (
            f"{required.value} must appear exactly once, got {types}"
        )
    assert types[-1] == SessionEventType.TRACE_SUMMARY
    # The runtime summary consumes this trace's structural events.
    span_names = [
        span["name"] for span in recorded[-1].payload.get("spans", [])
    ]
    assert "turn_admission" in span_names
    assert "context_inheritance_gate" in span_names


@pytest.mark.asyncio
async def test_factory_bridge_records_one_full_lifecycle_per_external_request(
    provider,
):
    events = InMemorySessionEventStore()
    _orchestrator, bridge = wired_bridge(provider, event_store=events)
    install_resolution(bridge, provider)
    chat = request(
        question="查询本月销售额",
        message_id="wiring-normal",
        conversation_id="wiring-normal",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    recorded = await replayed_trace(events, "wiring-normal", "wiring-normal")
    assert_single_replayable_lifecycle(recorded, message_id="wiring-normal")


@pytest.mark.asyncio
async def test_factory_bridge_internal_retry_shares_trace_and_single_lifecycle(
    provider,
):
    events = InMemorySessionEventStore()
    orchestrator, bridge = wired_bridge(
        provider, event_store=events, demo_mode=True
    )
    install_resolution(bridge, provider, completed="统计全部产品的销售额。")
    executions = []
    official_executor = orchestrator.execute_v1_from_completed_question

    async def failing_first_executor(chat, identity):
        executions.append(chat.model_copy(deep=True))
        response = await official_executor(chat, identity)
        if len(executions) == 1:
            response.status = "SAFE_FALLBACK"
            response.error_code = "DEPENDENCY_CONTRACT_REJECTED"
            response._upstream_error_code = "DEPENDENCY_CONTRACT_REJECTED"
        elif not any(
            item.kind == "QUERY_RESULT" for item in response.evidence
        ):
            response.evidence = [
                *response.evidence,
                EvidenceItem(
                    evidence_id="retry-query",
                    kind="QUERY_RESULT",
                    source_ref="data-source:retry",
                    payload={"row_count": 1},
                ),
            ]
        return response

    bridge.v1_executor = failing_first_executor
    chat = request(
        question="统计全部产品的销售额。",
        message_id="wiring-retry",
        conversation_id="wiring-retry",
    )

    result = await bridge.handle(chat, IDENTITY)

    assert result.status == "COMPLETED"
    # The demo availability retry really ran one internal V1 execution.
    assert len(executions) == 2
    assert executions[0].message_id == "wiring-retry"
    assert executions[1].message_id.startswith("v2-result-retry-")
    # The retry stayed silent: every recorded event belongs to the
    # external message and shares the single request-UUID trace.
    recorded = await replayed_trace(events, "wiring-retry", "wiring-retry")
    assert_single_replayable_lifecycle(recorded, message_id="wiring-retry")
