"""A valid monthly query must survive interpretation inside a compound task."""
import pytest

from app.analysis.engine import AnalysisEngine
from app.analysis.interpretation import AnswerPlanner, InsightInterpretationLayer
from app.domain.models import CanonicalAnalysisRequest, KnowledgeContext, MetricRef, PrimaryIntent


@pytest.mark.parametrize('values', [[10, 11, 12, 13, 113, 114], [200, 199, 198, 197, 97, 96]])
def test_monthly_query_with_one_large_change_finishes_with_grounded_insight(values):
    request = CanonicalAnalysisRequest(conversation_id='monthly-task', tenant_id='t', user_id='u',
        original_question='统计2025年月度订单笔数', primary_intent=PrimaryIntent.TREND_ANALYSIS,
        metrics=[MetricRef(input='订单笔数', canonical_name='订单笔数')])
    analysis = AnalysisEngine().analyze(request, ['月份', '订单笔数'],
        [{'月份': f'2025-{index:02}', '订单笔数': value} for index, value in enumerate(values, 1)],
        KnowledgeContext(query=''))
    interpreted = InsightInterpretationLayer().interpret(request, analysis)
    assert len(interpreted.abnormal_changes) == 1
    assert abs(interpreted.abnormal_changes[0]['absolute_change']) == 100
    answer = AnswerPlanner().plan(interpreted).render()
    assert '2025-04至2025-05变化100.00' in answer
