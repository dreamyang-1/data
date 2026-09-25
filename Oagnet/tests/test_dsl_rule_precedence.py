import json
from copy import deepcopy

import pytest

import agent
from prompt_build import PromptBuilder, SYSTEM_PROMPT
from test_temporal_metric_invariants import _asl, _sales_knowledge


def default_time():
    return {'type':'range','start':'2026-03-24','end':'2026-09-24',
            'unit':'day','anchor':'sales_order.created_date'}


def with_time_rule(index=0):
    knowledge = _sales_knowledge()
    knowledge['metrics'][index].metadata['time_caliber']['special_rule'] = {
        'description':'该指标未指定时间时按最近6个月统计',
    }
    return knowledge


def test_obsolete_selected_dsl_default_does_not_create_implicit_time():
    ast = _asl(time_context=default_time())
    agent._normalize_time_context(ast, with_time_rule(), '含税销售总额')
    assert ast['time_context'] is None
    agent._validate_asl_output(json.dumps(ast), with_time_rule(), '含税销售总额')


@pytest.mark.parametrize('knowledge', [_sales_knowledge(), with_time_rule(1)])
def test_missing_or_unselected_dsl_rule_does_not_authorize_implicit_time(knowledge):
    ast = _asl(time_context=default_time())
    agent._normalize_time_context(ast, deepcopy(knowledge), '含税销售总额')
    assert ast['time_context'] is None


def test_dsl_rule_cannot_authorize_an_invented_anchor():
    ast = _asl(time_context=dict(default_time(), anchor='private.secret'))
    agent._normalize_time_context(ast, with_time_rule(), '含税销售总额')
    assert ast['time_context'] is None


def test_explicit_user_dates_override_dsl_default():
    ast = _asl(time_context=default_time())
    agent._normalize_time_context(ast, with_time_rule(), '2025年10月17日至2025年12月30日含税销售总额')
    assert ast['time_context']['start'] == '2025-10-17'
    assert ast['time_context']['end'] == '2025-12-30'


def test_dsl_policy_is_rendered_instead_of_replaced_with_fixed_months():
    builder = object.__new__(PromptBuilder)
    rendered = builder._format_record(with_time_rule()['metrics'][0])
    assert '该指标未指定时间时按最近6个月统计' in rendered
    assert 'special_rule' in rendered
    assert 'DSL 没有定义时，不自行补最近一年、1000条' in SYSTEM_PROMPT
    assert '仅 Top N 场景填写，否则 null' not in SYSTEM_PROMPT
    assert '查询总量上限与页面预览条数、附件导出阈值不同' in SYSTEM_PROMPT


@pytest.mark.parametrize('limit', [25, 500, 1000])
def test_dsl_detail_limit_can_pass_existing_asl_schema_without_ranking(limit):
    from test_scalar_metric_grain import catalog, draft
    ast = draft([{'name':'hospital.hospital_name'}])
    ast['metrics'] = []
    ast['limit'] = limit
    actual = json.loads(agent._validate_asl_output(json.dumps(ast), catalog(), '医院名称'))
    assert actual['limit'] == limit
    assert actual['sort'] is None
