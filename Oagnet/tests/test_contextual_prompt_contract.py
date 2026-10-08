"""Prompt conflicts: keep execution contracts, not keyword-driven business policy."""
import json
from types import SimpleNamespace

import pytest

import agent
from prompt_build import PromptBuilder, SYSTEM_PROMPT
from test_temporal_metric_invariants import _asl, _sales_knowledge


@pytest.mark.parametrize('sections', [('entities',), ('entities','metrics'), ('entities','dimensions')])
def test_partial_recall_does_not_force_empty_catalog_clarification(sections):
    builder = object.__new__(PromptBuilder)
    builder.preferred_metric_codes = []
    builder.business_domain_ids = [205]
    builder.retrieve = lambda _: {s:[SimpleNamespace(metadata={'type':s, 'name':'available'})] for s in sections}
    builder._format_section = lambda records: 'available' if records else '(无召回)'
    prompt = builder.build('当前问题')
    assert '【目录提示】' in prompt
    assert '请严格遵守 System Prompt 第 10 条' not in prompt


def test_completely_empty_recall_still_forbids_invented_fields():
    builder = object.__new__(PromptBuilder)
    builder.preferred_metric_codes = []
    builder.business_domain_ids = [205]
    builder.retrieve = lambda _: {}
    builder._format_section = lambda records: '(无召回)'
    assert '请严格遵守 System Prompt 第 10 条' in builder.build('当前问题')


def test_prompt_retains_binding_contract_without_old_forced_business_examples():
    assert '### 示例 1' not in SYSTEM_PROMPT
    assert '名称为自由文本时优先使用' not in SYSTEM_PROMPT
    assert '用户明确说“含税销售总额 / 销量”等规范名称后才可唯一选择' not in SYSTEM_PROMPT
    assert '【输入分工】' in SYSTEM_PROMPT
    assert '不能要求 metrics[0] 必须存在' in SYSTEM_PROMPT
    assert '所选 `metric_code` 对应的 `metric_name`' in SYSTEM_PROMPT
    assert '缺少某个非必要展示属性' in SYSTEM_PROMPT
    assert '字段仍必须绑定到本轮目录' in SYSTEM_PROMPT


def test_contextual_selection_does_not_inject_keyword_sales_ambiguity():
    knowledge = _sales_knowledge()
    knowledge['_contextual_metric_selection'] = True
    ast = _asl()
    agent._normalize_generic_sales_metric(ast, knowledge, '上海的销售趋势')
    assert ast['ambiguity'] == []
    ast['ambiguity'] = [{'type':'metric','question':'确实缺少金额或数量口径','candidates':[]}]
    agent._normalize_generic_sales_metric(ast, knowledge, '上海的销售趋势')
    assert ast['ambiguity']  # Do not erase genuine model uncertainty.


def test_negated_metric_mention_is_not_a_required_output_in_contextual_path():
    knowledge = _sales_knowledge()
    knowledge['_contextual_metric_selection'] = True
    ast = _asl(metrics=[{'name':'sales_total_quantity','alias':'销售总数量'}])
    actual = json.loads(agent._validate_asl_output(json.dumps(ast), knowledge,
                                                 '不要含税销售总额，只看销售总数量'))
    assert [m['name'] for m in actual['metrics']] == ['sales_total_quantity']


def test_contextual_model_cannot_remove_caller_bound_metric_or_use_unknown_code():
    knowledge = _sales_knowledge()
    knowledge['_contextual_metric_selection'] = True
    with pytest.raises(ValueError, match='omitted metrics'):
        agent._validate_asl_output(json.dumps(_asl(metrics=[])), knowledge, '问题', ['annual_total_sales'])
    with pytest.raises(ValueError, match='not retrieved'):
        agent._validate_asl_output(json.dumps(_asl(metrics=[{'name':'unknown'}])), knowledge, '问题')
