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
        "chat": ChatRequest(application_id="test-app", conversation_id="c1", message_id="m1", question="你好"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    assert result["response"].status == "COMPLETED"
    assert result["response"].intent == PrimaryIntent.CHAT


@pytest.mark.asyncio
async def test_workflow_clarify_then_complete():
    """缺槽位先澄清，补充后走完整链路。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")

    first = await workflow.ainvoke({
        "chat": ChatRequest(application_id="test-app", conversation_id="c2", message_id="m1", question="帮我查一下销售额"),
        "identity": identity,
    })
    assert first["response"].status == "NEEDS_CLARIFICATION"
    assert "time_range" in first["response"].missing_slots

    second = await workflow.ainvoke({
        "chat": ChatRequest(application_id="test-app", conversation_id="c2", message_id="m2", question="本月"),
        "identity": identity,
    })
    assert second["response"].status == "COMPLETED"
    assert second["response"].intent == PrimaryIntent.METRIC_QUERY


@pytest.mark.asyncio
async def test_workflow_forecast_requires_history_before_execution():
    """预测不能只凭目标期硬算，必须先补充历史建模范围。"""
    orchestrator = make_orchestrator()
    workflow = build_workflow(orchestrator)
    result = await workflow.ainvoke({
        "chat": ChatRequest(application_id="test-app", conversation_id="c3", message_id="m1", question="预测下个月销售额"),
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
        "chat": ChatRequest(
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
        "chat": ChatRequest(application_id="test-app", conversation_id="c5", message_id="m1", question="销售额的口径是什么"),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    assert result["response"].status == "COMPLETED"
    assert result["response"].intent == PrimaryIntent.METRIC_DEFINITION
    # 走完整 semantic+policy 但不经过 execute，evidence 应包含 definition
    assert any(e.kind == "METRIC_DEFINITION" for e in result["response"].evidence)


# 8 种高级分析意图的端到端覆盖
# 问题中带"本月"是为了让 _time_range 识别到时间范围，避免 NEEDS_CLARIFICATION。
_ADVANCED_INTENT_CASES = [
    (PrimaryIntent.TREND_ANALYSIS, "分析本月销售额的趋势", "SAFE_FALLBACK"),
    (PrimaryIntent.COMPARISON_ANALYSIS, "对比本月销售额的同比", "SAFE_FALLBACK"),
    (PrimaryIntent.COMPOSITION_ANALYSIS, "按区域分析本月销售额的构成占比", "SAFE_FALLBACK"),
    (PrimaryIntent.ANOMALY_ANALYSIS, "检测本月销售额的异常", "SAFE_FALLBACK"),
    (PrimaryIntent.ROOT_CAUSE_ANALYSIS, "归因本月销售额的异常原因", "SAFE_FALLBACK"),
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
        "chat": ChatRequest(application_id="test-app", conversation_id=f"c-{intent.value}", message_id="m1", question=question),
        "identity": TrustedIdentity(tenant_id="t1", user_id="u1"),
    })
    response = result["response"]
    assert response.status == expected_status
    assert response.intent == intent
    if expected_status == "COMPLETED":
        assert any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
        assert response.reliability is not None
    else:
        assert not any(e.kind == "ANALYSIS_RESULT" for e in response.evidence)
