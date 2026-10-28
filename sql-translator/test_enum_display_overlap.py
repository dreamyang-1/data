"""A grouped enum and its display field must share one labeled projection."""
import copy
import json
import sqlite3

import pytest

from test_enum_identity_test4 import configured
from test_sql_translator_hardening import base_ast


@pytest.mark.parametrize('detail', [False, True])
def test_enum_display_overlap_keeps_label_identity_and_totals(detail):
    value = configured([{'value': '1', 'name': '同名'}, {'value': '2', 'name': '同名'}])
    ast = base_ast(dimensions=[{'name': 'channel'}])
    ast['display_fields'] = [{'name': 'order_info.channel', 'alias': '渠道业态'}]
    if detail:
        ast['metrics'] = []
    ast['sort'] = None
    before = copy.deepcopy(ast)
    sql = value.translate(json.dumps(ast), '6')
    assert ast == before
    assert sql.count(' AS `渠道业态`') == 1
    assert sql.count(' AS `渠道业态（编码）`') == 1
    with sqlite3.connect(':memory:') as db:
        db.executescript("CREATE TABLE order_info (channel TEXT, pay_amount REAL);"
                         "INSERT INTO order_info VALUES ('1',20),('1',30),('2',40);")
        rows = db.execute(sql).fetchall()
    assert sorted(rows) == ([('同名', '1'), ('同名', '1'), ('同名', '2')] if detail
                            else [('同名', '1', 50), ('同名', '2', 40)])


@pytest.mark.parametrize('alias', ['渠道业态（编码）', '原始编码'])
def test_explicit_code_display_preserves_requested_alias(alias):
    value = configured([{'value': '1', 'name': '集中配送'}])
    ast = base_ast(dimensions=[{'name': 'channel'}])
    ast['display_fields'] = [{'name': 'order_info.channel', 'alias': alias}]
    sql = value.translate(json.dumps(ast), '6')
    assert sql.count(f' AS `{alias}`') == 1
    assert "WHEN '1' THEN '集中配送'" in sql


@pytest.mark.parametrize('method', ['translate_only', 'execute_query'])
def test_different_sources_with_same_alias_report_exact_column(method):
    value = configured([{'value': '1', 'name': '集中配送'}])
    ast = base_ast(metrics=[], dimensions=[{'name': 'channel'}])
    ast['display_fields'] = [{'name': 'order_info.status', 'alias': '渠道业态'}]
    result = getattr(value, method)(json.dumps(ast), '6')
    assert not result['success']
    assert result['sql'] is None
    assert result['business_query_submitted'] is False
    error = result['semantic_validation_report']['errors'][0]
    assert error['code'] == 'DUPLICATE_OUTPUT_COLUMN'
    assert error['layer'] == 'BUSINESS'
    assert '渠道业态' in error['message'] and '两列' in error['message']
    assert error['message'] in result['error']


def test_untrusted_exception_text_does_not_become_public_alias_error():
    value = configured([])
    result = value._controlled_translation_failure(ValueError('列别名重复: password=secret'), False)
    assert 'password' not in json.dumps(result)
    assert 'semantic_validation_report' not in result


def test_alias_diagnostic_rejects_sensitive_or_unbounded_text():
    from sql_translator_prod import ProjectionAliasConflict
    value = configured([])
    for alias in ['password=secret', 'http://internal.example', 'x' * 81]:
        result = value._controlled_translation_failure(ProjectionAliasConflict(alias), False)
        assert alias not in json.dumps(result, ensure_ascii=False)
        assert result['error_code'] == 'DUPLICATE_OUTPUT_COLUMN'


def test_same_alias_does_not_hide_different_time_grains():
    value = configured([])
    ast = base_ast(metrics=[], dimensions=[{'name': 'order_info.pay_time', 'alias': '日期', 'granularity': 'month'}])
    ast['display_fields'] = [{'name': 'order_info.pay_time', 'alias': '日期'}]
    result = value.translate_only(json.dumps(ast), '6')
    assert result['error_code'] == 'DUPLICATE_OUTPUT_COLUMN'
