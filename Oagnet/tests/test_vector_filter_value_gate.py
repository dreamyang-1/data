"""Frozen user contract: ASL business values are vector standards before SQL."""
import json
from copy import deepcopy
from types import SimpleNamespace as Doc

import pytest

import agent
from asl_contract import ASLValidationError
from vector_filter_grounding import ground_filter_values


FIELD = 'dim_province.province_name'


def catalog(values=('上海市', '北京市', '江苏省')):
    return {'_vector_authorized_fields': [FIELD, 'dealer.dealer_name'],
        'entities': [Doc(metadata={'entity_code': 'dealer', 'attributes': [
            {'attr_code': 'dealer_name', 'field_mapping': 'dealer.dealer_name'}]}),
            Doc(metadata={'entity_code': 'province', 'attributes': [
                {'attr_code': 'province_name', 'field_mapping': FIELD}]})],
        'entity_attribute_values': [Doc(metadata={'source_table': 'dim_province',
            'source_field': 'province_name', 'attr_value': value,
            'semantic_model_id': 130, 'business_domain_id': 296}) for value in values]}


def draft(value='上海', operator='='):
    return {'version': '2.0', 'intent': 'query', 'subject': {'entity': 'dealer'},
        'metrics': [], 'dimensions': [{'name': 'dealer.dealer_name'}],
        'filters': [{'field': FIELD, 'operator': operator, 'value': value}],
        'time_context': None, 'sort': None, 'limit': 1000, 'having': [], 'ambiguity': []}


@pytest.mark.parametrize('operator', ['=', '!=', 'IN', 'NOT IN'])
@pytest.mark.parametrize('surface,canonical', [('上海', '上海市'), ('北京', '北京市'), ('江苏', '江苏省')])
def test_each_operator_uses_real_same_field_standard(operator, surface, canonical):
    ast = draft([surface] if operator in {'IN', 'NOT IN'} else surface, operator)
    k = catalog()
    repairs = ground_filter_values(ast, k)
    assert ast['filters'][0]['value'] == ([canonical] if isinstance(ast['filters'][0]['value'], list) else canonical)
    assert repairs[0]['source'] == 'VECTOR_CATALOG'
    agent._validate_vector_grounded_asl(json.dumps(ast), k)


def test_actual_vector_standard_wins_over_geographic_common_sense():
    ast = draft()
    assert ground_filter_values(ast, catalog(('上海',))) == []
    assert ast['filters'][0]['value'] == '上海'


def test_gate_rejects_uncorrected_value_even_when_field_is_valid():
    with pytest.raises(ASLValidationError) as caught:
        agent._validate_vector_grounded_asl(json.dumps(draft()), catalog())
    assert caught.value.code == 'ASL_FILTER_VALUE_UNGROUNDED'


@pytest.mark.parametrize('values', [(), ('上海市', '上海省')])
def test_missing_or_ambiguous_standard_is_never_pass(values):
    with pytest.raises(ASLValidationError):
        ground_filter_values(draft(), catalog(values))


def test_partial_in_binding_cannot_drop_unmatched_member():
    ast = draft(['上海', '未知地区'], 'IN')
    with pytest.raises(ASLValidationError):
        ground_filter_values(ast, catalog())
    assert len(ast['filters'][0]['value']) == 2


def test_other_field_value_cannot_authorize_the_selected_field():
    k = catalog(())
    k['entity_attribute_values'] = [Doc(metadata={'source_field': 'dealer.dealer_name', 'attr_value': '上海市'})]
    with pytest.raises(ASLValidationError):
        ground_filter_values(draft(), k)


def test_related_filter_values_use_the_same_gate():
    ast = draft()
    ast['related_filters'] = [{'target_filters': ast.pop('filters')}]
    ground_filter_values(ast, catalog())
    assert ast['related_filters'][0]['target_filters'][0]['value'] == '上海市'


@pytest.mark.parametrize('operator', ['>', '>=', '<', '<=', 'BETWEEN'])
def test_numeric_threshold_literal_passes_without_type_metadata(operator):
    # 数值阈值字面量直通：量纲连续值没有向量标准值（数值列不发布值向量），
    # 类型元数据缺失（如 130 目录 sales_order.quantity data_type=null）
    # 时不得误送向量匹配把合法阈值拦停成歧义（Q56 实测）。
    ast = draft(['0'] if operator == 'BETWEEN' else '0', operator)
    if operator == 'BETWEEN':
        ast['filters'][0]['value'] = ['0', '999']
    ast['filters'][0]['field'] = 'sales_order.quantity'
    k = catalog(())
    k['_vector_authorized_fields'] = ['sales_order.quantity']
    assert ground_filter_values(ast, k) == []
    assert ast['filters'][0]['value'] == (['0', '999'] if operator == 'BETWEEN' else '0')


def test_numeric_type_metadata_exempts_equality_too():
    # 类型元数据声明数值型（含 semantic_type 证据）时，等号数值同样直通。
    ast = draft('3', '=')
    ast['filters'][0]['field'] = 'sales_order.quantity'
    k = catalog(())
    k['_vector_authorized_fields'] = ['sales_order.quantity']
    k['entities'] = [Doc(metadata={'entity_code': 'sales_order', 'attributes': [
        {'attr_code': 'quantity', 'field_mapping': 'sales_order.quantity',
         'data_type': 'INT'}]})]
    assert ground_filter_values(ast, k) == []
    assert ast['filters'][0]['value'] == '3'


def test_equality_numeric_without_type_evidence_still_requires_grounding():
    # 等号数字可能是枚举/编码值：类型证据缺失时保守走向量匹配。
    ast = draft('1', '=')
    ast['filters'][0]['field'] = 'sales_order.status'
    k = catalog(())
    k['_vector_authorized_fields'] = ['sales_order.status']
    with pytest.raises(ASLValidationError):
        ground_filter_values(ast, k)


@pytest.mark.parametrize('field_format', ['qualified', 'bare'])
def test_missing_recall_uses_same_model_domain_and_both_published_field_formats(field_format):
    k = catalog(())
    record = deepcopy(catalog(('上海市',))['entity_attribute_values'][0])
    if field_format == 'qualified':
        record.metadata['source_field'] = FIELD
    calls = []
    class Store:
        def search(self, vector, *, top_k, where):
            calls.append(where)
            assert vector == [1] and top_k == 20
            assert {'semantic_model_id': 130} in where['$and'][0]['$and']
            assert {'business_domain_id': {'$in': [296]}} in where['$and'][0]['$and']
            assert {'source_field': FIELD} in where['$and'][1]['$or']
            return [record]
    ast = draft()
    ground_filter_values(ast, k, store=Store(), embed=lambda value: [1], semantic_model_id=130, domain_ids=[296])
    assert ast['filters'][0]['value'] == '上海市' and len(calls) == 1


@pytest.mark.parametrize('change', ['model', 'domain', 'field'])
def test_vector_backend_scope_or_field_mismatch_fails_closed(change):
    record = deepcopy(catalog(('上海市',))['entity_attribute_values'][0])
    record.metadata[{'model':'semantic_model_id','domain':'business_domain_id','field':'source_field'}[change]] = {'model':81,'domain':205,'field':'private_name'}[change]
    with pytest.raises(ValueError):
        ground_filter_values(draft(), catalog(()), store=Doc(search=lambda *a, **k: [record]),
            embed=lambda value: [1], semantic_model_id=130, domain_ids=[296])


def test_outage_never_turns_into_successful_literal_fallback():
    def failed(*args, **kwargs):
        raise ConnectionError('offline vector outage')
    with pytest.raises(ConnectionError):
        ground_filter_values(draft(), catalog(()), store=Doc(search=failed),
            embed=lambda value: [1], semantic_model_id=130, domain_ids=[])


@pytest.mark.parametrize('value', [12, 0, 1.5])
def test_numeric_thresholds_keep_typed_values(value):
    ast = draft(value)
    ground_filter_values(ast, catalog(()))
    assert ast['filters'][0]['value'] == value


@pytest.mark.parametrize('operator', ['=', '>', '<=', 'BETWEEN'])
def test_numeric_strings_follow_vector_field_type(operator):
    k = catalog(())
    k['entities'][1].metadata['attributes'][0]['data_type'] = 'DECIMAL(18,2)'
    value = ['0', '12.5'] if operator == 'BETWEEN' else '12.5'
    ast = draft(value, operator)
    ground_filter_values(ast, k)
    assert ast['filters'][0]['value'] == value


def test_like_wildcards_preserve_pattern_around_standard_business_term():
    ast = draft('%上海%', 'LIKE')
    ground_filter_values(ast, catalog())
    assert ast['filters'][0]['value'] == '%上海市%'
    agent._validate_vector_grounded_asl(json.dumps(ast), catalog())


def test_comparison_cannot_bypass_standard_value_gate():
    with pytest.raises(ASLValidationError):
        ground_filter_values(draft('不存在的省份', '>'), catalog())


PROSE_VALUE = '2025年销售额占比前3的产品线下各'


def test_hallucinated_prose_filter_value_is_dropped_with_receipt():
    # Q60 实测：ASL 生成 LLM 把任务问题的叙述片段幻觉成等值过滤值（规划期
    # 提取的过滤条件为空、值在执行期生成）。该值永远无法通过向量标准值绑定，
    # 零候选+多重叙述特征时在绑定闸口剥离并留 repair 记录，任务不再被拦停；
    # 不猜标准值，真实范围限制由依赖约束在 SQL 翻译阶段另行注入。
    ast = draft(PROSE_VALUE)
    k = catalog(())
    repairs = ground_filter_values(ast, k)
    assert ast['filters'] == []
    assert [r['type'] for r in repairs] == ['PROSE_PLACEHOLDER_FILTER_DROPPED']
    assert repairs[0]['resolved_field'] == FIELD and repairs[0]['mention'] == PROSE_VALUE
    # 复检（validate_only）看到的是干净 AST，不再拦停
    agent._validate_vector_grounded_asl(json.dumps(ast), k)


def test_prose_element_dropped_from_list_while_real_value_grounds():
    ast = draft(['上海市', PROSE_VALUE], 'IN')
    k = catalog(('上海市',))
    repairs = ground_filter_values(ast, k)
    assert ast['filters'][0]['value'] == ['上海市']
    assert [r['type'] for r in repairs] == ['PROSE_PLACEHOLDER_FILTER_DROPPED']


def test_validate_only_still_fails_closed_on_prose_value():
    # 干跑校验不剥离：绑定闸口负责修复，校验闸口负责如实报告。
    with pytest.raises(ASLValidationError):
        ground_filter_values(draft(PROSE_VALUE), catalog(()), validate_only=True)


@pytest.mark.parametrize('value', ['费森尤斯导管', '不存在的省份名称'])
def test_zero_candidate_value_without_prose_markers_still_fails_closed(value):
    # 零候选但无散文特征（可能是漏登记的真实业务值）保持原 fail-closed。
    with pytest.raises(ASLValidationError):
        ground_filter_values(draft(value), catalog(()))


def test_identifier_and_date_literals_keep_their_declared_types():
    k = catalog(())
    attrs = k['entities'][1].metadata['attributes']
    attrs.extend([{'field_mapping': 'dim_province.province_code'},
                  {'field_mapping': 'dim_province.date', 'data_type': 'DATE'}])
    k['_vector_authorized_fields'].extend(['dim_province.province_code','dim_province.date'])
    ast = draft()
    ast['filters'] = [{'field':'dim_province.province_code','operator':'=','value':'00123'},
                      {'field':'dim_province.date','operator':'=','value':'2025-01-01'}]
    ground_filter_values(ast, k)
    assert ast['filters'][0]['value'] == '00123'
    assert ast['filters'][1]['value'] == '2025-01-01'


FISCAL_MONTH = 'sales_order.fiscal_month'


def _month_catalog():
    k = catalog(())
    # fiscal_month 已发布元数据不含日期/时间标记（复现 2026-10-21 线上目录形态）
    k['entities'][1].metadata['attributes'].append({'field_mapping': FISCAL_MONTH})
    k['_vector_authorized_fields'].append(FISCAL_MONTH)
    return k


@pytest.mark.parametrize('bounds', [
    ['2025-10-01', '2025-10-31'], ['2025-10', '2025-10'], ['2025年10月', '2025年10月']])
def test_month_field_date_bounds_are_literal_thresholds_not_business_terms(bounds):
    ast = draft()
    ast['filters'] = [{'field': FISCAL_MONTH, 'operator': 'BETWEEN', 'value': bounds}]
    ground_filter_values(ast, _month_catalog())
    assert ast['filters'][0]['value'] == bounds


def test_month_field_business_term_still_requires_vector_grounding():
    ast = draft()
    ast['filters'] = [{'field': FISCAL_MONTH, 'operator': '=', 'value': '10月'}]
    with pytest.raises(ASLValidationError):
        ground_filter_values(ast, _month_catalog())


def test_original_question_generation_is_grounded_before_returning_pass(monkeypatch):
    k = catalog()
    class Builder:
        def __init__(self, *args, **kwargs): self.last_knowledge = deepcopy(k)
        def build(self, question): return 'scoped vector catalog'
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Doc(invoke=lambda messages: Doc(content=json.dumps(draft()))))
    response = agent.main('上海的经销商有哪些', store=object(), semantic_model_id=130,
                          business_domain_ids=[], include_evidence=True)
    assert response['asl_validation'] == 'PASS'
    assert json.loads(response['result'])['filters'][0]['value'] == '上海市'
    assert any(r['type'] == 'ADD_SOURCE_RESOLVED_ENTITY_FILTER' and r['mention'] == '上海'
               and r['resolved_field'] == FIELD and r['canonical_value'] == '上海市'
               for r in response['asl_repair'])


@pytest.mark.parametrize('model_value', ['上海', '上海市'])
def test_contract_checks_compare_canonical_value_after_grounding(monkeypatch, model_value):
    k = catalog()
    class Builder:
        def __init__(self, *args, **kwargs): self.last_knowledge = deepcopy(k)
        def build(self, question): return 'scoped vector catalog'
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: Doc(invoke=lambda messages: Doc(content=json.dumps(draft(model_value)))))
    response = agent.main('上海的经销商有哪些', store=object(), semantic_model_id=130,
        business_domain_ids=[], include_evidence=True,
        intent_asl_contract={'filters': [{'field':FIELD,'operator':'=','value':'上海'}]})
    assert response['asl_validation'] == 'PASS'
    assert json.loads(response['result'])['filters'][0]['value'] == '上海市'
    assert any(r.get('mention') == '上海' and r.get('canonical_value') == '上海市'
               for r in response['asl_repair'])


@pytest.mark.parametrize('injected,passes', [('上海', True), ('不存在的地区', False)])
def test_late_contract_repair_cannot_bypass_fallback_value_gate(monkeypatch, injected, passes):
    import query_binding_review
    import structured_binding

    k = catalog()
    class Builder:
        def __init__(self, *args, **kwargs): pass
        def retrieve(self, text): return deepcopy(k)
    monkeypatch.setattr(agent, '_recover_question_asl', lambda *args, **kwargs: None)
    monkeypatch.setattr(agent, 'PromptBuilder', Builder)
    monkeypatch.setattr(agent, '_get_chat_model', lambda: object())
    monkeypatch.setattr(agent, 'embed_query', lambda value: [1])
    monkeypatch.setattr(structured_binding, 'bind', lambda *args, **kwargs: (draft('上海市'), []))
    monkeypatch.setattr(query_binding_review, 'review_bindings', lambda content, *args, **kwargs: (content, []))
    def late_repair(ast, *args, **kwargs):
        ast['filters'][0]['value'] = injected
    monkeypatch.setattr(agent, '_repair_contract_filter', late_repair)
    response = agent.main('上海的经销商有哪些', store=Doc(search=lambda *args, **kwargs: []), semantic_model_id=130,
        business_domain_ids=[], include_evidence=True, structured_extraction={'过滤条件': []},
        intent_asl_contract={'filters': [{'field': FIELD,'operator': '=','value': injected}]})
    assert (response['asl_validation'] == 'PASS') is passes
    ast = json.loads(response['result'])
    if passes:
        assert ast['filters'][0]['value'] == '上海市'
    else:
        assert ast['ambiguity']
