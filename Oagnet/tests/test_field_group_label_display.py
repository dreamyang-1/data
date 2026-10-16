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
