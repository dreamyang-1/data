"""JOIN reuse across compiler phases and shared paths, with alias protection."""
import json
import sqlite3
from copy import deepcopy
from itertools import combinations

import pytest

from join_assembly import JoinAssembly, split_joins
from test_sql_translator_hardening import translator


ORDER_JOIN = 'LEFT JOIN sales_order ON dealer.dealer_code = sales_order.dealer_code'


def fixture():
    value = translator()
    columns = {'dealer': ['dealer_code', 'dealer_name'],
               'sales_order': ['order_key', 'dealer_code', 'created_date', 'amount', 'product_code'],
               'product': ['product_code', 'product_name']}
    value.loader.entities = {code: {
        'entity_code': code, 'entity_name': code,
        'physical_table_join': {'base_table': code},
        'attributes': [{'attr_code': field, 'field_mapping': f'{code}.{field}'} for field in fields],
        'relations': [], 'sub_table_mappings': []} for code, fields in columns.items()}
    value.loader.entities['dealer']['relations'] = [
        {'target_entity': 'sales_order', 'join_key': 'dealer.dealer_code = sales_order.dealer_code'}]
    value.loader.entities['sales_order']['relations'] = [
        {'target_entity': 'product', 'join_key': 'sales_order.product_code = product.product_code'}]
    value.loader.dimensions = {}
    value.loader.metrics = {'order_count': {
        'metric_code': 'order_count', 'metric_name': '订单笔数', 'metric_level': '原子指标',
        'source_dependency': {'bind_entity': ['dealer']},
        'calculation_rule': {'calc_formula': 'COUNT(DISTINCT sales_order.order_key)',
                             'global_filters': [], 'depend_metrics': []}, 'params': {}}}
    ast = {'version': '2.0', 'intent': 'query', 'subject': {'entity': 'dealer'},
           'metrics': [{'name': 'order_count', 'alias': '订单笔数'}],
           'dimensions': [{'name': 'dealer.dealer_code'}], 'filters': [], 'having': [],
           'time_context': {'type': 'range', 'start': '2025-09-30', 'end': '2026-09-30',
                            'unit': 'day', 'anchor': 'sales_order.created_date'},
           'sort': {'field': 'order_count', 'field_type': 'metric', 'direction': 'ASC'}, 'limit': 5}
    return value, ast


def test_time_and_metric_join_once_and_generated_query_executes():
    value, ast = fixture()
    before = deepcopy(ast)
    sql = value.translate(json.dumps(ast), '81')
    assert sql.count('JOIN sales_order ') == 1
    assert '2025-09-30' in sql and '2026-10-01' in sql and 'ASC LIMIT 5' in sql
    assert ast == before
    with sqlite3.connect(':memory:') as database:
        database.execute('CREATE TABLE dealer (dealer_code TEXT, dealer_name TEXT)')
        database.execute('CREATE TABLE sales_order (order_key TEXT, dealer_code TEXT, created_date TEXT)')
        database.executemany('INSERT INTO dealer VALUES (?, ?)', [('D1', 'A'), ('D2', 'B')])
        database.executemany('INSERT INTO sales_order VALUES (?, ?, ?)', [
            ('O1', 'D1', '2025-10-01'), ('O2', 'D1', '2025-11-01'),
            ('O3', 'D2', '2025-12-01'), ('O4', 'D2', '2027-01-01')])
        rows = database.execute(sql).fetchall()
    assert rows == [('D2', 1), ('D1', 2)]


def test_time_phase_registers_all_bridge_tables_for_later_phases():
    value, _ = fixture()
    tables = {'dealer'}
    joins = value._detect_time_context_joins('dealer', {'anchor': 'product.created_date'}, tables, '81')
    assert len(joins) == 1
    assert tables == {'dealer', 'sales_order', 'product'}
    assert value._detect_time_context_joins('dealer', {'anchor': 'product.created_date'}, tables, '81') == []


@pytest.mark.parametrize('sources', [*combinations(['dimension', 'filter', 'having', 'global', 'derived'], 2),
                                     ('dimension', 'filter', 'having', 'global', 'derived')])
def test_all_table_contributors_share_the_same_relation(sources):
    value, ast = fixture()
    if 'dimension' in sources:
        ast['dimensions'].append({'name': 'sales_order.created_date'})
    if 'filter' in sources:
        ast['filters'].append({'field': 'sales_order.amount', 'operator': '>', 'value': 0})
    if 'having' in sources:
        ast['having'] = ['COUNT(DISTINCT sales_order.order_key) > 0']
    if 'global' in sources:
        value.loader.metrics['order_count']['calculation_rule']['global_filters'] = [
            {'condition': 'sales_order.amount > 0'}]
    if 'derived' in sources:
        value.loader.metrics['ratio'] = {
            'metric_code': 'ratio', 'metric_name': 'Ratio', 'metric_level': '衍生指标',
            'source_dependency': {'bind_entity': ['dealer']}, 'params': {},
            'calculation_rule': {'calc_formula': 'order_count / 2', 'global_filters': [],
                                 'depend_metrics': ['order_count']}}
        ast['metrics'].append({'name': 'ratio', 'alias': 'Ratio'})
    sql = value.translate(json.dumps(ast), '81')
    assert sql.count('JOIN sales_order ') == 1


def test_time_global_formula_share_bridge_and_preserve_predicates():
    value, ast = fixture()
    ast['filters'] = [{'field': 'product.product_name', 'operator': '=', 'value': 'test'}]
    value.loader.metrics['order_count']['calculation_rule']['global_filters'] = [
        {'condition': 'sales_order.amount > 0'}]
    sql = value.translate(json.dumps(ast), '81')
    assert sql.count('JOIN sales_order ') == sql.count('JOIN product ') == 1
    assert "product.product_name = 'test'" in sql and 'sales_order.amount > 0' in sql


def test_remote_global_filter_anchor_keeps_formula_on_business_path():
    value, ast = fixture()
    value.loader.entities['dealer']['attributes'].append({'field_mapping': 'dealer.product_code'})
    value.loader.entities['dealer']['relations'].append({
        'target_entity': 'product', 'join_key': 'dealer.product_code = product.product_code'})
    metric = value.loader.metrics['order_count']['calculation_rule']
    metric['calc_formula'] = 'COUNT(DISTINCT product.product_code)'
    metric['global_filters'] = [{'condition': 'sales_order.amount > 0'}]
    sql = value.translate(json.dumps(ast), '81')
    assert sql.count('JOIN sales_order ') == sql.count('JOIN product ') == 1
    assert 'sales_order.product_code = product.product_code' in sql
    assert 'dealer.product_code = product.product_code' not in sql


def test_request_reuse_has_no_previous_join_state():
    value, ast = fixture()
    first = value.translate(json.dumps(ast), '81')
    second = value.translate(json.dumps(ast), '81')
    assert first == second and first.count('JOIN sales_order ') == 1


def test_final_assembly_is_defensive_if_a_contributor_repeats_a_join(monkeypatch):
    value, ast = fixture()
    monkeypatch.setattr(value, '_detect_time_context_joins', lambda *a: [ORDER_JOIN, ORDER_JOIN])
    monkeypatch.setattr(value, '_detect_calc_formula_joins', lambda *a, **k: [ORDER_JOIN])
    sql = value.translate(json.dumps(ast), '81')
    assert sql.count('JOIN sales_order ') == 1


def test_conflicting_alias_is_explained_before_database_execution(monkeypatch):
    value, ast = fixture()
    monkeypatch.setattr(value, '_detect_time_context_joins', lambda *a: [ORDER_JOIN])
    monkeypatch.setattr(value, '_detect_calc_formula_joins', lambda *a, **k: [
        'LEFT JOIN sales_order ON dealer.dealer_code = sales_order.other_dealer_code'])
    with pytest.raises(ValueError, match='重复用于不同连接条件'):
        value.translate(json.dumps(ast), '81')


@pytest.mark.parametrize('join', [ORDER_JOIN, ORDER_JOIN.lower(),
    'LEFT OUTER JOIN `sales_order` ON `dealer`.`dealer_code` = `sales_order`.`dealer_code`'])
def test_same_batch_repeated_paths_are_deduplicated(join):
    value, _ = fixture()
    merged = value._filter_existing_joins(join + ' ' + join, {'dealer'})
    assert len(split_joins(merged)) == 1
    assert value._extract_tables_from_join(merged) == {'sales_order'}


def test_relation_with_j_in_on_does_not_drop_earlier_path_segment():
    value, _ = fixture()
    text = 'LEFT JOIN project ON dealer.project_id = project.id LEFT JOIN city ON dealer.city_id = city.id'
    result = value._filter_existing_joins(text, {'dealer'})
    assert result == text
    assert value._extract_tables_from_join(result) == {'project', 'city'}


@pytest.mark.parametrize('condition', ["sales_order.note = 'LEFT JOIN ghost ON a=b'",
                                      'sales_order.note = "LEFT JOIN ghost ON a=b"',
                                      'sales_order.`LEFT JOIN` = dealer.id',
                                      'sales_order.id IN (SELECT x.id FROM x JOIN y ON x.id=y.id)'])
def test_join_keywords_inside_quotes_or_nested_scope_are_not_boundaries(condition):
    value, _ = fixture()
    text = 'LEFT JOIN sales_order ON ' + condition + ' LEFT JOIN product ON product.id = sales_order.id'
    assert value._filter_existing_joins(text, {'dealer'}) == text
    assert value._extract_tables_from_join(text) == {'sales_order', 'product'}


def test_existing_path_is_reused_without_mutating_callers_set():
    value, _ = fixture()
    tables = {'dealer', 'sales_order'}
    tail = 'LEFT JOIN product ON sales_order.product_code = product.product_code'
    assert value._filter_existing_joins(ORDER_JOIN + ' ' + tail, tables) == tail
    assert tables == {'dealer', 'sales_order'}


def test_two_roles_of_same_table_and_self_join_keep_distinct_aliases():
    joins = JoinAssembly({'employee'}, strict_base=True)
    joins.add(['LEFT JOIN employee manager ON manager.id = employee.manager_id',
               'LEFT JOIN employee AS mentor ON mentor.id = employee.mentor_id'])
    assert len(joins.fragments) == 2
    assert joins.tables == {'employee', 'manager', 'mentor'}


@pytest.mark.parametrize('conflict', ['INNER JOIN t ON a.id = t.id',
                                     'LEFT JOIN t ON a.other_id = t.id',
                                     'LEFT JOIN other_table t ON a.id = t.id'])
def test_same_alias_with_different_table_type_or_condition_is_not_silently_removed(conflict):
    joins = JoinAssembly({'a'}, strict_base=True)
    joins.add(['LEFT JOIN t ON a.id = t.id'])
    with pytest.raises(ValueError, match='独立别名'):
        joins.add([conflict])


def test_equivalent_join_spelling_and_reversed_equalities_reuse_one_relation():
    joins = JoinAssembly({'a'}, strict_base=True)
    joins.add(['LEFT JOIN t ON a.id = t.id AND a.x = t.x',
               'left outer join `t` on `t`.`x`=`a`.`x` AND `t`.`id`=`a`.`id`'])
    assert len(joins.fragments) == 1


def test_case_sensitive_tables_and_string_conditions_are_not_merged():
    joins = JoinAssembly({'a'}, strict_base=True)
    joins.add(["LEFT JOIN t ON a.id=t.id AND t.kind='A'"])
    with pytest.raises(ValueError):
        joins.add(["LEFT JOIN t ON a.id=t.id AND t.kind='a'"])
    joins.add(['LEFT JOIN T ON a.id=T.id'])
    assert len(joins.fragments) == 2


def test_base_table_cannot_be_reintroduced_without_an_alias():
    joins = JoinAssembly({'a'}, strict_base=True)
    with pytest.raises(ValueError, match='独立别名'):
        joins.add(['LEFT JOIN a ON a.parent_id = a.id'])
