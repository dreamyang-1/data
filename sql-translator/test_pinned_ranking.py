import sqlite3

import pytest

from pinned_catalog import translate_pinned_catalog, _digest
from semantic_scope import RequestScope
from test_pinned_catalog import Pin, asl


def request(direction='DESC', nulls='EXCLUDE'):
    query = asl(); query['filters'] = []; query['sort'] = dict(field='total', field_type='metric', direction=direction)
    query['metrics'][0]['alias'] = metric = 'v2_' + 'a' * 40
    query['dimensions'][0].update(alias='v2_' + 'b' * 40, include_null_group=True)
    group = query['dimensions'][0]['alias']
    policy = dict(contract='pinned-ranking-v1', semantic_fingerprint='c' * 64,
        output_aliases=[group, metric], order_by=[dict(sql_alias=metric, direction=direction, nulls_policy=nulls),
        dict(sql_alias=group, direction='ASC', nulls_policy='EXCLUDE')], ties_policy='EXCLUDE_TIES', limit=5)
    return query, policy


@pytest.mark.parametrize('direction,nulls', [(d,n) for d in ('ASC','DESC') for n in ('FIRST','LAST','EXCLUDE')])
def test_actual_generated_ordering_matches_null_policy(direction, nulls):
    query, policy = request(direction, nulls); pin = Pin()
    result = translate_pinned_catalog(pin, RequestScope(81,(205,)), query, ordering_contract=policy)
    assert result['success'], result
    assert result['ordering_contract_hash'] == _digest(policy)
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE orders(amount INTEGER, name TEXT)')
        db.executemany('INSERT INTO orders VALUES (?, ?)', [(20,'B'), (10,'A'), (None,'C'), (20,'D'), (99,None)])
        rows = db.execute(result['sql']).fetchall()
    expected = [('A',10), ('B',20), ('D',20)] if direction == 'ASC' else [('B',20), ('D',20), ('A',10)]
    if nulls == 'FIRST': expected.insert(0, ('C',None))
    if nulls == 'LAST': expected.append(('C',None))
    assert rows == expected
    pin.finish.assert_called_once()


@pytest.mark.parametrize('fault', ['extra', 'ties', 'scope', 'alias', 'alias_injection', 'direction',
    'nulls', 'limit', 'bool_limit', 'fingerprint', 'empty', 'primary', 'projection', 'having'])
def test_malformed_or_unbound_private_ordering_never_finishes_pin(fault):
    query, policy = request(); pin = Pin()
    if fault == 'extra': policy['sort_field'] = 'orders.secret'
    elif fault == 'ties': policy['ties_policy'] = 'INCLUDE_TIES'
    elif fault == 'scope': policy['semantic_model_id'] = 82
    elif fault == 'alias': policy['order_by'][0]['sql_alias'] = 'unknown'
    elif fault == 'alias_injection': policy['output_aliases'][0] = '`; DROP TABLE orders; --'
    elif fault == 'direction': policy['order_by'][0]['direction'] = 'DESC LIMIT 1'
    elif fault == 'nulls': policy['order_by'][0]['nulls_policy'] = 'AUTO'
    elif fault == 'limit': policy['limit'] = 1
    elif fault == 'bool_limit': policy['limit'] = True; query['limit'] = True
    elif fault == 'fingerprint': policy['semantic_fingerprint'] = 'unbound'
    elif fault == 'empty': policy['order_by'] = []
    elif fault == 'primary': query['sort']['direction'] = 'ASC'
    elif fault == 'projection': query['metrics'][0]['alias'] = 'v2_' + 'd' * 40
    elif fault == 'having': query['having'] = ['SUM(orders.amount)>0']
    result = translate_pinned_catalog(pin, RequestScope(81,(205,)), query, ordering_contract=policy)
    assert not result['success'] and result['sql'] is None
    assert result['error_code'].startswith('PINNED_RANKING_'), result
    pin.finish.assert_not_called()


def test_existing_public_asl_sort_retains_single_key_behavior():
    query, _ = request()
    result = translate_pinned_catalog(Pin(), RequestScope(81,(205,)), query)
    assert result['success'] and 'ordering_contract_hash' not in result
    assert 'HAVING' not in result['sql']
