from datetime import date

import pytest

from app.domain.models import TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.planning.task_dag import extraction_user_prompt


@pytest.mark.parametrize('question', [
    '查询上海地区各经销商的已合作医院数',
    '查询上海地区的区域全部医院总数',
    '查询销售额排名前5的经销商',
    '分析上海地区销售趋势',
    '查询上海地区的销售订单明细',
    '列出上海地区正在销售费森尤斯产品的经销商',
])
def test_no_user_period_means_no_implicit_year_or_time_clarification(question):
    request = RuleBasedIntentClassifier().classify(question,
        TrustedIdentity(tenant_id='test',user_id='test'),'no-default-time')
    assert request.time_range is None
    assert 'time_range' not in request.missing_slots
    assert not any(a.startswith(('DEFAULT_TIME_RANGE=', 'ACTIVE_TIME_DEFAULT=')) for a in request.assumptions)


@pytest.mark.parametrize('question', ['今年上海销售额', '最近一年上海销售额', '2025年上海销售额'])
def test_explicit_period_survives(question):
    request = RuleBasedIntentClassifier().classify(question,
        TrustedIdentity(tenant_id='test',user_id='test'),'explicit-time')
    assert request.time_range is not None
    if question.startswith('2025'):
        assert request.time_range.start == date(2025,1,1)
        assert request.time_range.end_exclusive == date(2026,1,1)


def test_description_sent_to_extraction_no_longer_defaults_to_twelve_months():
    prompt = extraction_user_prompt('查询销售额')
    assert '默认取最近12个月' not in prompt
    assert '默认取最近 12 个月' not in prompt
    assert '不添加默认时间筛选' in prompt
