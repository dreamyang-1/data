"""Portable request traces and deterministic harness evaluation metrics."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import Field

from app.domain.models import AgentResponse, PrimaryIntent, StrictModel
from app.stores.events import SessionEvent, SessionEventType


class TraceSpan(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["REQUEST", "TOOL", "ANALYSIS"]
    status: Literal["COMPLETED", "FAILED", "CLARIFICATION", "PARTIAL"]
    latency_ms: int = Field(ge=0)
    attributes: dict = Field(default_factory=dict)


class TraceSummary(StrictModel):
    trace_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=50)
    intent: str = Field(min_length=1, max_length=80)
    latency_ms: int = Field(ge=0)
    spans: list[TraceSpan] = Field(default_factory=list, max_length=100)
    tool_calls: int = Field(ge=0)
    tool_successes: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    evidence_covered: bool
    clarification_required: bool
    clarification_round: int = Field(ge=0)
    recovery_success: bool | None = None

    @classmethod
    def from_response(
        cls,
        *,
        trace_id: str,
        session_id: str,
        response: AgentResponse,
        latency_ms: int,
        events: Iterable[SessionEvent] = (),
    ) -> "TraceSummary":
        spans = [TraceSpan(
            name="agent_request",
            kind="REQUEST",
            status=cls._span_status(response.status),
            latency_ms=latency_ms,
            attributes={"intent": response.intent.value},
        )]
        event_spans = cls._event_spans(events)
        if event_spans:
            spans.extend(event_spans)
        else:
            for execution in response.extension_executions:
                spans.append(TraceSpan(
                    name=execution.name,
                    kind="TOOL",
                    status=(
                        "COMPLETED" if execution.status == "COMPLETED" else "FAILED"
                    ),
                    latency_ms=execution.latency_ms or 0,
                    attributes={
                        "kind": execution.kind,
                        "attempts": execution.attempts,
                        "error_type": execution.error_type,
                    },
                ))
        data_intent = response.intent not in {
            PrimaryIntent.CHAT,
            PrimaryIntent.CAPABILITY_HELP,
            PrimaryIntent.OUT_OF_SCOPE,
        }
        clarification_required = response.status == "NEEDS_CLARIFICATION"
        clarification_round = response.clarification_round or 0
        recovery_success = (
            response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
            if clarification_round > 0 and not clarification_required else None
        )
        return cls(
            trace_id=trace_id,
            session_id=session_id,
            status=response.status,
            intent=response.intent.value,
            latency_ms=latency_ms,
            spans=spans,
            tool_calls=sum(item.kind == "TOOL" for item in spans),
            tool_successes=sum(
                item.kind == "TOOL" and item.status == "COMPLETED" for item in spans
            ),
            evidence_count=len(response.evidence),
            evidence_covered=(not data_intent or bool(response.evidence)),
            clarification_required=clarification_required,
            clarification_round=clarification_round,
            recovery_success=recovery_success,
        )

    @classmethod
    def _event_spans(cls, events: Iterable[SessionEvent]) -> list[TraceSpan]:
        spans: list[TraceSpan] = []
        for event in events:
            payload = event.payload
            if event.event_type == SessionEventType.TURN_ADMISSION:
                spans.append(TraceSpan(
                    name="turn_admission",
                    kind="ANALYSIS",
                    status="COMPLETED",
                    latency_ms=0,
                    attributes=cls._bounded_attributes(payload, {
                        "turn_relation", "context_mode", "self_contained",
                        "context_dependency", "core_subject_changed", "reference_signals",
                        "followup_signals", "omitted_slots", "temporal_reference",
                        "inheritance_allowed", "inheritance_slots",
                        "protected_slots", "cleared_slots", "previous_thread",
                        "selected_thread", "new_thread_created", "reason_codes",
                    }),
                ))
            elif event.event_type == SessionEventType.CONTEXT_MERGE:
                spans.append(TraceSpan(
                    name="context_inheritance_gate",
                    kind="ANALYSIS",
                    status=(
                        "FAILED" if payload.get("context_conflicts") else "COMPLETED"
                    ),
                    latency_ms=0,
                    attributes=cls._bounded_attributes(payload, {
                        "turn_relation", "context_mode", "inheritance_allowed",
                        "inheritance_slots", "protected_slots", "cleared_slots",
                        "context_before", "context_delta", "context_after",
                        "context_conflicts", "new_thread_created",
                        "inherited_slots", "temporal_anchor", "resolved_periods",
                        "comparison",
                    }),
                ))
            elif event.event_type == SessionEventType.QUERY_RESOLUTION:
                spans.append(TraceSpan(
                    name="conversation_query_resolution",
                    kind="ANALYSIS",
                    status="COMPLETED",
                    latency_ms=0,
                    attributes=cls._bounded_attributes(payload, {
                        "raw_query", "turn_relation", "active_thread",
                        "active_episode", "context_dependency", "omitted_slots",
                        "inherited_slots", "temporal_reference", "temporal_anchor",
                        "resolved_periods", "comparison_type", "comparison",
                        "previous_result_available", "result_sufficiency",
                        "execution_mode", "source_dataset_id",
                    }),
                ))
            elif event.event_type == SessionEventType.TOOL_RESULT:
                spans.append(TraceSpan(
                    name=str(payload.get("tool_name") or "tool")[:100],
                    kind="TOOL",
                    status=cls._runtime_status(payload.get("status")),
                    latency_ms=cls._latency(payload.get("latency_ms")),
                    attributes=cls._bounded_attributes(payload, {
                        "kind", "execution_id", "attempts", "status_code", "error_type"
                    }),
                ))
            elif event.event_type == SessionEventType.PYTHON_ANALYSIS:
                for step in payload.get("step_results", []):
                    if not isinstance(step, dict):
                        continue
                    spans.append(TraceSpan(
                        name=str(step.get("tool") or step.get("step_id") or "analysis")[:100],
                        kind="ANALYSIS",
                        status=cls._runtime_status(step.get("status")),
                        latency_ms=cls._latency(step.get("latency_ms")),
                        attributes=cls._bounded_attributes(step, {
                            "step_id", "action", "attempts", "error_code"
                        }),
                    ))
            elif event.event_type == SessionEventType.VALIDATION_RESULT:
                spans.append(TraceSpan(
                    name=str(payload.get("validation_scope") or "result_validation")[:100],
                    kind="ANALYSIS",
                    status=cls._runtime_status(payload.get("status")),
                    latency_ms=cls._latency(payload.get("latency_ms")),
                    attributes=cls._bounded_attributes(payload, {
                        "validation_scope", "code", "warning_count", "error_count",
                        "current_entities", "stale_entities",
                    }),
                ))
        return spans

    @staticmethod
    def _latency(value: Any) -> int:
        return max(0, round(value)) if isinstance(value, (int, float)) else 0

    @staticmethod
    def _bounded_attributes(payload: dict[str, Any], keys: set[str]) -> dict[str, Any]:
        return {key: payload[key] for key in keys if key in payload and payload[key] is not None}

    @staticmethod
    def _runtime_status(value: Any) -> str:
        if value == "COMPLETED" or value == "PASS":
            return "COMPLETED"
        if value == "PARTIAL_SUCCESS" or value == "WARN":
            return "PARTIAL"
        return "FAILED"

    @staticmethod
    def _span_status(status: str) -> str:
        if status == "NEEDS_CLARIFICATION":
            return "CLARIFICATION"
        if status == "PARTIAL_SUCCESS":
            return "PARTIAL"
        if status in {"COMPLETED", "CANCELLED"}:
            return "COMPLETED"
        return "FAILED"


class EvaluationReport(StrictModel):
    trace_count: int = Field(ge=0)
    completion_rate: float = Field(ge=0, le=1)
    partial_success_rate: float = Field(ge=0, le=1)
    clarification_rate: float = Field(ge=0, le=1)
    tool_success_rate: float | None = Field(default=None, ge=0, le=1)
    recovery_success_rate: float | None = Field(default=None, ge=0, le=1)
    evidence_coverage_rate: float = Field(ge=0, le=1)
    latency_ms_p50: int = Field(ge=0)
    latency_ms_p95: int = Field(ge=0)
    intent_breakdown: dict[str, int] = Field(default_factory=dict)


class HarnessEvaluator:
    """Aggregate trace summaries without interpreting business result text."""

    @classmethod
    def evaluate(cls, traces: list[TraceSummary]) -> EvaluationReport:
        if not traces:
            return EvaluationReport(
                trace_count=0, completion_rate=0, clarification_rate=0,
                partial_success_rate=0,
                evidence_coverage_rate=0, latency_ms_p50=0, latency_ms_p95=0,
            )
        total = len(traces)
        tool_calls = sum(item.tool_calls for item in traces)
        recoveries = [
            item.recovery_success for item in traces
            if item.recovery_success is not None
        ]
        intents: dict[str, int] = defaultdict(int)
        for item in traces:
            intents[item.intent] += 1
        latencies = sorted(item.latency_ms for item in traces)
        return EvaluationReport(
            trace_count=total,
            completion_rate=sum(
                item.status in {"COMPLETED", "PARTIAL_SUCCESS"} for item in traces
            ) / total,
            partial_success_rate=sum(
                item.status == "PARTIAL_SUCCESS" for item in traces
            ) / total,
            clarification_rate=sum(item.clarification_required for item in traces) / total,
            tool_success_rate=(
                sum(item.tool_successes for item in traces) / tool_calls
                if tool_calls else None
            ),
            recovery_success_rate=(
                sum(bool(item) for item in recoveries) / len(recoveries)
                if recoveries else None
            ),
            evidence_coverage_rate=sum(item.evidence_covered for item in traces) / total,
            latency_ms_p50=cls._percentile(latencies, 0.50),
            latency_ms_p95=cls._percentile(latencies, 0.95),
            intent_breakdown=dict(sorted(intents.items())),
        )

    @staticmethod
    def _percentile(values: list[int], quantile: float) -> int:
        if not values:
            return 0
        index = max(0, math.ceil(quantile * len(values)) - 1)
        return values[index]
