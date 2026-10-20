"""A product filter must follow the order fact, not hospital administration."""
import json
import sqlite3
from copy import deepcopy

import pytest

from test_sql_translator_hardening import translator


def fixture():
    compiler = translator()
    fields = {
        'hospital': ['hospital_id', 'hospital_name', 'province_id'],
        'hospital_dept_relation': ['hospital_id', 'dept_code'],
        'department': ['dept_code'],
        'product_dept_relation': ['product_code', 'dept_code'],
        'sales_order': ['order_key', 'hospital_id', 'product_code'],
        'product': ['product_code'],
        'province': ['province_id'],
    }
    compiler.loader.entities = {table: {
        'entity_code': table, 'entity_name': table,
        'physical_table_join': {'base_table': table},
        'attributes': [{'attr_code': field, 'field_mapping': f'{table}.{field}'} for field in columns],
        'relations': [], 'sub_table_mappings': [],
    } for table, columns in fields.items()}
    # The administrative route deliberately precedes the equally short fact route.
    for source, target, key in [
        ('hospital', 'hospital_dept_relation', 'hospital.hospital_id = hospital_dept_relation.hospital_id'),
        ('hospital_dept_relation', 'department', 'hospital_dept_relation.dept_code = department.dept_code'),
        ('department', 'product_dept_relation', 'department.dept_code = product_dept_relation.dept_code'),
        ('hospital', 'sales_order', 'hospital.hospital_id = sales_order.hospital_id'),
        ('sales_order', 'product', 'sales_order.product_code = product.product_code'),
        ('product', 'product_dept_relation', 'product.product_code = product_dept_relation.product_code'),
        ('hospital', 'province', 'hospital.province_id = province.province_id'),
    ]:
        compiler.loader.entities[source]['relations'].append({'target_entity': target, 'join_key': key})
    compiler.loader.dimensions = {}
    compiler.loader.metrics = {'order_count': {
        'metric_code': 'order_count', 'metric_name': 'OrderCount', 'metric_level': 'atomic',
        'source_dependency': {'bind_entity': ['sales_order']},
        'calculation_rule': {'calc_formula': 'COUNT(DISTINCT sales_order.order_key)',
                             'global_filters': [], 'depend_metrics': []}, 'params': {},
    }}
    ast = {'version': '2.0', 'intent': 'query', 'subject': {'entity': 'hospital'},
           'metrics': [{'name': 'order_count', 'alias': 'OrderCount'}],
           'dimensions': [{'name': 'hospital.hospital_name'}],
           'filters': [{'field': 'product_dept_relation.dept_code', 'operator': '=', 'value': 'digestive'}],
           'having': [], 'sort': {'field': 'order_count', 'field_type': 'metric', 'direction': 'DESC'}, 'limit': 1}
    return compiler, ast


@pytest.mark.parametrize('subject', ['hospital', 'sales_order'])
def test_product_department_filters_order_products_for_either_subject(subject):
    compiler, ast = fixture()
    ast['subject']['entity'] = subject
    before = deepcopy(ast)
    sql = compiler.translate(json.dumps(ast), '81')
    assert ast == before
    assert 'JOIN hospital_dept_relation ' not in sql and 'JOIN department ' not in sql
    assert sql.count('JOIN product_dept_relation ') == 1
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE hospital (hospital_id TEXT, hospital_name TEXT, province_id TEXT);
            CREATE TABLE sales_order (order_key TEXT, hospital_id TEXT, product_code TEXT);
            CREATE TABLE product (product_code TEXT);
            CREATE TABLE product_dept_relation (product_code TEXT, dept_code TEXT);
            CREATE TABLE hospital_dept_relation (hospital_id TEXT, dept_code TEXT);
            CREATE TABLE department (dept_code TEXT);
            INSERT INTO hospital VALUES ('A','Hospital A','P'),('B','Hospital B','P');
            INSERT INTO sales_order VALUES ('1','A','other'),('2','A','other'),('3','B','digestive-product');
            INSERT INTO product VALUES ('other'),('digestive-product');
            INSERT INTO product_dept_relation VALUES ('other','other'),('digestive-product','digestive'),
                                                     ('digestive-product','digestive');
            INSERT INTO hospital_dept_relation VALUES ('A','digestive'),('B','other');
            INSERT INTO department VALUES ('digestive'),('other');
        ''')
        assert db.execute(sql).fetchall() == [('Hospital B', 1)]


@pytest.mark.parametrize('table', ['province', 'department'])
def test_registered_shorter_paths_are_preserved(table):
    compiler, _ = fixture()
    original = compiler._find_join_to_table('hospital', table, {'hospital'}, '81')
    actual = compiler._find_join_to_table('hospital', table, {'hospital'}, '81', metric_entities=['sales_order'])
    assert actual == original and 'JOIN sales_order ' not in actual


def test_explicit_catalog_hierarchy_anchor_keeps_priority():
    compiler, _ = fixture()
    actual = compiler._find_join_to_table('hospital', 'product_dept_relation',
        {'hospital', 'department'}, '81', preferred_entities=['department'], metric_entities=['sales_order'])
    assert actual == 'LEFT JOIN product_dept_relation ON department.dept_code = product_dept_relation.dept_code'


@pytest.mark.parametrize('owners', [[], ['sales_order', 'hospital']])
def test_missing_or_ambiguous_metric_owner_keeps_existing_route(owners):
    compiler, ast = fixture()
    compiler.loader.metrics['order_count']['source_dependency']['bind_entity'] = owners
    sql = compiler.translate(json.dumps(ast), '81')
    assert 'JOIN hospital_dept_relation ' in sql
