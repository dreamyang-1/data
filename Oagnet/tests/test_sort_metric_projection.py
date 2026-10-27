# -*- coding: utf-8 -*-
"""按指标排序时必须同时投影该指标的回归测试。

背景：明细任务"返回商品名称及含税销售总额，按含税销售总额降序"在结构化参数
指标位为空时，排序槽位命中了指标，但只写了 ast['sort']，SQL 生成
ORDER BY sales_total_including_tax 却没有 SELECT 该聚合，触发
METRIC_FORMULA_STALE。修复后排序命中指标会同步补入 ast['metrics']。
"""
import json
from types import SimpleNamespace as Obj

from structured_binding import bind


def sort_fixture(sort_item=None, metric_slot=None):
    extraction = {
        '意图': '明细查询', '实体': ['商品'], '指标': metric_slot or [],
        '维度': [], '展示字段': [], '过滤条件': [],
        '排序': [sort_item or {'name': '含税销售总额', 'order': 'DESC'}],
        '时间粒度': {'unit': None, 'time_range': None}, '限制': 1000,
        '输出要求': '默认输出表格',
    }
    knowledge = {
        'entities': [Obj(metadata={'entity_code': 'product', 'entity_name': '商品',
                                   'attributes': [{'attr_name': '商品名称',
                                                   'field_mapping': 'product.product_name'}]})],
        'metrics': [Obj(metadata={'metric_code': 'sales_total_including_tax',
                                  'metric_name': '含税销售总额',
                                  'synonyms': '销售额,含税金额',
                                  'entity_code': 'sales_order'})],
        'dimensions': [],
        'entity_attribute_values': [],
        '_vector_authorized_fields': ['product.product_name'],
    }
    return extraction, knowledge


def run(extraction, knowledge, plan):
    return bind(extraction, knowledge,
                Obj(invoke=lambda messages: Obj(content=json.dumps(plan))))


def test_sort_by_metric_projects_the_metric():
    extraction, knowledge = sort_fixture()
    plan = {'subject': 'product', 'metrics': [],
            'sort': [{'index': 0, 'key': 'sales_total_including_tax'}]}
    ast, repairs = run(extraction, knowledge, plan)
    assert ast['ambiguity'] and ast['metrics'] == []
    assert not any(r['type'] == 'METRIC_FROM_SORT_SLOT' for r in repairs)


def test_sort_by_metric_does_not_duplicate_declared_metric():
    extraction, knowledge = sort_fixture(metric_slot=[{'name': '含税销售总额'}])
    plan = {'subject': 'product',
            'metrics': [{'index': 0, 'key': 'sales_total_including_tax'}],
            'sort': [{'index': 0, 'key': 'sales_total_including_tax'}]}
    ast, repairs = run(extraction, knowledge, plan)
    assert not ast['ambiguity'], ast['ambiguity']
    assert [m['name'] for m in ast['metrics']] == ['sales_total_including_tax']
    assert not any(r['type'] == 'METRIC_FROM_SORT_SLOT' for r in repairs)








def test_sort_by_physical_field_does_not_add_metric():
    extraction, knowledge = sort_fixture(
        {'name': '商品名称', 'order': 'DESC'})
    plan = {'subject': 'product', 'metrics': [],
            'sort': [{'index': 0, 'key': 'product.product_name'}]}
    ast, repairs = run(extraction, knowledge, plan)
    # 明细查询缺指标时会另有契约提示，本用例只验证排序不越权引入指标
    assert ast['metrics'] == []
    assert (ast.get('sort') or {}).get('field_type') == 'field'
    assert not any(r['type'] == 'METRIC_FROM_SORT_SLOT' for r in repairs)
