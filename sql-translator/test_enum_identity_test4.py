"""Enum names and stable codes survive SQL generation; no database required."""
import json
import pytest
from test_sql_translator_hardening import translator, base_ast


def configured(items):
    value = translator()
    value.loader.dimensions['channel'] = {
        'dim_code': 'channel', 'dim_name': '渠道业态', 'dim_type': '枚举维度',
        'field_mapping': {'fact_table_field': 'order_info.channel'},
        'bind_entities': [{'entity_code': 'ent_order'}], 'enum_list': items,
    }
    return value


@pytest.mark.parametrize('key', ['code', 'value'])
def test_full_sql_maps_registered_codes_and_retains_identity(key):
    value = configured([{key: '1', 'name': '集中配送'}, {key: '8', 'name': '其他'}])
    sql = value.translate(json.dumps(base_ast(dimensions=[{'name': 'channel'}]), ensure_ascii=False), '6')
    assert "WHEN '1' THEN '集中配送'" in sql and "WHEN '8' THEN '其他'" in sql
    assert "WHEN ''" not in sql
    assert 'order_info.channel AS `渠道业态（编码）`' in sql
    assert 'GROUP BY order_info.channel' in sql


def test_equal_names_remain_distinct_raw_group_keys():
    value = configured([{'value': '1', 'name': '同名'}, {'value': '2', 'name': '同名'}])
    sql = value._build_select_clause([], [{'name': 'channel'}], 'ent_order', '6')
    assert sql.count("THEN '同名'") == 2
    assert 'order_info.channel AS `渠道业态（编码）`' in sql
    assert value._build_group_by_clause([{'name': 'channel'}], 'ent_order', '6') == 'GROUP BY order_info.channel'


@pytest.mark.parametrize('items', [
    [{'value': '1', 'code': '2', 'name': '冲突'}],
    [{'name': '缺少编码'}],
    [{'value': False, 'name': '非法编码'}],
    [{'value': '1', 'name': '甲'}, {'value': '1', 'name': '乙'}],
])
def test_invalid_enum_metadata_is_rejected(items):
    with pytest.raises(ValueError):
        configured(items)._build_dim_expression('channel', 'order_info.channel', model_id='6')


def test_zero_code_deduplication_and_quoted_labels():
    value = configured([{'value': 0, 'code': '0', 'name': "O'Brien"}, {'value': '0', 'name': "O'Brien"}])
    expr, _ = value._build_dim_expression('channel', 'order_info.channel', model_id='6')
    assert expr.count("WHEN '0'") == 1 and "O''Brien" in expr
