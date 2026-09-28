"""Name predicates use labels; JOIN identity and code predicates remain exact."""
import json
import sqlite3

import pytest

from test_sql_translator_hardening import translator


def translate_filter(field, operator, value):
    t = translator()
    columns = {'dealer': ['dealer_code', 'dealer_name'],
               'sales_order': ['dealer_code', 'product_code'],
               'product': ['product_code', 'product_name']}
    t.loader.entities = {code: {'entity_code': code,
        'physical_table_join': {'base_table': code},
        'attributes': [{'field_mapping': code+'.'+col} for col in cols],
        'relations': [], 'sub_table_mappings': []} for code, cols in columns.items()}
    t.loader.entities['sales_order']['relations'] = [
        {'target_entity': code, 'join_key': f'sales_order.{code}_code = {code}.{code}_code'}
        for code in ('dealer', 'product')]
    ast = {'version': '2.0', 'intent': 'query', 'subject': {'entity': 'dealer'},
           'metrics': [], 'dimensions': [{'name': 'dealer.dealer_name'}],
           'filters': [{'field': field, 'operator': operator, 'value': value}],
           'time_context': None, 'sort': None, 'limit': None, 'having': [], 'ambiguity': []}
    return t.translate(json.dumps(ast), model_id='81')


@pytest.mark.parametrize('name', ['空心纤维血液透析器', 'Prismaflex M60 set', "O'Brien M60"])
def test_name_query_includes_all_same_name_codes_beyond_eight(name):
    sql = translate_filter('product.product_name', '=', name)
    assert 'product.product_name =' in sql
    assert 'sales_order.product_code = product.product_code' in sql
    assert 'product_code IN' not in sql
    with sqlite3.connect(':memory:') as c:
        c.executescript('CREATE TABLE dealer(dealer_code TEXT, dealer_name TEXT);'
                       'CREATE TABLE sales_order(dealer_code TEXT, product_code TEXT);'
                       'CREATE TABLE product(product_code TEXT PRIMARY KEY, product_name TEXT);')
        c.execute('INSERT INTO dealer VALUES (?,?)', ('D', 'test-dealer'))
        c.executemany('INSERT INTO product VALUES (?,?)', [(f'{i:05d}', name) for i in range(12)] + [('other', 'unrelated')])
        c.executemany('INSERT INTO sales_order VALUES (?,?)', [('D', f'{i:05d}') for i in range(12)] + [('D', 'other')])
        assert len(c.execute(sql).fetchall()) == 12


@pytest.mark.parametrize('code', ['00001', 'AbC-01', 'M60'])
def test_user_code_stays_quoted_exact_literal(code):
    sql = translate_filter('product.product_code', '=', code)
    assert f"product.product_code = '{code}'" in sql
    assert 'product.product_name' not in sql


@pytest.mark.parametrize('operator', ['IN', 'NOT IN'])
def test_multiple_names_are_not_replaced_by_codes(operator):
    sql = translate_filter('product.product_name', operator, ['名称甲', '名称乙'])
    assert f"product.product_name {operator} ('名称甲', '名称乙')" in sql
