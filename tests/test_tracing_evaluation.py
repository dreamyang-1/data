import pytest
from uuid import uuid4

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import (
    AgentResponse, ChatRequest, EvidenceItem, ExtensionExecution, PrimaryIntent,
    TrustedIdentity,
)
from app.intent.classifier import RuleBasedIntentClassifier
from app.observability import HarnessEvaluator, TraceSummary
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEvent, SessionEventType


def response(status="COMPLETED", *, evidence=True, tool_status="COMPLETED"):
    return AgentResponse(
        request_id=uuid4(), conversation_id="conversation", status=status,
        intent=PrimaryIntent.METRIC_QUERY, answer="结果",
        evidence=([EvidenceItem(
            evidence_id="e1", kind="QUERY_RESULT", source_ref="query", payload={}
        )] if evidence else []),
        extension_executions=[ExtensionExecution(
            name="tool", kind="HTTP_TOOL", status=tool_status, latency_ms=20
        )],
    )


def test_trace_summary_contains_request_and_tool_spans():
    trace = TraceSummary.from_response(
        trace_id="message", session_id="conversation",
        response=response(), latency_ms=120,
    )
    assert trace.tool_calls == 1
    assert trace.tool_successes == 1
    assert trace.evidence_covered is True
    assert [(item.kind, item.latency_ms) for item in trace.spans] == [
        ("REQUEST", 120), ("TOOL", 20)
    ]


def test_trace_summary_uses_exact_runtime_event_spans():
    common = {
        "session_id": "conversation", "user_id": "user", "tenant_id": "tenant",
        "application_id": "app", "message_id": "m1", "trace_id": "m1",
    }
    events = [
        SessionEvent(
            **common, event_type=SessionEventType.TOOL_RESULT,
            payload={
                "tool_name": "intelligent_semantic_query", "status": "COMPLETED",
                "kind": "INTERNAL_QUERY_TOOL", "attempts": 2, "latency_ms": 35,
            },
        ),
        SessionEvent(
            **common, event_type=SessionEventType.PYTHON_ANALYSIS,
            payload={"step_results": [{
                "step_id": "trend", "tool": "deterministic_analysis_engine",
                "action": "TREND", "status": "COMPLETED", "latency_ms": 8,
            }]},
        ),
        SessionEvent(
            **common, event_type=SessionEventType.VALIDATION_RESULT,
            payload={"status": "PASS", "latency_ms": 2, "warning_count": 0},
        ),
    ]

    trace = TraceSummary.from_response(
        trace_id="m1", session_id="conversation", response=response(),
        latency_ms=60, events=events,
    )

    assert trace.tool_calls == 1
    assert trace.tool_successes == 1
    assert [(span.name, span.kind, span.latency_ms) for span in trace.spans[1:]] == [
        ("intelligent_semantic_query", "TOOL", 35),
        ("deterministic_analysis_engine", "ANALYSIS", 8),
        ("result_validation", "ANALYSIS", 2),
    ]


def test_evaluator_calculates_rates_and_nearest_rank_percentiles():
    traces = [
        TraceSummary.from_response(
            trace_id=f"m{i}", session_id="conversation",
            response=response(
                status="NEEDS_CLARIFICATION" if i == 0 else "COMPLETED",
                evidence=i != 2, tool_status="FAILED" if i == 2 else "COMPLETED",
            ), latency_ms=value,
        )
        for i, value in enumerate([10, 20, 30, 40])
    ]
    report = HarnessEvaluator.evaluate(traces)
    assert report.completion_rate == 0.75
    assert report.partial_success_rate == 0
    assert report.clarification_rate == 0.25
    assert report.tool_success_rate == 0.75
    assert report.evidence_coverage_rate == 0.75
    assert report.latency_ms_p50 == 20
    assert report.latency_ms_p95 == 40


@pytest.mark.asyncio
async def test_orchestrator_records_trace_summary_after_final_event():
    events = InMemorySessionEventStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(), event_store=events,
    )
    await agent.handle(
        ChatRequest(semantic_model_id=81,
            application_id="app", conversation_id="conversation",
            message_id="m1", question="你好",
        ), TrustedIdentity(tenant_id="tenant", user_id="user"),
    )
    recorded = await events.list_events(
        "tenant", "user", "app", "conversation", trace_id="m1"
    )
    assert recorded[-1].event_type == SessionEventType.TRACE_SUMMARY
    assert recorded[-1].payload["latency_ms"] >= 0
