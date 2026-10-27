import copy
import json
import sqlite3

import pytest
from test_aggregate_display_fields import fixture


@pytest.mark.parametrize('detail', [True, False])
def test_overlapping_group_and_display_returns_one_column(detail):
    translator, ast = fixture()
    ast['dimensions'] = [{'name':'staff.code'}]
    ast['display_fields'] = [{'name':'staff.code'}]
    ast['sort'] = None
    if detail: ast['metrics'] = []
    before = copy.deepcopy(ast)
    sql = translator.translate(json.dumps(ast), '81')
    assert sql.split(' FROM ')[0].count('staff.code AS') == 1
    assert ast == before
    assert ('GROUP BY' in sql) is not detail


def test_display_only_detail_has_no_group_by():
    translator, ast = fixture()
    ast.update(metrics=[], dimensions=[], display_fields=[{'name':'staff.name'}], sort=None)
    sql = translator.translate(json.dumps(ast), '81')
    assert 'staff.name' in sql and 'GROUP BY' not in sql


def test_same_entity_label_is_retained_without_extra_rows():
    translator, ast = fixture()
    ast['display_fields'] = [{'name':'staff.name','alias':'姓名'}]
    sql = translator.translate(json.dumps(ast), '81')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE staff(code TEXT, name TEXT, company TEXT)')
        db.execute('CREATE TABLE orders(staff TEXT, amount REAL)')
        db.executemany('INSERT INTO staff VALUES (?,?,?)', [('a','同名',None),('b','同名',None)])
        db.executemany('INSERT INTO orders VALUES (?,?)', [('a',10),('a',20),('b',40)])
        rows = db.execute(sql).fetchall()
    assert rows == [('b','同名',40),('a','同名',30)]


def test_aggregate_display_does_not_invent_missing_group():
    translator, ast = fixture()
    ast['dimensions'] = []
    with pytest.raises(ValueError, match='唯一'):
        translator.translate(json.dumps(ast), '81')


def test_display_source_is_checked_before_execution():
    from types import SimpleNamespace
    from semantic_scope import ScopedTranslator, ScopeError
    planner=object.__new__(ScopedTranslator)
    planner.loader=SimpleNamespace(find_all_entity_codes_by_table=lambda table, model: [table])
    planner._get_entity=lambda code, model: {'data_source_id': '1' if code=='staff' else '2'}
    planner._get_dimension_entities=lambda name, model: []
    ast={'subject':{'entity':'staff'},'dimensions':[],
         'display_fields':[{'name':'company.name'}]}
    with pytest.raises(ScopeError, match='exactly one data source'):
        planner._plan_sources(ast, '81')
