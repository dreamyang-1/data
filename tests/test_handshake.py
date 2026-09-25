"""Capability Handshake 测试。

覆盖：
1. probe_capabilities 并发探活四个服务，返回正确状态。
2. 任一服务超时/异常不阻塞其他探测。
3. HandshakeReport.is_capable 按能力名查询。
4. workflow route_after_understand 在能力缺失时路由到 safe_terminate。
5. safe_terminate 在 capability 缺失时构造友好提示。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.handshake import HandshakeReport, probe_capabilities
from app.domain.models import (
    AgentResponse,
    CanonicalAnalysisRequest,
    ChatRequest,
    PendingState,
    PrimaryIntent,
    TrustedIdentity,
)
from app.graph.workflow import WorkflowNodes, _INTENT_CAPABILITY_DEPS


def _fake_adapters(semantic_ok=True, policy_ok=True, query_ok=True, knowledge_ok=True, analysis_ok=True):
    bundle = MagicMock()
    bundle.semantic = MagicMock()
    bundle.semantic.health = AsyncMock(return_value=semantic_ok)
    bundle.policy = MagicMock()
    bundle.policy.health = AsyncMock(return_value=policy_ok)
    bundle.query = MagicMock()
    bundle.query.health = AsyncMock(return_value=query_ok)
    bundle.knowledge = MagicMock()
    bundle.knowledge.health = AsyncMock(return_value=knowledge_ok)
    bundle.analysis = MagicMock()
    bundle.analysis.health = AsyncMock(return_value=analysis_ok)
    return bundle


@pytest.mark.asyncio
async def test_probe_capabilities_all_healthy():
    bundle = _fake_adapters(True, True, True, True, True)
    report = await probe_capabilities(bundle)
    assert report.semantic is True
    assert report.policy is True
    assert report.query is True
    assert report.knowledge is True
    assert report.analysis is True
    assert report.raw_errors == {}
    assert report.probed_at  # ISO timestamp


@pytest.mark.asyncio
async def test_probe_capabilities_partial_failure():
    bundle = _fake_adapters(True, False, True, False, True)
    report = await probe_capabilities(bundle)
    assert report.semantic is True
    assert report.policy is False
    assert report.query is True
    assert report.knowledge is False
    assert report.analysis is True
    assert "policy" in report.raw_errors
    assert "knowledge" in report.raw_errors
    assert report.raw_errors["policy"] == ""  # MagicMock 默认返回 True 不带 error


@pytest.mark.asyncio
async def test_probe_capabilities_timeout_does_not_block_others():
    """一个服务超时不应该拖慢其他服务的探测。"""
    async def slow_health():
        await asyncio.sleep(5)
        return True

    bundle = MagicMock()
    bundle.semantic = MagicMock()
    bundle.semantic.health = AsyncMock(return_value=True)
    bundle.policy = MagicMock()
    bundle.policy.health = slow_health  # 模拟超时
    bundle.query = MagicMock()
    bundle.query.health = AsyncMock(return_value=True)
    bundle.knowledge = MagicMock()
    bundle.knowledge.health = AsyncMock(return_value=True)
    bundle.analysis = MagicMock()
    bundle.analysis.health = AsyncMock(return_value=True)

    report = await asyncio.wait_for(probe_capabilities(bundle), timeout=4.0)
    assert report.semantic is True
    assert report.policy is False  # 被 2s 超时切断
    assert report.query is True
    assert report.knowledge is True
    assert report.analysis is True
    assert "timeout" in report.raw_errors["policy"]


def test_handshake_report_is_capable():
    report = HandshakeReport(
        semantic=True, policy=False, query=True, knowledge=True, analysis=True, probed_at="t"
    )
    assert report.is_capable("semantic") is True
    assert report.is_capable("policy") is False
    assert report.is_capable("analysis") is True
    assert report.is_capable("unknown") is False


def test_intent_capability_deps_mapping_complete():
    """每个 PrimaryIntent 都应该有映射（无依赖的意图不在此表中）。"""
    expected = {
        PrimaryIntent.METRIC_QUERY,
        PrimaryIntent.DETAIL_QUERY,
        PrimaryIntent.METRIC_DEFINITION,
        PrimaryIntent.DATA_LINEAGE,
        PrimaryIntent.TREND_ANALYSIS,
        PrimaryIntent.COMPARISON_ANALYSIS,
        PrimaryIntent.COMPOSITION_ANALYSIS,
        PrimaryIntent.ANOMALY_ANALYSIS,
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        PrimaryIntent.FORECAST_ANALYSIS,
        PrimaryIntent.REPORT_GENERATION,
        PrimaryIntent.DATA_QUALITY,
    }
    assert set(_INTENT_CAPABILITY_DEPS.keys()) == expected


def _make_request(intent: PrimaryIntent) -> CanonicalAnalysisRequest:
    return CanonicalAnalysisRequest(
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="问个问题",
        primary_intent=intent,
        metrics=[],
        missing_slots=[],
    )


def _make_state(intent: PrimaryIntent) -> dict:
    chat = ChatRequest(semantic_model_id=81,
        conversation_id="c1",
        message_id="m1",
        question="问个问题",
        application_id="test-app",
    )
    return {
        "chat": chat,
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1", roles=[]),
        "request": _make_request(intent),
        "clarification_rounds": 1,
    }


class _FakeOrch:
    """最小 orchestrator 替身，仅供路由测试用。"""

    NO_DATA_INTENTS = {PrimaryIntent.CHAT, PrimaryIntent.CAPABILITY_HELP, PrimaryIntent.OUT_OF_SCOPE}
    # 高级分析意图已全部开放，不再在路由阶段禁用。
    ADVANCED_DISABLED: set[PrimaryIntent] = set()
    ADVANCED_INTENTS = {
        PrimaryIntent.TREND_ANALYSIS,
        PrimaryIntent.COMPARISON_ANALYSIS,
        PrimaryIntent.COMPOSITION_ANALYSIS,
        PrimaryIntent.ANOMALY_ANALYSIS,
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        PrimaryIntent.FORECAST_ANALYSIS,
        PrimaryIntent.REPORT_GENERATION,
        PrimaryIntent.DATA_QUALITY,
    }

    class _Settings:
        max_clarification_rounds = 2

    def __init__(self):
        self.settings = self._Settings()

    def _fallback(self, request: CanonicalAnalysisRequest, reason: str) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            status="SAFE_TERMINATED",
            intent=request.primary_intent,
            answer=reason,
        )


def test_route_after_understand_routes_to_safe_terminate_when_capability_missing():
    """METRIC_QUERY 依赖 semantic+policy+query；policy 不可用 → safe_terminate。"""
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=False, query=True, knowledge=True, analysis=True, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.METRIC_QUERY)
    assert nodes.route_after_understand(state) == "safe_terminate"


def test_route_after_understand_proceeds_when_all_capabilities_ok():
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=True, query=True, knowledge=True, analysis=True, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.METRIC_QUERY)
    assert nodes.route_after_understand(state) == "semantic"


@pytest.mark.parametrize(
    "intent",
    [
        PrimaryIntent.METRIC_QUERY,
        PrimaryIntent.METRIC_DEFINITION,
        PrimaryIntent.TREND_ANALYSIS,
        PrimaryIntent.ANOMALY_ANALYSIS,
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        PrimaryIntent.FORECAST_ANALYSIS,
    ],
)
def test_document_knowledge_outage_does_not_block_semantic_data_flows(intent):
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True,
        policy=True,
        query=True,
        knowledge=False,
        analysis=True,
        probed_at="t",
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    assert nodes.route_after_understand(_make_state(intent)) == "semantic"


def test_route_after_understand_proceeds_when_handshake_none():
    """handshake 为 None（如旧测试）时保持兼容，不阻断路由。"""
    orch = _FakeOrch()
    nodes = WorkflowNodes(orch, handshake=None)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.METRIC_QUERY)
    assert nodes.route_after_understand(state) == "semantic"


def test_route_after_understand_chat_intent_no_capability_check():
    """CHAT 意图不依赖外部能力，即使所有能力都不可用也应路由到 compose_answer。"""
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=False, policy=False, query=False, knowledge=False, analysis=False, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.CHAT)
    assert nodes.route_after_understand(state) == "compose_answer"


def test_route_after_understand_trend_analysis_blocked_when_analysis_unavailable():
    """TREND_ANALYSIS 依赖 analysis 能力；analysis 不可用 → safe_terminate。"""
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=True, query=True, knowledge=True, analysis=False, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.TREND_ANALYSIS)
    assert nodes.route_after_understand(state) == "safe_terminate"


def test_route_after_understand_trend_analysis_proceeds_when_analysis_ok():
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=True, query=True, knowledge=True, analysis=True, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.TREND_ANALYSIS)
    assert nodes.route_after_understand(state) == "semantic"


@pytest.mark.asyncio
async def test_safe_terminate_message_includes_missing_capabilities():
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=False, query=True, knowledge=True, analysis=True, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.METRIC_QUERY)

    result = await nodes.safe_terminate(state)
    response: AgentResponse = result["response"]
    # 提示中应包含不可用能力名 policy
    assert "policy" in response.answer


@pytest.mark.asyncio
async def test_safe_terminate_message_includes_analysis_when_missing():
    """高级分析意图在 analysis 不可用时，提示中应包含 analysis。"""
    orch = _FakeOrch()
    report = HandshakeReport(
        semantic=True, policy=True, query=True, knowledge=True, analysis=False, probed_at="t"
    )
    nodes = WorkflowNodes(orch, handshake=report)  # type: ignore[arg-type]
    state = _make_state(PrimaryIntent.ANOMALY_ANALYSIS)

    result = await nodes.safe_terminate(state)
    response: AgentResponse = result["response"]
    assert "analysis" in response.answer
