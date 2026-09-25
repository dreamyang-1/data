"""验证 LangGraph 多节点状态机的关键路径。"""
import pytest

from app.adapters import build_mock_adapters
from app.config import Settings
from app.domain.models import ChatRequest, PrimaryIntent, TrustedIdentity
from app.graph import build_workflow
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def make_orchestrator() -> DataAnalysisOrchestrator:
    return DataAnalysisOrchestrator(
        settings=Settings(env="test", adapter_mode="mock", intent_model_enabled=False),
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(),
    )


@pytest.mark.asyncio
async def test_workflow_routes_chat_directly_to_compose():
    """CHAT 意图走 understand → compose_answer，跳过语义/权限/查询。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81, application_id="test-app", conversation_id="c1", message_id="m1", question="你好"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    assert result["response"].status == "COMPLETED"
    assert result["response"].intent == PrimaryIntent.CHAT


@pytest.mark.asyncio
async def test_workflow_default_time_executes_directly_without_pending():
    """受控默认时间填充时间槽：指标请求直接执行，不产生伪 pending。

    真正缺槽位的工作流澄清路径由
    ``test_workflow_forecast_requires_history_before_execution`` 保护。
    """
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81, application_id="test-app", conversation_id="c2", message_id="m1", question="帮我查一下销售额"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    response = result["response"]
    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.METRIC_QUERY
    assert response.missing_slots == []
    assert await orchestrator.sessions.get_pending(
        "t1", "u1", "test-app", "c2"
    ) is None


@pytest.mark.asyncio
async def test_workflow_forecast_requires_history_before_execution():
    """预测不能只凭目标期硬算，必须先补充历史建模范围。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81, application_id="test-app", conversation_id="c3", message_id="m1", question="预测下个月销售额"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    response = result["response"]
    assert response.status == "NEEDS_CLARIFICATION"
    assert response.intent == PrimaryIntent.FORECAST_ANALYSIS
    assert "forecast_history_range" in response.missing_slots


@pytest.mark.asyncio
async def test_workflow_detail_runs_while_platform_permission_module_is_deferred():
    """当前明确不启用权限模块，明细仍走受控查询和脱敏链。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81,
            application_id="test-app",
            conversation_id="c4", message_id="m1",
            question="查询昨天订单明细，显示订单号和金额",
        ),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    assert result["response"].status == "COMPLETED"


@pytest.mark.asyncio
async def test_workflow_routes_metric_definition_skip_execute():
    """指标口径查询跳过 execute 节点，直接到 compose_answer。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81, application_id="test-app", conversation_id="c5", message_id="m1", question="销售额的口径是什么"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    assert result["response"].status == "COMPLETED"
    assert result["response"].intent == PrimaryIntent.METRIC_DEFINITION
    # 走完整 semantic+policy 但不经过 execute，evidence 应包含 definition
    assert any(e.kind == "METRIC_DEFINITION" for e in result["response"].evidence)


# 8 种高级分析意图的端到端覆盖
# 问题中带"本月"是为了让 _time_range 识别到时间范围，避免 NEEDS_CLARIFICATION。
_ADVANCED_INTENT_CASES = [
    (PrimaryIntent.TREND_ANALYSIS, "分析本月销售额的趋势", "PARTIAL_SUCCESS"),
    (PrimaryIntent.COMPARISON_ANALYSIS, "对比本月销售额的同比", "PARTIAL_SUCCESS"),
    (PrimaryIntent.COMPOSITION_ANALYSIS, "按区域分析本月销售额的构成占比", "PARTIAL_SUCCESS"),
    (PrimaryIntent.ANOMALY_ANALYSIS, "检测本月销售额的异常", "PARTIAL_SUCCESS"),
    (PrimaryIntent.ROOT_CAUSE_ANALYSIS, "归因本月销售额的异常原因", "PARTIAL_SUCCESS"),
    (PrimaryIntent.FORECAST_ANALYSIS, "预测下个月销售额", "NEEDS_CLARIFICATION"),
    (PrimaryIntent.REPORT_GENERATION, "生成本月销售额的分析报告", "COMPLETED"),
    (PrimaryIntent.DATA_QUALITY, "评估本月销售额的数据质量", "COMPLETED"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,question,expected_status", _ADVANCED_INTENT_CASES)
async def test_workflow_advanced_intents_obey_data_and_input_gates(
    intent: PrimaryIntent, question: str, expected_status: str
):
    """高级分析必须服从样本量、完整性和证据门禁。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(semantic_model_id=81, application_id="test-app", conversation_id=f"c-{intent.value}", message_id="m1", question=question),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    response = result["response"]
    assert response.status == expected_status
    assert response.intent == intent
    if expected_status == "COMPLETED":
        assert any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
        assert response.reliability is not None
    elif expected_status == "PARTIAL_SUCCESS":
        # “有数据但分析能力受限”的部分成功必须携带结构化降级事实，
        # 不得伪造完整分析结论：
        # 1) 可靠性门禁明确查询成功但样本不足且已扣留结论；
        # 2) warnings 给出按意图的最小行数缺口（降级原因）；
        # 3) 证据只含真实查询结果，不含 ANALYSIS_RESULT。
        gates = response.reliability.gates
        assert gates["query_succeeded"] is True
        assert gates["analysis_data_sufficient"] is False
        assert gates["analysis_conclusion_withheld"] is True
        assert response.reliability.level == "LIMITED"
        assert response.reliability.warnings
        assert all("少于" in warning for warning in response.reliability.warnings)
        assert any(e.kind == "QUERY_RESULT" for e in response.evidence)
        assert not any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
        assert "数据充足性说明" in response.answer
    else:
        assert not any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
