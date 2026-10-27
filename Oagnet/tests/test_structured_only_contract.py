import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest
import agent
from test_structured_binding import fixture, run


def test_missing_extraction_never_calls_model_or_question_generator(monkeypatch):
    monkeypatch.setattr(agent, '_get_chat_model', lambda: pytest.fail('must not generate'))
    monkeypatch.setattr(agent, 'PromptBuilder', lambda *a, **k: pytest.fail('must not retrieve'))
    result = json.loads(agent.main('查询上海医院总数', semantic_model_id=81, store=object()))
    assert result['ambiguity'] and not result['metrics']


@pytest.mark.parametrize('aggregate', [False, True])
def test_same_field_can_have_both_roles_without_changing_grouping(aggregate):
    extraction, knowledge, plan = fixture()
    extraction.update(维度=[{'field': '医院名称'}], 展示字段=[{'field': '医院名称'}])
    plan.update(dimensions=[{'index': 0, 'key': 'hospital.name'}],
                display_fields=[{'index': 0, 'key': 'hospital.name'}])
    if not aggregate:
        extraction['指标'] = []; plan['metrics'] = []
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity']
    assert [d['name'] for d in ast['dimensions']] == ['hospital.name']
    assert [d['name'] for d in ast['display_fields']] == ['hospital.name']


def test_display_only_detail_does_not_create_grouping():
    extraction, knowledge, plan = fixture()
    extraction.update(指标=[], 展示字段=[{'field': '医院名称'}])
    plan.update(metrics=[], display_fields=[{'index': 0, 'key': 'hospital.name'}])
    ast, _ = run(extraction, knowledge, plan)
    assert not ast['ambiguity'] and ast['dimensions'] == []
    assert ast['display_fields'][0]['name'] == 'hospital.name'


def test_sort_cannot_add_undeclared_metric():
    extraction, knowledge, plan = fixture()
    extraction.update(指标=[], 展示字段=[{'field': '医院名称'}], 排序=[{'field':'医院总数','order':'desc'}])
    plan.update(metrics=[], display_fields=[{'index':0,'key':'hospital.name'}],
                sort=[{'index':0,'key':'count_hospital'}])
    ast, _ = run(extraction, knowledge, plan)
    assert ast['metrics'] == [] and ast['ambiguity']


def test_empty_filter_is_not_dropped_to_broaden_query():
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'][0].update(op='in', value=[])
    plan['filters'][0].pop('value_ids')
    ast, repairs = run(extraction, knowledge, plan)
    assert ast['ambiguity']
    assert not any(r['type'] == 'EMPTY_FILTER_VALUE_DROPPED' for r in repairs)


@pytest.mark.parametrize('extra', ['filters', 'required_groupings', 'sorting'])
def test_contract_does_not_add_missing_slots(monkeypatch, extra):
    extraction, knowledge, plan = fixture()
    extraction['过滤条件'] = []; plan['filters'] = []
    before = deepcopy(extraction)
    class Builder:
        def __init__(self, *a, **k): pass
        def retrieve(self, _): return knowledge
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, 'canonicalize_structured_mentions', lambda *a: [])
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Obj(invoke=lambda _: Obj(content=json.dumps(plan))))
    contract = {'intent':'METRIC_QUERY','metric_required':True,'required_metrics':['医院总数']}
    contract[extra] = {
        'filters':[{'field':'省份','operator':'=','value':'上海市'}],
        'required_groupings':['医院名称'],
        'sorting':{'required':True,'direction':'DESC','limit':3},
    }[extra]
    result = json.loads(agent.main('ignored', semantic_model_id=81, store=object(),
        structured_extraction=extraction, intent_asl_contract=contract))
    assert result['ambiguity']
    assert result['filters'] == result['dimensions'] == [] and result['sort'] is None
    assert extraction == before
