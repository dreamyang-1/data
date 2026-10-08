"""Machine-readable capability inventory for the fixed bridge mainline."""
from __future__ import annotations

from typing import Any


BRIDGE_TIMING_VERSION = "bridge-timing-v1"
SEMANTIC_DECISION_VERSION = "semantic-decision-v1"
BRIDGE_RUNTIME_MODE = "V2_CONTEXT_V1_EXECUTION"

_CAPABILITIES = (
    ("V2_CONTEXT", "catalog", "V2 request-scoped Catalog", "v2.catalog.load"),
    ("V2_CONTEXT", "model", "V2 current-turn recognition model", "v2.model.current_turn"),
    ("V2_CONTEXT", "model", "V2 semantic binding model", "v2.model.semantic_edits"),
    ("V2_CONTEXT", "bridge", "V2 context resolution", "v2.context_resolution"),
    ("V2_CONTEXT", "contract", "Unified semantic decision build", "semantic.contract.build"),
    ("V1_ORCHESTRATION", "bridge", "V1 execution bridge", "bridge.v1_execution"),
    ("V1_ORCHESTRATION", "contract", "Semantic decision safety validation", "semantic.contract.validation"),
    ("V1_ORCHESTRATION", "rewrite", "V1 question rewrite", "v1.question_rewrite"),
    ("V1_ORCHESTRATION", "intent", "V1 intent recognition", "v1.intent_recognition"),
    ("V1_ORCHESTRATION", "planning", "V1 task decomposition", "v1.task_decomposition"),
    ("V1_ORCHESTRATION", "semantic", "V1 semantic binding", "v1.semantic_binding"),
    ("UPSTREAM", "oagnet", "Oagnet ASL generation", "upstream.oagnet.asl_generation"),
    ("UPSTREAM", "sql", "SQL translation", "upstream.sql.translation"),
    ("UPSTREAM", "sql", "SQL execution", "upstream.sql.execution"),
    ("VALIDATION", "validation", "Result reliability validation", "validation.result_reliability"),
    ("ANALYSIS", "analysis", "Deterministic analysis", "analysis.deterministic"),
    ("ANALYSIS", "model", "Analysis synthesis model", "analysis.synthesis"),
    ("ANALYSIS", "chart", "Deterministic chart specification", "chart.specification"),
    ("EXTENSION", "mcp", "MCP chart rendering", "mcp.chart_render"),
    ("EXTENSION", "chart", "Inline chart fallback", "chart.inline_render"),
)


def bridge_capability_profile(*, active_runtime_mode: str) -> dict[str, Any]:
    return {
        "profile": "V2 context with V1 execution",
        "required_runtime_mode": BRIDGE_RUNTIME_MODE,
        "active": active_runtime_mode == BRIDGE_RUNTIME_MODE,
        "pure_v2_execution_expansion": "PAUSED",
        "timing_trace_version": BRIDGE_TIMING_VERSION,
        "semantic_decision_version": SEMANTIC_DECISION_VERSION,
        "semantic_handoff": {
            "transport_visibility": "INTERNAL_PRIVATE_ATTRIBUTE",
            "accepted_source": "V2_AUTHORIZED_PLAN",
            "accepted_v1_intent_model": "SKIPPED",
            "fallback_source": "V1_SEMANTIC_FALLBACK",
            "fallback_reason_recorded": True,
            "v1_execution_safety_validation": "REQUIRED",
        },
        "timing_delivery": ["complete.performance_trace", "server_log"],
        "content_capture": False,
        "capabilities": [
            {
                "layer": layer,
                "kind": kind,
                "name": name,
                "timing_operation": timing_operation,
            }
            for layer, kind, name, timing_operation in _CAPABILITIES
        ],
    }
