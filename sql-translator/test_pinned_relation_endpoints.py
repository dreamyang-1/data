"""Declared endpoints stay within their scoped, governed physical owners."""
from copy import deepcopy
import sqlite3

import pytest

from pinned_catalog import _SnapshotLoader
from semantic_scope import RequestScope
from test_pinned_catalog import Pin, asl, seal, snapshot, translate


def add_entity(s, code, identifier):
    doc = s['documents'][0]
    entity = deepcopy(doc['entities'][0])
    entity.update(entity_id=str(identifier), entity_code=code, relations=[])
    for attr in entity['attributes']:
        attr['attribute_id'] = str(identifier) + ':' + attr['attribute_id']
        attr['field_mapping'] = code + '.' + attr['field_mapping'].split('.')[-1]
    doc['entities'].append(entity)
    source = deepcopy(s['physical_catalog']['sql_translation_sources']['entities'][0])
    source.update(entity_id=str(identifier), entity_code=code, main_table_name=code, sub_table_mappings=[])
    s['physical_catalog']['sql_translation_sources']['entities'].append(source)
    table = deepcopy(s['physical_catalog']['tables'][0])
    table.update(table_id=identifier, table_name=code)
    for field in table['fields']:
        field['table_id'] = identifier
    s['physical_catalog']['tables'].append(table)


def catalog(source='id', target='id'):
    s = snapshot()
    add_entity(s, 'customers', 2)
    s['documents'][0]['entities'][0]['relations'] = [dict(
        relation_code='ordered_by', relation_type='N:1', target_entity='customers',
        join_key=dict(source_field=source, target_field=target))]
    return seal(s)


def loader(s):
    return _SnapshotLoader(RequestScope(81, (205,)), seal(s))


def relation(s):
    return s['documents'][0]['entities'][0]['relations'][0]


def query():
    return {**asl(), 'metrics': [],
        'dimensions': [{'name': 'orders.amount'}, {'name': 'customers.name'}],
        'filters': [{'field': 'customers.name', 'operator': '=', 'value': 'Alice'}]}


@pytest.mark.parametrize('source,target', [
    ('id', 'id'), ('orders.id', 'id'), ('id', 'customers.id'), ('orders.id', 'customers.id')])
def test_unique_declared_endpoints_produce_correct_sql_without_mutating_pin(source, target):
    s = catalog(source, target)
    before = deepcopy(s)
    result = translate(Pin(s), query())
    assert result['success'], result
    assert s == before
    assert loader(s).unresolved_relationships == []
    assert loader(s).get_entity('orders', 81)['relations'][0]['join_key'] == 'orders.id = customers.id'
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE orders(id INT, amount INT, name TEXT);'
            'CREATE TABLE customers(id INT, name TEXT);'
            "INSERT INTO orders VALUES(1,10,'x'),(2,20,'x');"
            "INSERT INTO customers VALUES(1,'Alice'),(3,'Other');")
        assert db.execute(result['sql']).fetchall() == [(10, 'Alice')]


@pytest.mark.parametrize('identifier', ['customers', '2'])
def test_endpoint_entity_code_and_governed_id_are_equivalent(identifier):
    s = catalog(); relation(s)['target_entity'] = identifier
    assert loader(s).get_entity('orders', 81)['relations'][0]['target_entity'] == 'customers'


@pytest.mark.parametrize('fault', [
    'missing_source', 'missing_target', 'null', 'list', 'expression', 'empty', 'whitespace',
    'wrong_source_owner', 'wrong_target_owner', 'undeclared_table', 'unknown_target', 'extra_key', 'foreign_source'])
def test_unusable_declared_endpoint_does_not_authorize_a_join(fault):
    s = catalog(); r = relation(s); join = r['join_key']
    if fault == 'missing_source': join['source_field'] = 'absent'
    elif fault == 'missing_target': join['target_field'] = 'absent'
    elif fault == 'null': join['source_field'] = None
    elif fault == 'list': join['target_field'] = ['id']
    elif fault == 'expression': join['source_field'] = 'orders.id OR 1=1'
    elif fault == 'empty': join['source_field'] = ''
    elif fault == 'whitespace': join['source_field'] = ' id '
    elif fault == 'wrong_source_owner': join['source_field'] = 'customers.id'
    elif fault == 'wrong_target_owner': join['target_field'] = 'orders.id'
    elif fault == 'undeclared_table':
        add_entity(s, 'third', 3); join['source_field'] = 'third.id'
    elif fault == 'unknown_target': r['target_entity'] = 'outside_scope'
    elif fault == 'extra_key': join['inferred'] = True
    elif fault == 'foreign_source':
        s['physical_catalog']['sql_translation_sources']['entities'][1]['data_source_id'] = 8
        table = s['physical_catalog']['tables'][1]; table['data_source_id'] = 8
        for field in table['fields']: field['data_source_id'] = 8
    resolved = loader(s)
    assert resolved.unresolved_relationships == ['ordered_by']
    assert resolved.get_entity('orders', 81)['relations'][0]['join_key'] == ''
    assert not translate(Pin(seal(s)), query())['success']
    assert translate(Pin(seal(s)), asl())['success']


def add_subtable(s):
    add_entity(s, 'order_extra', 3)
    # Register a same-source governed sub-table of orders. No column-name
    # search may prefer the base table simply because it comes first.
    s['physical_catalog']['sql_translation_sources']['entities'][0]['sub_table_mappings'].append(
        dict(sub_table_name='order_extra', main_join_column='id', sub_join_column='id'))


@pytest.mark.parametrize('reverse', [False, True])
def test_base_subtable_collision_is_unresolved_even_with_unique_attribute_mapping(reverse):
    s = catalog(); add_subtable(s)
    if reverse:
        s['physical_catalog']['tables'].reverse()
        s['documents'][0]['entities'].reverse()
        s['physical_catalog']['sql_translation_sources']['entities'].reverse()
    assert loader(s).unresolved_relationships == ['ordered_by']
    assert not translate(Pin(seal(s)), query())['success']


@pytest.mark.parametrize('field', ['orders.id', 'order_extra.id'])
def test_explicit_qualified_endpoint_can_choose_declared_base_or_subtable(field):
    s = catalog(field, 'customers.id'); add_subtable(s)
    resolved = loader(s)
    assert resolved.unresolved_relationships == []
    assert resolved.get_entity('orders', 81)['relations'][0]['join_key'] == field + ' = customers.id'


def test_unqualified_column_unique_to_declared_subtable_resolves():
    s = catalog('parent_id', 'id'); add_subtable(s)
    table = s['physical_catalog']['tables'][-1]
    table['fields'].append({**table['fields'][0], 'field_id': 99, 'field_name': 'parent_id'})
    resolved = loader(s)
    assert resolved.unresolved_relationships == []
    assert resolved.get_entity('orders', 81)['relations'][0]['join_key'] == 'order_extra.parent_id = customers.id'


def test_global_registered_column_cannot_fill_an_absent_owned_endpoint():
    s = catalog('external_id', 'id'); add_entity(s, 'third', 3)
    table = s['physical_catalog']['tables'][-1]
    table['fields'].append({**table['fields'][0], 'field_id': 99, 'field_name': 'external_id'})
    assert loader(s).unresolved_relationships == ['ordered_by']


def test_source_owned_bridge_is_not_a_complete_target_entity_endpoint():
    s = catalog('orders.id', 'order_extra.id'); add_subtable(s)
    # The declaration names customers as target, but only joins orders to
    # orders' own bridge. There is no declared bridge -> customers endpoint.
    assert loader(s).unresolved_relationships == ['ordered_by']
    assert not translate(Pin(seal(s)), query())['success']


def test_current_scope_and_final_pin_acceptance_are_still_required():
    s = catalog()
    assert not translate(Pin(s), query(), RequestScope(81, (206,)))['success']
    pin = Pin(s); pin.finish.side_effect = ValueError('changed')
    result = translate(pin, query())
    assert not result['success'] and result['sql'] is None
    pin.finish.assert_called_once()
