"""SQL defaults are disclosed from the same policy, never caller predicates."""
import copy
import json

import pytest

from sql_translator_prod import MAX_QUERY_ROWS
from test_sql_translator_hardening import base_ast, translator_with_hospital


@pytest.mark.parametrize('metrics,limit,expected,source', [
    ([], None, MAX_QUERY_ROWS, 'DETAIL_DEFAULT'),
    ([], 10, 10, 'ASL'),
    ([{'name': 'sales'}], None, None, 'NONE'),
    ([{'name': 'sales'}], 10, 10, 'ASL'),
])
def test_limits_match_sql_and_do_not_rewrite_asl(metrics, limit, expected, source):
    t = translator_with_hospital()
    ast = base_ast(metrics=metrics, dimensions=[{'name': 'hospital.hospital_name'}], limit=limit)
    before = copy.deepcopy(ast)
    result = t.translate_only(json.dumps(ast), '81')
    assert result['success'], result
    summary = result['effective_filter_summary']
    assert summary['effective_limit'] == expected
    assert summary['limit_source'] == source
    assert (f'LIMIT {expected}' in result['sql']) if expected else ('LIMIT' not in result['sql'])
    assert ast == before
    assert summary['asl_filters'] == ast['filters']


@pytest.mark.parametrize('mode,has_system_filter', [
    ('group_main', True), ('detail', False), ('explicit_null', False),
    ('include_null_group', False), ('non_main', False), ('explicit_non_null', False),
    ('no_dimension', False),
])
def test_implicit_null_filter_is_reported_only_when_applied(mode, has_system_filter):
    t = translator_with_hospital()
    ast = base_ast(dimensions=[{'name': 'hospital.hospital_name'}], limit=None)
    if mode == 'detail':
        ast['metrics'] = []
    if mode == 'no_dimension':
        ast['dimensions'] = []
    if mode == 'non_main':
        ast['dimensions'] = [{'name': 'hospital.hospital_level'}]
    if mode == 'include_null_group':
        ast['dimensions'][0]['include_null_group'] = True
    if mode in ('explicit_null', 'explicit_non_null'):
        ast['filters'] = [{'field': 'hospital.hospital_name', 'operator':
                          'IS NULL' if mode == 'explicit_null' else 'IS NOT NULL', 'value': None}]
    result = t.translate_only(json.dumps(ast), '81')
    assert result['success'], result
    rules = result['effective_filter_summary']['system_filters']
    assert bool(rules) == has_system_filter
    for rule in rules:
        assert rule['condition'] == 'hospital.hospital_name IS NOT NULL'
        assert rule['condition'] in result['sql']
        assert rule['source'] == 'ENTITY_IDENTITY_DEFAULT'
        assert '不参与' in rule['reason']
    if not has_system_filter and mode != 'explicit_non_null':
        assert 'hospital.hospital_name IS NOT NULL' not in result['sql']
