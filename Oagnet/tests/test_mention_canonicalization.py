# -*- coding: utf-8 -*-
"""结构化参数槽位名向量库标准化（canonicalize_structured_mentions）回归。

覆盖：口语别名唯一高分命中改写、标准名/同义词不改写（防止指标被同名属性
覆盖）、指标位只认指标、多义不改写、低分不改写、过滤值不动、物理字段路径
跳过、空库与检索异常不阻断。
"""
from vector_store import SearchResult
from prompt_build import canonicalize_structured_mentions

SM_ID = 130
DOMAINS = [296]

# surface -> [(score, metadata), ...]，模拟一次跨类检索（不按类型过滤）。
RECORDS = {
    # 标准指标名：指标精确命中，同时属性"含税金额"语义更近（真实数据 0.7808）
    '含税销售总额': [
        (0.7808, {'type': 'attribute', 'attr_code': 'amount_with_tax',
                  'attr_name': '含税金额'}),
        (0.7652, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                  'metric_name': '含税销售总额', 'synonyms': '{"销售总额","销售额"}'}),
    ],
    # 标准属性名：属性精确命中 1.0
    '含税金额': [
        (1.0, {'type': 'attribute', 'attr_code': 'amount_with_tax',
               'attr_name': '含税金额'}),
        (0.6298, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                  'metric_name': '含税销售总额', 'synonyms': '销售总额,销售额'}),
        (0.6243, {'type': 'metric', 'metric_code': 'average_price_per_order',
                  'metric_name': '订单级均价', 'synonyms': None}),
    ],
    # 指标同义词：精确同义词命中，不改写
    '销售额': [
        (0.8421, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                  'metric_name': '含税销售总额', 'synonyms': '{"销售总额","销售额"}'}),
    ],
    # 目录外的口语叫法：指标唯一高分命中，改写
    '卖出去的钱': [
        (0.93, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                'metric_name': '含税销售总额', 'synonyms': '{"销售总额"}'}),
        (0.52, {'type': 'metric', 'metric_code': 'sales_total_quantity',
                'metric_name': '销售总数量', 'synonyms': None}),
    ],
    # 多义：两个指标身份分差 < margin
    '金额': [
        (0.88, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                'metric_name': '含税销售总额', 'synonyms': None}),
        (0.86, {'type': 'metric', 'metric_code': 'average_price_per_order',
                'metric_name': '订单级均价', 'synonyms': None}),
    ],
    # 低分：低于 min_score
    '货值': [
        (0.55, {'type': 'metric', 'metric_code': 'sales_total_including_tax',
                'metric_name': '含税销售总额', 'synonyms': None}),
        (0.40, {'type': 'metric', 'metric_code': 'sales_total_cost',
                'metric_name': '销售总成本', 'synonyms': None}),
    ],
    # 维度标准名
    '商品名称': [
        (0.95, {'type': 'attribute', 'attr_code': 'goods_name',
                'attr_name': '商品名称'}),
    ],
}


class FakeStore:
    def __init__(self, records, empty=False):
        self._records = records
        self._empty = empty
        self.search_calls = []

    def count(self):
        return 0 if self._empty else 10

    def search(self, query_vector, top_k=20, where=None):
        self.search_calls.append({'top_k': top_k, 'where': where})
        return [SearchResult(id=str(i), score=score, text='',
                             metadata=dict(meta))
                for i, (score, meta) in enumerate(self._records.get(query_vector, []))]


def fake_embed(surface):
    return surface


def run_canonicalize(extraction, records=None):
    store = FakeStore(records if records is not None else RECORDS)
    repairs = canonicalize_structured_mentions(
        extraction, store, fake_embed, SM_ID, DOMAINS)
    return repairs, store


def test_canonical_metric_not_overwritten_by_nearer_attribute():
    # 回归：真实向量库里"含税金额"(属性, 0.7808) 比"含税销售总额"(指标, 0.7652) 得分高，
    # 指标名曾被误改成属性名。精确标准名必须保持不变。
    extraction = {'排序': ['含税销售总额']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['排序'] == ['含税销售总额']
    assert repairs == []


def test_metric_filter_is_not_rewritten_to_its_underlying_attribute():
    extraction = {'指标': ['含税销售总额'], '过滤条件': [
        {'entity': None, 'field': '含税销售总额', 'op': '>',
         'value': [1000000], 'stage': 'having'}]}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['过滤条件'][0]['field'] == '含税销售总额'
    assert repairs == []


def test_selected_metric_filter_is_preserved_even_with_where_stage():
    extraction = {'指标': [{'name': '含税销售总额'}], '过滤条件': [
        {'field': '含税销售总额', 'op': '>', 'value': [1000000], 'stage': 'where'}]}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['过滤条件'][0]['field'] == '含税销售总额'
    assert repairs == []


def test_metric_filter_flows_through_catalog_binding_as_having():
    from copy import deepcopy
    from test_structured_binding import fixture, run
    extraction, knowledge, plan = fixture()
    knowledge = deepcopy(knowledge)
    knowledge['metrics'][0].metadata['calculation_rule'] = {
        'calc_formula': 'count_hospital=COUNT(hospital.name)'}
    extraction['指标'] = [{'name': '区域全部医院总数'}]
    extraction['过滤条件'] = [{'field': '区域全部医院总数', 'op': '>',
                            'value': [12], 'stage': 'having'}]
    plan['filters'] = [{'index': 0, 'key': 'hospital.name'}]
    records = {'区域全部医院总数': [
        (0.85, {'type': 'attribute', 'attr_code': 'name', 'attr_name': '医院名称'}),
        (0.80, {'type': 'metric', 'metric_code': 'count_hospital',
                'metric_name': '区域全部医院总数'}),
    ]}
    run_canonicalize(extraction, records)
    ast, _ = run(extraction, knowledge, plan)
    assert ast['having'] == ['COUNT(hospital.name) > 12']
    assert not ast['filters'] and not ast['ambiguity']


def test_row_level_amount_filter_remains_an_attribute():
    extraction = {'指标': ['含税销售总额'], '过滤条件': [
        {'entity': '销售订单', 'field': '含税金额', 'op': '>',
         'value': [1000000], 'stage': 'where'}]}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['过滤条件'][0]['field'] == '含税金额'
    assert repairs == []


def test_canonical_attribute_not_rewritten():
    extraction = {'展示字段': [{'name': '商品名称'}, {'field': '含税金额'}]}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['展示字段'] == [{'name': '商品名称'}, {'field': '含税金额'}]
    assert repairs == []


def test_metric_synonym_is_canonical():
    extraction = {'指标': ['销售额']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['指标'] == ['销售额']
    assert repairs == []


def test_colloquial_metric_rewritten_to_canonical():
    extraction = {'指标': ['卖出去的钱']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['指标'] == ['含税销售总额']
    assert len(repairs) == 1
    assert repairs[0]['type'] == 'VECTOR_MENTION_CANONICALIZED'
    assert repairs[0]['section'] == '指标'
    assert repairs[0]['original'] == '卖出去的钱'
    assert repairs[0]['canonical'] == '含税销售总额'


def test_ambiguous_surface_left_untouched():
    extraction = {'指标': ['金额']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['指标'] == ['金额']
    assert repairs == []


def test_low_score_surface_left_untouched():
    extraction = {'指标': ['货值']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['指标'] == ['货值']
    assert repairs == []


def test_metric_slot_never_maps_to_attribute_identity():
    # 指标位只允许 metric：即使属性得分更高也不得改写为属性名
    extraction = {'指标': ['含税金额']}
    repairs, _ = run_canonicalize(extraction)
    assert extraction['指标'] == ['含税金额']


def test_physical_field_path_not_rewritten():
    extraction = {'过滤条件': [{'entity': '订单', 'field': 'sales_order.order_id',
                               'operator': '=', 'value': 'W1'}]}
    repairs, store = run_canonicalize(extraction)
    assert extraction['过滤条件'][0]['field'] == 'sales_order.order_id'
    assert repairs == []
    assert store.search_calls == []


def test_filter_value_not_touched():
    extraction = {'过滤条件': [{'entity': '销售订单', 'field': '卖出去的钱',
                               'operator': '=', 'value': '卖出去的钱'}]}
    records = {'卖出去的钱': RECORDS['卖出去的钱']}
    repairs, _ = run_canonicalize(extraction, records)
    item = extraction['过滤条件'][0]
    # 过滤字段允许匹配指标，筛选值保持原样。
    assert item['value'] == '卖出去的钱'
    assert item['field'] == '含税销售总额'
    assert repairs


def test_filter_field_rewritten_with_value_kept():
    records = {
        '客户名': [
            (0.91, {'type': 'attribute', 'attr_code': 'customer_name',
                    'attr_name': '客户名称'}),
        ],
    }
    extraction = {'过滤条件': [{'field': '客户名', 'operator': '=', 'value': '客户名'}]}
    repairs, _ = run_canonicalize(extraction, records)
    assert extraction['过滤条件'][0]['field'] == '客户名称'
    assert extraction['过滤条件'][0]['value'] == '客户名'
    assert repairs


def test_empty_store_noop():
    extraction = {'指标': ['卖出去的钱']}
    store = FakeStore(RECORDS, empty=True)
    repairs = canonicalize_structured_mentions(
        extraction, store, fake_embed, SM_ID, DOMAINS)
    assert repairs == []
    assert extraction['指标'] == ['卖出去的钱']


def test_search_exception_swallowed():
    class BrokenStore:
        def count(self):
            return 5

        def search(self, *args, **kwargs):
            raise RuntimeError('milvus down')

    extraction = {'指标': ['卖出去的钱']}
    repairs = canonicalize_structured_mentions(
        extraction, BrokenStore(), fake_embed, SM_ID, DOMAINS)
    assert repairs == []
    assert extraction['指标'] == ['卖出去的钱']


def test_single_search_per_slot():
    extraction = {'指标': ['卖出去的钱', '销售额'],
                  '展示字段': [{'name': '商品名称'}]}
    _, store = run_canonicalize(extraction)
    assert len(store.search_calls) == 3


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'{name} 通过')
    print('全部通过')
