"""Explain the actual missing/bad slot without filling it or weakening grounding."""
from copy import deepcopy
import pytest

from test_scalar_metric_detail_binding import fixture
from test_structured_binding import run


def reasons(ast):
    return '\n'.join(x['question'] for x in ast['ambiguity'])


def test_metric_filter_without_selected_metric_names_the_omission():
    e, k, p = fixture()
    e['指标'] = []
    p['metrics'] = []
    before = deepcopy(e)
    ast, _ = run(e, k, p)
    assert '指标列表为空，没有提取出“合作时长”指标' in reasons(ast)
    assert not ast['having'] and not ast['metrics']
    assert e == before
    assert '筛选公式或数值阈值' not in reasons(ast)


def test_other_selected_metric_cannot_satisfy_filter_metric():
    e, k, p = fixture()
    from types import SimpleNamespace as Obj
    k['metrics'].append(Obj(metadata={'metric_code': 'other', 'metric_name': '其他指标'}))
    e['指标'] = [{'name': '其他指标'}]
    p['metrics'] = [{'index': 0, 'key': 'other'}]
    ast, _ = run(e, k, p)
    assert '漏提了“合作时长”' in reasons(ast)
    assert not ast['having']


@pytest.mark.parametrize('section,target', [('维度','dimensions'), ('展示字段','display_fields')])
def test_metric_in_wrong_slot_is_not_reported_as_missing_configuration(section, target):
    e, k, p = fixture()
    e['指标'] = []
    e[section] = [{'name': '合作时长'}]
    p['metrics'] = []
    p[target] = [{'index': 0, 'key': 'duration'}]
    ast, _ = run(e, k, p)
    assert f'被放进了{section}' in reasons(ast)
    assert '提取位置有误' in reasons(ast)
    assert not ast['metrics'] and not ast['having']


def test_plain_field_in_metric_slot_names_wrong_position():
    e, k, p = fixture()
    e['指标'] = [{'name': '经销商名称'}]
    ast, _ = run(e, k, p)
    assert '“经销商名称”是普通字段，却被放进了指标' in reasons(ast)


@pytest.mark.parametrize('change,expected', [
    ({'field': ''}, '没有提取出字段名称'),
    ({'op': ''}, '没有提取出比较方式'),
    ({'op': 'UNKNOWN'}, '比较方式提取有问题'),
    ({'value': []}, '没有提取出完整的条件值'),
    ({'value': None}, '没有提取出完整的条件值'),
    ({'value': [' ']}, '没有提取出完整的条件值'),
    ({'value': [90, None]}, '没有提取出完整的条件值'),
    ({'value': {'number': 90}}, '条件值提取格式有问题'),
    ({'value': True}, '条件值提取格式有问题'),
    ({'value': [1, 2]}, '却提取出了多个值'),
    ({'value': [1], 'op': 'BETWEEN'}, '需要起点和终点两个值'),
    ({'value': ['九十']}, '需要一个用于比较的数字'),
    ({'value': [float('inf')]}, '需要一个用于比较的数字'),
])
def test_filter_parts_are_reported_separately(change, expected):
    e, k, p = fixture()
    e['过滤条件'][0].update(change)
    ast, _ = run(e, k, p)
    assert expected in reasons(ast)
    assert not ast['having'] and not ast['filters']


def test_zero_threshold_is_a_value_and_valid_scalar_metric_still_executes():
    e, k, p = fixture()
    e['过滤条件'][0]['value'] = [0]
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['having'] == ['DATEDIFF(CURRENT_DATE, dealer.first_order_date) > 0']


def test_attribute_filter_does_not_require_a_metric():
    e, k, p = fixture()
    e.update(指标=[], 排序=[], 过滤条件=[{'field': '经销商编码', 'op': '=', 'value': ['D001']}])
    p.update(metrics=[], sort=[], filters=[{'index': 0, 'key': 'dealer.dealer_code'}])
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
    assert ast['filters'][0]['value'] == 'D001'


def test_unsupported_formula_does_not_claim_metric_was_not_extracted():
    e, k, p = fixture()
    k['metrics'][0].metadata['calculation_rule']['calc_formula'] = 'duration=UNSUPPORTED(dealer.first_order_date)'
    ast, _ = run(e, k, p)
    assert '当前无法按已登记的计算公式完成筛选' in reasons(ast)
    assert '没有提取出“合作时长”指标' not in reasons(ast)


def test_sort_direction_names_the_missing_part():
    e, k, p = fixture()
    e['排序'][0].pop('order')
    ast, _ = run(e, k, p)
    assert '“合作时长”没有提取出排序方向' in reasons(ast)


def test_present_metric_with_failed_binding_is_not_reported_as_omitted():
    from structured_binding import _unselected_metric_reason, catalog_candidates
    e, k, _ = fixture()
    e['展示字段'].append({'name': '合作时长'})
    reason = _unselected_metric_reason('duration', '筛选条件', e, {'metrics': []}, catalog_candidates(k))
    assert '该指标已经提取，但未能正确匹配' in reason
    assert '没有作为指标提取' not in reason


def test_same_name_attribute_and_metric_does_not_prove_wrong_position():
    e, k, p = fixture()
    e.update(过滤条件=[], 排序=[])
    p.update(filters=[], sort=[])
    k['entities'][0].metadata['attributes'][0]['attr_name'] = '合作时长'
    e['展示字段'][0]['field'] = '合作时长'
    ast, _ = run(e, k, p)
    assert not ast['ambiguity']
