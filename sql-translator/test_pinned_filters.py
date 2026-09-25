import sqlite3
import pytest

from pinned_catalog import translate_pinned_catalog, _digest
from semantic_scope import RequestScope
from test_pinned_catalog import Pin, asl, seal
from test_pinned_ranking import request as rank_request


def leaf(field='name',op='=',value='A'):
    return dict(field='orders.'+field,operator=op,value=value)


def group(op,*children): return dict(operator=op,children=list(children))


def policy(tree): return dict(contract='pinned-filter-tree-v1',semantic_fingerprint='c'*64,expression=tree)


def detail():
    query=asl();query.update(metrics=[],filters=[],dimensions=[dict(name='orders.id',alias='id')],limit=100)
    return query


def execute(query):
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE orders (id INTEGER, name TEXT, amount INTEGER)')
        db.executemany('INSERT INTO orders VALUES (?,?,?)',[(1,'A',10),(2,'B',20),(3,None,30),(4,"O'Reilly",40)])
        return sorted(r[0] for r in db.execute(query).fetchall())


@pytest.mark.parametrize('tree,expected',[
    (group('OR',leaf(),leaf(value='B')),[1,2]),
    (group('NOT',group('OR',leaf(),leaf(value='B'))),[4]),
    (group('AND',group('OR',leaf(),leaf(value='B')),leaf('amount','>',10)),[2]),
    (leaf(op='IS NULL',value=None),[3]),(leaf(op='IS NOT NULL',value=None),[1,2,4]),
    (leaf(op='NOT LIKE',value='A%'),[2,4]),
    (group('OR',leaf(op='IS NULL',value=None),leaf(value="O'Reilly")),[3,4]),
    (group('NOT',leaf(op='IN',value=['A','B'])),[4]),
    (group('NOT',leaf('amount','BETWEEN',[10,30])),[4]),
    (group('NOT',group('NOT',leaf())),[1]),
    (leaf(op='IN',value=[None]),[]), (group('NOT',leaf(op='IN',value=[None])),[])])
def test_boolean_and_null_operators_keep_sql_three_valued_logic(tree,expected):
    pin=Pin(); contract=policy(tree)
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),detail(),filter_contract=contract)
    assert result['success'],result
    assert execute(result['sql'])==expected
    assert result['filter_contract_hash']==_digest(contract)
    pin.finish.assert_called_once()


@pytest.mark.parametrize('fault',['extra','version','scope','fingerprint','empty_group','not_arity','boolean_op',
    'extra_leaf','leaf_field','operator','null_value','in_empty','between_arity','scalar_list','nonfinite',
    'backslash','nul','asl_conflict','policy_in_asl','depth','width'])
def test_invalid_private_filters_cannot_finish_catalog_pin(fault):
    query=detail(); contract=policy(group('OR',leaf(),leaf(value='B'))); pin=Pin()
    node=contract['expression']['children'][0]
    if fault=='extra':contract['sql']='SELECT secret'
    elif fault=='version':contract['contract']='legacy'
    elif fault=='scope':contract['business_domain_ids']=[]
    elif fault=='fingerprint':contract['semantic_fingerprint']='other'
    elif fault=='empty_group':contract['expression']['children']=[]
    elif fault=='not_arity':contract['expression']['operator']='NOT'
    elif fault=='boolean_op':contract['expression']['operator']='XOR'
    elif fault=='extra_leaf':node['condition']='1=1'
    elif fault=='leaf_field':node['field']='id'
    elif fault=='operator':node['operator']='= OR 1=1'
    elif fault=='null_value':node['operator']='IS NULL'
    elif fault=='in_empty':node.update(operator='IN',value=[])
    elif fault=='between_arity':node.update(operator='BETWEEN',value=[1])
    elif fault=='scalar_list':node['value']=['A']
    elif fault=='nonfinite':node['value']=float('nan')
    elif fault=='backslash':node['value']='a\\b'
    elif fault=='nul':node['value']='a\x00b'
    elif fault=='asl_conflict':query['filters']=[leaf()]
    elif fault=='policy_in_asl':query['filter_contract']=contract
    elif fault=='depth':
        for _ in range(21):contract['expression']=group('NOT',contract['expression'])
    elif fault=='width':contract['expression']['children']=[leaf()]*101
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),query,filter_contract=contract)
    assert not result['success'] and result['sql'] is None
    assert result['error_code'].startswith(('PINNED_FILTER_','PINNED_ASL_POLICY_')),result
    pin.finish.assert_not_called()


@pytest.mark.parametrize('field',['foreign.secret','orders.missing'])
def test_every_or_branch_requires_registered_scoped_field(field):
    contract=policy(group('OR',leaf(),leaf()));contract['expression']['children'][1]['field']=field
    pin=Pin()
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),detail(),filter_contract=contract)
    assert not result['success'] and result['sql'] is None
    pin.finish.assert_not_called()


def test_order_and_filter_cannot_come_from_different_semantic_plans():
    query,rank=rank_request(); filtering=policy(group('OR',leaf(),leaf(value='B')))
    filtering['semantic_fingerprint']='d'*64;pin=Pin()
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),query,ordering_contract=rank,filter_contract=filtering)
    assert result['error_code']=='PINNED_POLICY_PLAN_MISMATCH'
    pin.finish.assert_not_called()


def test_governed_filter_and_original_pin_acceptance_are_both_preserved():
    pin=Pin();pin.snapshot['documents'][0]['metrics'][0]['calculation_rule']['global_filters']=[dict(condition='orders.id != 1')]
    seal(pin.snapshot);pin.identity['catalog_version']=pin.snapshot['catalog_version']
    query=asl();query['filters']=[];contract=policy(group('OR',leaf(),leaf(value='B')))
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),query,filter_contract=contract)
    assert result['success'] and ' AND (orders.id != 1)' in result['sql']
    pin=Pin();pin.finish.side_effect=ValueError('private stale data')
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),query,filter_contract=contract)
    assert not result['success'] and 'private stale data' not in str(result)
