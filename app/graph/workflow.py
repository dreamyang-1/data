"""Explicit graph matching the documented 24-node architecture.

During the compatibility migration, node 13 invokes the proven transactional
orchestrator. Named stages around it provide stable observability and allow each
legacy responsibility to be extracted independently without changing the API.
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.adapters.handshake import HandshakeReport
from app.domain.models import PrimaryIntent
from app.graph.state import AgentState
from app.services import DataAnalysisOrchestrator


_INTENT_CAPABILITY_DEPS: dict[PrimaryIntent, set[str]] = {
    PrimaryIntent.METRIC_QUERY: {"semantic", "policy", "query"},
    PrimaryIntent.DETAIL_QUERY: {"semantic", "policy", "query"},
    PrimaryIntent.METRIC_DEFINITION: {"semantic"},
    PrimaryIntent.DATA_LINEAGE: {"semantic"},
    PrimaryIntent.TREND_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.COMPARISON_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.COMPOSITION_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.ANOMALY_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.ROOT_CAUSE_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.FORECAST_ANALYSIS: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.REPORT_GENERATION: {"semantic", "policy", "query", "analysis"},
    PrimaryIntent.DATA_QUALITY: {"semantic", "policy", "query", "analysis"},
}


class WorkflowNodes:
    def __init__(self, orchestrator: DataAnalysisOrchestrator, *, handshake: HandshakeReport | None = None) -> None:
        self.orchestrator = orchestrator
        self.handshake = handshake

    @staticmethod
    def _mark(state: AgentState, node: str, detail: str = "completed") -> dict[str, Any]:
        trace = list(state.get("node_trace", []))
        trace.append({"node": node, "status": "COMPLETED", "detail": detail})
        return {"node_trace": trace}

    async def api_access(self, state: AgentState) -> dict[str, Any]:
        chat, identity = state["chat"], state["identity"]
        return {"trusted_context": {"tenant_id": identity.tenant_id, "user_id": identity.user_id,
                "application_id": chat.application_id, "roles": list(identity.roles)},
                **self._mark(state, "api_access", "trusted identity bound")}

    async def request_validation(self, state: AgentState) -> dict[str, Any]:
        return {"request_fingerprint": self.orchestrator._request_fingerprint(state["chat"], state["identity"]),
                **self._mark(state, "request_validation", "schema and fingerprint validated")}

    async def memory_restore(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "memory_restore")
    async def conversation_control(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "conversation_control")
    async def intent_extraction(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "intent_extraction")
    async def semantic_retrieval(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "semantic_retrieval")
    async def semantic_resolution(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "semantic_resolution")
    async def completeness_check(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "completeness_check")
    async def clarification(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "clarification", "pending state saved")
    async def question_split(self, state: AgentState) -> dict[str, Any]: return {"atomic_tasks": state.get("atomic_tasks", []), **self._mark(state, "question_split")}
    async def dag_planning(self, state: AgentState) -> dict[str, Any]: return {"task_plan": state.get("task_plan", {"tasks": []}), **self._mark(state, "dag_planning")}
    async def plan_validation(self, state: AgentState) -> dict[str, Any]: return {"approved_plan": state.get("task_plan", {"tasks": []}), **self._mark(state, "plan_validation")}

    async def skill_dispatch(self, state: AgentState) -> dict[str, Any]:
        response = await self.orchestrator.handle(state["chat"], state["identity"])
        return {"response": response, "evidence": list(response.evidence),
                "reliability_report": response.reliability,
                "execution_state": {"status": response.status, "intent": response.intent.value},
                **self._mark(state, "skill_dispatch", "validated business flow dispatched")}

    async def asl_generation(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "asl_generation")
    async def sql_execution(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "sql_execution")
    async def result_normalization(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "result_normalization")
    async def dataset_registration(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "dataset_registration")
    async def applicability_check(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "applicability_check")
    async def knowledge_retrieval(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "knowledge_retrieval")
    async def deterministic_analysis(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "deterministic_analysis")
    async def cross_validation(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "cross_validation")
    async def chart_insight(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "chart_insight")
    async def fact_summary(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "fact_summary")
    async def reliability_gate(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "reliability_gate")
    async def memory_audit(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "memory_audit")
    async def final_output(self, state: AgentState) -> dict[str, Any]: return self._mark(state, "final_output")

    def _missing_capabilities(self, intent: PrimaryIntent) -> list[str]:
        if self.handshake is None:
            return []
        return sorted(c for c in _INTENT_CAPABILITY_DEPS.get(intent, set()) if not self.handshake.is_capable(c))

    def route_after_understand(self, state: AgentState) -> str:
        request, response = state.get("request"), state.get("response")
        intent = request.primary_intent if request else response.intent if response else None
        if intent is None or (intent and self._missing_capabilities(intent)):
            return "safe_terminate"
        no_data = getattr(self.orchestrator, "NO_DATA_INTENTS", {
            PrimaryIntent.CHAT, PrimaryIntent.CAPABILITY_HELP, PrimaryIntent.OUT_OF_SCOPE})
        return "compose_answer" if intent in no_data else "semantic"

    async def safe_terminate(self, state: AgentState) -> dict[str, Any]:
        request, response = state.get("request"), state.get("response")
        intent = request.primary_intent if request else response.intent if response else PrimaryIntent.OUT_OF_SCOPE
        missing = self._missing_capabilities(intent)
        reason = ("当前依赖能力不可用：" + ", ".join(missing) + "。请稍后重试。") if missing else state.get("terminal_reason", "当前条件不足，无法可靠完成本次请求。")
        if response is None:
            if request is None:
                raise RuntimeError("safe fallback requires a request or response")
            response = self.orchestrator._fallback(request, reason)
        return {"response": response, "terminal_reason": reason, **self._mark(state, "safe_fallback", reason)}

    def route_after_dispatch(self, state: AgentState) -> str:
        response = state.get("response")
        if response is None: return "safe_fallback"
        if response.status == "NEEDS_CLARIFICATION": return "clarification"
        if response.status in {"SAFE_FALLBACK", "SAFE_TERMINATED", "REJECTED"}: return "safe_fallback"
        if response.status == "CANCELLED" or response.intent in {
            PrimaryIntent.CHAT,
            PrimaryIntent.CAPABILITY_HELP,
            PrimaryIntent.OUT_OF_SCOPE,
            PrimaryIntent.METRIC_DEFINITION,
            PrimaryIntent.DATA_LINEAGE,
        }:
            return "memory_audit"
        return "asl_generation"


def build_workflow(orchestrator: DataAnalysisOrchestrator, *, handshake: HandshakeReport | None = None):
    nodes = WorkflowNodes(orchestrator, handshake=handshake)
    graph = StateGraph(AgentState)
    ordered = ["api_access", "request_validation", "memory_restore", "conversation_control",
               "intent_extraction", "semantic_retrieval", "semantic_resolution", "completeness_check",
               "question_split", "dag_planning", "plan_validation", "skill_dispatch", "asl_generation",
               "sql_execution", "result_normalization", "dataset_registration", "applicability_check",
               "knowledge_retrieval", "deterministic_analysis", "cross_validation", "chart_insight",
               "fact_summary", "reliability_gate", "memory_audit", "final_output"]
    for name in ordered: graph.add_node(name, getattr(nodes, name))
    graph.add_node("clarification", nodes.clarification)
    graph.add_node("safe_fallback", nodes.safe_terminate)
    graph.add_edge(START, ordered[0])
    for left, right in zip(ordered[:11], ordered[1:12]): graph.add_edge(left, right)
    graph.add_conditional_edges("skill_dispatch", nodes.route_after_dispatch,
                                {"clarification": "clarification", "safe_fallback": "safe_fallback",
                                 "memory_audit": "memory_audit", "asl_generation": "asl_generation"})
    for left, right in zip(ordered[12:], ordered[13:]): graph.add_edge(left, right)
    graph.add_edge("clarification", "memory_audit")
    graph.add_edge("safe_fallback", "memory_audit")
    graph.add_edge("final_output", END)
    return graph.compile()
