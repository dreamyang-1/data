"""Restore scoped question generation with provisional planner slots as context."""
import json
from copy import deepcopy
from types import SimpleNamespace as Obj

import pytest
import agent
from test_scalar_metric_grain import catalog, draft


def install(monkeypatch, ast=None):
    seen = []
    class Builder:
        def __init__(self, *args, **kwargs):
            assert kwargs['semantic_model_id'] == 81
            assert kwargs['business_domain_ids'] == [205]
            self.last_knowledge = catalog()
        def build(self, question):
            seen.append(('question', question))
            return 'Existing scoped generation prompt'
    def invoke(messages):
        seen.append(('messages', messages))
        return Obj(content=json.dumps(deepcopy(ast if ast is not None else draft()), ensure_ascii=False))
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Obj(invoke=invoke))
    return seen


def test_missing_extraction_can_generate_from_completed_question(monkeypatch):
    seen = install(monkeypatch)
    result = json.loads(agent.main('裸序号', completed_question='查询上海医院总数',
        store=object(), semantic_model_id=81, business_domain_ids=[205]))
    assert not result['ambiguity'] and result['metrics'] == draft()['metrics']
    assert seen[0] == ('question', '查询上海医院总数')
    messages = seen[1][1]
    assert messages[0]['content'] == 'Existing scoped generation prompt'
    assert json.loads(messages[1]['content'])['completed_question'] == '查询上海医院总数'


def test_provisional_grain_error_does_not_block_question_generation(monkeypatch):
    import structured_binding
    seen = install(monkeypatch)
    monkeypatch.setattr(structured_binding, 'bind', lambda *args, **kwargs: pytest.fail('valid original generation must run first'))
    result = json.loads(agent.main('查询上海医院总数', store=object(), semantic_model_id=81,
        business_domain_ids=[205], structured_extraction={'指标': [], '维度': [], '展示字段': [{'field': '名称'}]}))
    assert not result['ambiguity'] and seen[0] == ('question', '查询上海医院总数')


@pytest.mark.parametrize('change', ['metric', 'field', 'subject'])
def test_recovery_cannot_authorize_unknown_catalog_items(monkeypatch, change):
    ast = draft()
    if change == 'metric': ast['metrics'][0]['name'] = 'outside_metric'
    elif change == 'field': ast['filters'][0]['field'] = 'outside.private_column'
    else: ast['subject']['entity'] = 'outside_entity'
    install(monkeypatch, ast)
    result = json.loads(agent.main('查询上海医院总数', store=object(),
        semantic_model_id=81, business_domain_ids=[205]))
    assert result['ambiguity'] and result['metrics'] == []


def test_recovery_retains_confirmed_caller_constraints(monkeypatch):
    seen = install(monkeypatch)
    def validate(content, contract, knowledge):
        assert contract == {'confirmed': 'original-choice'}
        raise ValueError('omitted confirmed choice')
    monkeypatch.setattr(agent, '_validate_intent_asl_contract', validate)
    result = json.loads(agent.main('查询医院总数', store=object(), semantic_model_id=81,
        business_domain_ids=[205], intent_asl_contract={'confirmed': 'original-choice'}))
    assert result['ambiguity'] and seen


def test_question_generation_receives_existing_analysis_requirements(monkeypatch):
    seen = install(monkeypatch)
    contract = {'required_columns': {'count': ['医院数']}, 'minimum_rows': 1}
    exploration = {'minimum_numeric_metrics': 1, 'maximum_numeric_metrics': 1,
                   'minimum_dimensions': 0, 'maximum_dimensions': 0}
    result = json.loads(agent.main('查询医院总数', store=object(), semantic_model_id=81,
        business_domain_ids=[205], analysis_operator='existing-analysis',
        result_contract=contract, exploration_requirements=exploration))
    context = json.loads(seen[1][1][1]['content'])
    assert context['result_contract'] == contract and context['exploration_requirements'] == exploration
    assert result['analysis_contract']['result_contract'] == contract
    assert not result['ambiguity']


def test_recovery_outage_retains_original_failure_and_redacts_errors(monkeypatch, caplog):
    install(monkeypatch)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Obj(invoke=lambda *_: (_ for _ in ()).throw(RuntimeError('private-token-fixture'))))
    result = json.loads(agent.main('查询医院总数', store=object(), semantic_model_id=81,
        business_domain_ids=[205]))
    assert result['ambiguity'] and 'private-token-fixture' not in json.dumps(result)


@pytest.mark.parametrize('left', ['total_hospitals_in_region', '区域全部医院总数',
    'TIMESTAMPDIFF(MONTH,hospital.first_order_date,CURRENT_DATE)'])
def test_registered_scalar_metric_threshold_uses_full_catalog_formula(left):
    knowledge = catalog()
    formula = 'TIMESTAMPDIFF(MONTH,hospital.first_order_date,CURRENT_DATE)'
    knowledge['metrics'][0].metadata['calculation_rule']['calc_formula'] = 'total_hospitals_in_region=' + formula
    ast = draft();ast['having'] = [left + ' > 12']
    result = json.loads(agent._validate_asl_output(json.dumps(ast), knowledge))
    assert result['having'] == [formula + ' > 12']


@pytest.mark.parametrize('expression', ['outside_metric > 12',
    'total_hospitals_in_region > 12 OR 1=1', 'total_hospitals_in_region > 12; SELECT 1'])
def test_permissive_metric_threshold_does_not_accept_unregistered_sql(expression):
    knowledge = catalog();ast = draft();ast['having'] = [expression]
    with pytest.raises(ValueError, match='having'):
        agent._validate_asl_output(json.dumps(ast), knowledge)
