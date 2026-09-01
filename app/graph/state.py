"""Shared structured state for the 24 primary graph nodes."""
from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from app.domain.models import (
    AgentResponse, CanonicalAnalysisRequest, ChatRequest, DataQueryResult,
    Dataset, EvidenceItem, KnowledgeContext, ReliabilityReport, TrustedIdentity,
)


class NodeTrace(TypedDict):
    node: str
    status: str
    detail: str


class AgentState(TypedDict):
    chat: ChatRequest
    identity: TrustedIdentity
    trusted_context: NotRequired[dict[str, Any]]
    request_fingerprint: NotRequired[str]
    conversation_context: NotRequired[dict[str, Any]]
    conversation_control: NotRequired[str]
    intent_candidate: NotRequired[dict[str, Any]]
    semantic_candidates: NotRequired[list[dict[str, Any]]]
    request: NotRequired[CanonicalAnalysisRequest]
    completeness_report: NotRequired[dict[str, Any]]
    pending_task: NotRequired[dict[str, Any]]
    clarification_rounds: NotRequired[int]
    atomic_tasks: NotRequired[list[dict[str, Any]]]
    task_plan: NotRequired[dict[str, Any]]
    approved_plan: NotRequired[dict[str, Any]]
    execution_state: NotRequired[dict[str, Any]]
    validated_asl: NotRequired[dict[str, Any]]
    query_result: NotRequired[DataQueryResult]
    dataset: NotRequired[Dataset]
    dataset_reference: NotRequired[dict[str, Any]]
    applicability_report: NotRequired[dict[str, Any]]
    knowledge_context: NotRequired[KnowledgeContext]
    analysis_facts: NotRequired[list[dict[str, Any]]]
    validation_report: NotRequired[dict[str, Any]]
    chart_specs: NotRequired[list[dict[str, Any]]]
    insights: NotRequired[list[dict[str, Any]]]
    narrative: NotRequired[str]
    evidence: NotRequired[list[EvidenceItem]]
    reliability_report: NotRequired[ReliabilityReport]
    audit_events: NotRequired[list[dict[str, Any]]]
    response: NotRequired[AgentResponse]
    terminal_reason: NotRequired[str]
    node_trace: NotRequired[list[NodeTrace]]


PRIMARY_NODE_NAMES = (
    "api_access", "request_validation", "memory_restore", "conversation_control",
    "intent_extraction", "semantic_retrieval", "semantic_resolution",
    "completeness_check", "clarification", "question_split", "dag_planning",
    "plan_validation", "skill_dispatch", "asl_generation", "sql_execution",
    "result_normalization", "dataset_registration", "applicability_check",
    "knowledge_retrieval", "deterministic_analysis", "cross_validation",
    "chart_insight", "fact_summary", "reliability_gate",
)
FRAMEWORK_NODE_NAMES = ("memory_audit", "final_output", "safe_fallback")
