"""Do not present upstream guesses as the final meaning of a row-level query."""
import pytest

from app.domain.models import CanonicalAnalysisRequest, MetricRef, PrimaryIntent, SemanticAmbiguity
from app.services.orchestrator import DataAnalysisOrchestrator
from test_orchestrator import service


PROMPT = "## 核心指标\n| 指标 | 同义词 |\n|---|---|\n| 含税销售总额 | 销售额、订单金额 |"


@pytest.mark.parametrize('wording', ['单笔', '逐笔', '每笔', '每条', '逐条'])
def test_detail_amount_is_not_rewritten_as_aggregate_alias(wording):
    question = f'查询最近一年{wording}销售额大于1000的订单'
    request = CanonicalAnalysisRequest(conversation_id='row-comparison', tenant_id='t1', user_id='u1',
        original_question=question, rewritten_question=question, primary_intent=PrimaryIntent.DETAIL_QUERY,
        metrics=[MetricRef(input='销售额')])
    before = request.model_copy(deep=True)
    DataAnalysisOrchestrator._apply_platform_metric_vocabulary(request, PROMPT)
    assert request == before


def test_aggregate_metric_alias_still_applies():
    request = CanonicalAnalysisRequest(conversation_id='sum-comparison', tenant_id='t1', user_id='u1',
        original_question='查询销售额大于1000的城市', primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input='销售额')])
    DataAnalysisOrchestrator._apply_platform_metric_vocabulary(request, PROMPT)
    assert request.metrics[0].canonical_name == '含税销售总额'


@pytest.mark.asyncio
async def test_asl_clarification_does_not_claim_stale_metric_or_entity_was_validated():
    request = CanonicalAnalysisRequest(application_id='app1', conversation_id='detail-time-ambiguity',
        tenant_id='t1', user_id='u1', original_question='查询最近一年单笔销售额大于1000的订单',
        primary_intent=PrimaryIntent.DETAIL_QUERY, metrics=[MetricRef(input='含税销售总额')], entity='产品',
        missing_slots=['semantic_ambiguity'], semantic_ambiguities=[SemanticAmbiguity(
            type='time_anchor', question='请确认按订单日期还是付款日期筛选。',
            candidates=['订单日期', '付款日期'], affected_slots=['time_range'])])
    response = await service()._request_clarification(request, 1, source_stage='OAGNET_ASL_GENERATION')
    assert response.status == 'NEEDS_CLARIFICATION'
    assert '当前问题：查询最近一年单笔销售额大于1000的订单' in response.answer
    assert '指标=含税销售总额' not in response.answer
    assert '明细对象=产品' not in response.answer
    assert '订单日期' in response.answer and '付款日期' in response.answer
    assert '请回复序号或完整的候选名称' in response.answer
