"""Frozen user contract: 结构化展示字段声明后 ASL 必须能投影这些列。"""
from types import SimpleNamespace

import agent


def knowledge():
    return {'entities': [
        SimpleNamespace(metadata={
            'entity_code': 'product_line', 'entity_name': '产品线',
            'attributes': [
                {'attr_code': 'line_level1', 'attr_name': '产品线一级分类',
                 'field_mapping': 'product_line.line_level1',
                 'is_display_name': True},
            ]}),
        SimpleNamespace(metadata={
            'entity_code': 'product', 'entity_name': '商品',
            'attributes': [
                {'attr_code': 'product_name', 'attr_name': '商品名称',
                 'field_mapping': 'product.product_name',
                 'is_display_name': True},
            ]}),
    ]}


def extraction():
    return {'展示字段': [
        {'entity': '产品线', 'field': '产品线一级分类'},
        {'entity': '商品', 'field': '商品名称'},
    ]}


def ast_with_product_only():
    return {'dimensions': [{'name': 'product.product_name'}], 'metrics': []}


def test_missing_structured_display_field_is_appended():
    ast = ast_with_product_only()

    added = agent._ensure_structured_display_projection(ast, extraction(), knowledge())

    assert added == ['product_line.line_level1']
    assert {'name': 'product_line.line_level1'} in ast['dimensions']


def test_entity_alias_substring_never_broadens_the_match():
    # 「产品」⊂「产品线一级分类」：实体别名子串不得把商品列带成候选（Q60 调试实测）。
    ast = ast_with_product_only()

    added = agent._ensure_structured_display_projection(ast, extraction(), knowledge())

    assert 'product.product_name' not in added


def test_already_projected_display_field_is_not_duplicated():
    ast = {
        'dimensions': [{'name': 'product.product_name'},
                       {'name': 'product_line.line_level1'}],
        'metrics': [],
    }

    assert agent._ensure_structured_display_projection(
        ast, extraction(), knowledge()) == []
    assert len(ast['dimensions']) == 2


def test_zero_candidates_never_invent_a_field():
    ast = ast_with_product_only()
    product_only = {'entities': knowledge()['entities'][1:]}  # 只有商品实体

    assert agent._ensure_structured_display_projection(
        ast, extraction(), product_only) == []
    assert ast['dimensions'] == [{'name': 'product.product_name'}]


def test_ambiguous_label_is_never_resolved():
    ast = ast_with_product_only()
    k = knowledge()
    k['entities'][1].metadata['attributes'].append({
        'attr_code': 'line_level1_copy', 'attr_name': '产品线一级分类',
        'field_mapping': 'product.line_level1_copy', 'is_display_name': True})

    assert agent._ensure_structured_display_projection(
        ast, extraction(), k) == []
    assert ast['dimensions'] == [{'name': 'product.product_name'}]


def test_ambiguous_ast_is_untouched():
    ast = ast_with_product_only()
    ast['ambiguity'] = [{'type': 'metric', 'field': '指标', 'phrase': 'x'}]

    assert agent._ensure_structured_display_projection(
        ast, extraction(), knowledge()) == []
    assert ast['dimensions'] == [{'name': 'product.product_name'}]
