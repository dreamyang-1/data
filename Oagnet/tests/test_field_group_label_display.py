# -*- coding: utf-8 -*-
"""字段式分组维度下同实体主名称展示被误判为无唯一关联的复现与回归。

任务2（查询这些经销商主要销售的产品明细）偶发待补充条件的根因复现：
规划把任务2解析成指标查询（指标=含税销售总额，维度=经销商+商品）时，
ASL 模型按「商品」维度的治理绑定（商品编码）把分组写成物理字段
product.product_code；展示字段商品名称（product.product_name）与该分组
同属商品实体、是同一行的 1:1 身份/标签对，但 _group_exposes_field 只
识别逻辑维度定义，字段式分组直接返回 False，随后 _related_display_field
又因 `target_table not in starts` 排除了同表场景，最终误报
「该展示属性与分组对象之间没有可确认的唯一关联」歧义。该歧义类型为
dimension（中文槽位「展示字段」），澄清门判定为可提问（ASK），
DAG 子任务终止为 NEEDS_CLARIFICATION，前端表现为任务2卡在
"正在解析并校验结构化参数，生成标准 ASL"。
"""
import json

import pytest
from types import SimpleNamespace as Obj

from test_structured_binding import run


def _product_dealer_knowledge():
    attrs_product = [
        {'attr_code': 'product_code', 'attr_name': '商品编码',
         'field_mapping': 'product.product_code'},
        {'attr_code': 'product_name', 'attr_name': '商品名称',
         'field_mapping': 'product.product_name', 'is_main_attribute': True},
    ]
    attrs_dealer = [
        {'attr_code': 'dealer_code', 'attr_name': '经销商编码',
         'field_mapping': 'dealer.dealer_code'},
        {'attr_code': 'dealer_name', 'attr_name': '经销商名称',
         'field_mapping': 'dealer.dealer_name', 'is_main_attribute': True},
    ]
    return {
        'entities': [
            Obj(metadata={'entity_code': 'product', 'attributes': attrs_product}),
            Obj(metadata={'entity_code': 'dealer', 'attributes': attrs_dealer}),
        ],
        'metrics': [
            Obj(metadata={'metric_code': 'sales_including_tax',
                          'metric_name': '含税销售总额',
                          'entity_code': 'sales_order'}),
        ],
        'dimensions': [
            Obj(metadata={'dim_code': 'dealer',
                          'bind_entities': [{'mappingTable': 'dealer',
                                             'mappingColumn': 'dealer_code'}]}),
            Obj(metadata={'dim_code': 'product',
                          'bind_entities': [{'mappingTable': 'product',
                                             'mappingColumn': 'product_code'}]}),
        ],
        'relations': [
            Obj(metadata={'relation_type': 'N:1',
                          'join_key': {'source_field': 'sales_order.dealer_code',
                                       'target_field': 'dealer.dealer_code'}}),
            Obj(metadata={'relation_type': 'N:1',
                          'join_key': {'source_field': 'sales_order.product_code',
                                       'target_field': 'product.product_code'}}),
        ],
        '_vector_authorized_fields': [
            'product.product_code', 'product.product_name',
            'dealer.dealer_code', 'dealer.dealer_name',
            'sales_order.sales_including_tax',
        ],
    }


def _metric_extraction():
    return {'实体': ['经销商', '商品'], '指标': [{'name': '含税销售总额'}],
            '维度': ['经销商', '商品'],
            '展示字段': [{'entity': '商品', 'field': '商品名称'}],
            '过滤条件': [], '排序': [],
            '时间粒度': {'unit': None, 'time_range': None}, '限制': 1000}


def _hospital_extraction():
    extraction = _metric_extraction()
    extraction['实体'] = ['经销商', '商品', '医院']
    extraction['展示字段'] = [{'entity': '医院', 'field': '医院名称'}]
    return extraction


def _metric_plan(dimension_key):
    return {'subject': 'sales_order',
            'metrics': [{'index': 0, 'key': 'sales_including_tax'}],
            'dimensions': [{'index': 0, 'key': 'dealer'},
                           {'index': 1, 'key': dimension_key}],
            'display_fields': [{'index': 0, 'key': 'product.product_name'}]}


def _no_display_relation_ambiguity(ast):
    return not any(
        '没有可确认的唯一关联' in json.dumps(item, ensure_ascii=False)
        for item in ast['ambiguity']
    )


def test_field_style_code_group_allows_same_entity_label_display():
    """字段式分组 product.product_code + 展示商品名称：同实体身份/标签对。

    回归任务2偶发「待补充条件」：分组按商品维度治理绑定（商品编码）落成
    物理字段时，商品名称作为同一行的 1:1 主名称标签不得再被误报为
    无唯一关联；该标签由分组字段本身随行投影，不重复进 display_fields。
    """
    ast, _ = run(_metric_extraction(), _product_dealer_knowledge(),
                 _metric_plan('product.product_code'))
    assert _no_display_relation_ambiguity(ast), json.dumps(
        ast['ambiguity'], ensure_ascii=False)
    assert not ast.get('display_fields')


def test_logical_product_group_still_allows_label_display():
    """逻辑维度分组 product + 展示商品名称：既有身份/标签判定保持不变。"""
    ast, _ = run(_metric_extraction(), _product_dealer_knowledge(),
                 _metric_plan('product'))
    assert _no_display_relation_ambiguity(ast), json.dumps(
        ast['ambiguity'], ensure_ascii=False)
    assert not ast.get('display_fields')


def test_cross_entity_display_without_relation_still_ambiguous():
    """跨实体展示且无 to-one 关系：仍按歧义拦停，不得放宽。"""
    knowledge = _product_dealer_knowledge()
    knowledge['_vector_authorized_fields'].append('hospital.hospital_name')
    knowledge['entities'].append(Obj(metadata={'entity_code': 'hospital', 'attributes': [
        {'attr_code': 'hospital_name', 'attr_name': '医院名称',
         'field_mapping': 'hospital.hospital_name', 'is_main_attribute': True}]}))
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'hospital.hospital_name'}]
    ast, _ = run(_hospital_extraction(), knowledge, plan)
    assert any('没有可确认的唯一关联' in json.dumps(item, ensure_ascii=False)
               for item in ast['ambiguity'])


def _amount_extraction(field='含税金额', intent='明细查询'):
    extraction = _metric_extraction()
    extraction['意图'] = intent
    extraction['实体'] = ['商品', '销售订单']
    extraction['展示字段'] = [{'entity': '销售订单', 'field': field}]
    return extraction


def _amount_knowledge(formula):
    knowledge = _product_dealer_knowledge()
    knowledge['entities'].append(Obj(metadata={'entity_code': 'sales_order', 'attributes': [
        {'attr_code': 'amount_with_tax', 'attr_name': '含税金额',
         'field_mapping': 'sales_order.amount_with_tax'},
        {'attr_code': 'net_amount', 'attr_name': '不含税金额',
         'field_mapping': 'sales_order.net_amount'},
    ]}))
    knowledge['metrics'][0].metadata['calculation_rule'] = {'calc_formula': formula}
    knowledge['_vector_authorized_fields'].extend(
        ['sales_order.amount_with_tax', 'sales_order.net_amount'])
    return knowledge


def test_display_field_covered_by_bound_metric_is_omitted():
    """汇总场景：展示字段度量已由指标公式覆盖，不重复作为行级列。"""
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'sales_order.amount_with_tax'}]
    ast, repairs = run(
        _amount_extraction(intent='指标查询'),
        _amount_knowledge('sales_total_including_tax=SUM(sales_order.amount_with_tax)'),
        plan,
    )
    assert _no_display_relation_ambiguity(ast), json.dumps(
        ast['ambiguity'], ensure_ascii=False)
    assert not ast.get('display_fields')
    assert any(item['type'] == 'OMIT_DISPLAY_FIELD_COVERED_BY_METRIC'
               for item in repairs)


def test_detail_display_field_covered_by_metric_is_projected_as_row_column():
    """明细场景：指标会被行级口径清空，同一物理列仍须作为返回列投影。"""
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'sales_order.amount_with_tax'}]
    ast, repairs = run(
        _amount_extraction(),
        _amount_knowledge('sales_total_including_tax=SUM(sales_order.amount_with_tax)'),
        plan,
    )
    assert _no_display_relation_ambiguity(ast), json.dumps(
        ast['ambiguity'], ensure_ascii=False)
    assert 'sales_order.amount_with_tax' in {d['name'] for d in ast['dimensions']}
    assert any(item['type'] == 'DISPLAY_FIELD_AS_ROW_PROJECTION'
               for item in repairs)


def test_display_field_not_covered_by_metric_stays_ambiguous():
    """指标公式未引用该列：不得借指标覆盖悄悄丢弃展示字段。"""
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'sales_order.net_amount'}]
    ast, repairs = run(
        _amount_extraction('不含税金额'),
        _amount_knowledge('sales_total_including_tax=SUM(sales_order.amount_with_tax)'),
        plan,
    )
    assert any('没有可确认的唯一关联' in json.dumps(item, ensure_ascii=False)
               for item in ast['ambiguity'])
    assert not any(item['type'] == 'OMIT_DISPLAY_FIELD_COVERED_BY_METRIC'
                   for item in repairs)


def _run_with_contract_intent(extraction, knowledge, plan, row_level_detail):
    from structured_binding import bind
    return bind(
        extraction, knowledge,
        Obj(invoke=lambda messages: Obj(content=json.dumps(plan))),
        row_level_detail=row_level_detail,
    )


def test_contract_detail_intent_projects_field_when_extraction_omits_it():
    """抽取未写“意图”槽位时，以调用方契约的 DETAIL_QUERY 作为行级口径。

    真实问题：任务2要求返回商品名称与含税金额，抽取的意图槽位缺失，
    仅按抽取文本判定会把含税金额当作汇总口径省略，结果校验报
    「未返回本子任务要求的展示字段」。
    """
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'sales_order.amount_with_tax'}]
    extraction = _amount_extraction()
    extraction.pop('意图')
    ast, repairs = _run_with_contract_intent(
        extraction,
        _amount_knowledge('sales_total_including_tax=SUM(sales_order.amount_with_tax)'),
        plan,
        True,
    )
    assert 'sales_order.amount_with_tax' in {d['name'] for d in ast['dimensions']}
    assert any(item['type'] == 'DISPLAY_FIELD_AS_ROW_PROJECTION'
               for item in repairs)


def test_contract_metric_intent_omits_field_when_extraction_omits_it():
    """契约是汇总口径时仍按聚合投影，不额外附加行级列。"""
    plan = _metric_plan('product.product_code')
    plan['display_fields'] = [{'index': 0, 'key': 'sales_order.amount_with_tax'}]
    extraction = _amount_extraction()
    extraction.pop('意图')
    ast, repairs = _run_with_contract_intent(
        extraction,
        _amount_knowledge('sales_total_including_tax=SUM(sales_order.amount_with_tax)'),
        plan,
        False,
    )
    assert 'sales_order.amount_with_tax' not in {d['name'] for d in ast['dimensions']}
    assert any(item['type'] == 'OMIT_DISPLAY_FIELD_COVERED_BY_METRIC'
               for item in repairs)


def _salesperson_id_knowledge():
    """复现模型130「业务员」实体：id 属性 is_primary_key=0 且 attr_code='id'。

    真实问题（2026-10-23）：「给出2025年订单平均金额最高的前五位销售人员，
    以及他们所在的销售公司」。维度槽位落成物理字段 salesperson.id 时，
    身份证据链只认 is_primary_key / attr_code ∈ {code+'_id', code+'_code'}，
    配置漏打主键标记导致 identities 为空，同表主名称「销售员姓名」被误报
    「该展示属性与分组对象之间没有可确认的唯一关联」。
    """
    attrs_salesperson = [
        # 配置缺失：无 is_primary_key/is_unique，编码为 'id'（非 *_id 形）
        {'attr_code': 'id', 'attr_name': '业务员ID',
         'field_mapping': 'salesperson.id'},
        {'attr_code': 'salesperson_code', 'attr_name': '销售员编码',
         'field_mapping': 'salesperson.salesperson_code'},
        {'attr_code': 'department_code', 'attr_name': '销售部门编码',
         'field_mapping': 'salesperson.department_code'},
        {'attr_code': 'salesperson_name', 'attr_name': '销售员姓名',
         'field_mapping': 'salesperson.salesperson_name', 'is_main_attribute': True},
    ]
    attrs_company = [
        {'attr_code': 'guoyao_name', 'attr_name': '国药公司名称',
         'field_mapping': 'guoyao_company.guoyao_name', 'is_main_attribute': True},
    ]
    return {
        'entities': [
            Obj(metadata={'entity_code': 'salesperson', 'attributes': attrs_salesperson}),
            Obj(metadata={'entity_code': 'guoyao_company', 'attributes': attrs_company}),
        ],
        'metrics': [
            Obj(metadata={'metric_code': 'sales_including_tax',
                          'metric_name': '含税销售总额',
                          'entity_code': 'sales_order'}),
        ],
        'dimensions': [],
        'relations': [],
        '_vector_authorized_fields': [
            'salesperson.id', 'salesperson.salesperson_code',
            'salesperson.department_code', 'salesperson.salesperson_name',
            'guoyao_company.guoyao_name',
        ],
    }


def _salesperson_extraction():
    return {'实体': ['业务员', '销售订单'], '指标': [{'name': '含税销售总额'}],
            '维度': ['业务员'],
            '展示字段': [{'entity': '业务员', 'field': '销售员姓名'}],
            '过滤条件': [], '排序': [],
            '时间粒度': {'unit': None, 'time_range': None}, '限制': 1000}


def test_unflagged_surrogate_id_group_allows_same_entity_label_display():
    """主键标记漏打的 id 列分组 + 同实体主名称展示：不得误报无唯一关联。

    回归模型130实测：分组键 salesperson.id 的身份证据由代理主键列命名
    （attr_code/列名为 id）兜底，销售员姓名作为同一行的 1:1 主名称标签
    随分组投影，不再触发澄清拦截。
    """
    plan = {'subject': 'sales_order',
            'metrics': [{'index': 0, 'key': 'sales_including_tax'}],
            'dimensions': [{'index': 0, 'key': 'salesperson.id'}],
            'display_fields': [{'index': 0, 'key': 'salesperson.salesperson_name'}]}
    ast, _ = run(_salesperson_extraction(), _salesperson_id_knowledge(), plan)
    assert _no_display_relation_ambiguity(ast), json.dumps(
        ast['ambiguity'], ensure_ascii=False)
    assert not ast.get('display_fields')


def test_non_identity_code_group_still_ambiguous():
    """分组键是非身份业务编码列（部门编码）：粒度保护不得放宽。"""
    plan = {'subject': 'sales_order',
            'metrics': [{'index': 0, 'key': 'sales_including_tax'}],
            'dimensions': [{'index': 0, 'key': 'salesperson.department_code'}],
            'display_fields': [{'index': 0, 'key': 'salesperson.salesperson_name'}]}
    ast, _ = run(_salesperson_extraction(), _salesperson_id_knowledge(), plan)
    assert any('没有可确认的唯一关联' in json.dumps(item, ensure_ascii=False)
               for item in ast['ambiguity'])

