import sqlite3

import pytest

from app.semantic_v2 import models as m
from test_v2_asl2_lowering import provider, current, bind, payload, args, sql
from test_v2_ranking_lowering import ranked


def leaf(session, field, op='EQ', value='A'):
    literal = (m.NullValue() if value is None else m.NumberValue(value=value) if type(value) is int
               else m.StringValue(value=value))
    return m.Predicate(field_ref=bind(session,'ATTRIBUTE',field,'FILTER_FIELD'), operator=op,
        value=literal,source='CURRENT_EXPLICIT',scope='CURRENT_TASK')


def group(op,*children): return m.BooleanFilterGroup(operator=op,children=list(children))


def run_fixture(query):
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE orders(id INTEGER, name TEXT, amount INTEGER, ordered_at TEXT)')
        db.executemany('INSERT INTO orders VALUES(?,?,?,NULL)', [
            (1,'A',10),(2,'B',20),(3,'C',30),(4,None,40),(5,'A',50),(6,"O'Reilly",60)])
        cursor=db.execute(query)
        columns=[r[0] for r in cursor.description]
        return columns,[dict(zip(columns,row)) for row in cursor.fetchall()]


@pytest.mark.parametrize('case,expected', [
    ('or',[1,2,5]),('nested',[1,2]),('not',[3,6]),('null',[4]),('not_null',[1,2,3,5,6]),
    ('not_like',[2,3,6]),('not_or_null',[1,2,3,5]),('double_not',[1,5]),('quote',[2,6])])
def test_actual_sql_preserves_boolean_grouping_and_null_truth(provider,case,expected):
    session=current(provider)
    a=leaf(session,'name'); b=leaf(session,'name',value='B')
    expression={
        'or':group('OR',a,b), 'nested':group('AND',group('OR',a,b),leaf(session,'amount','LT',30)),
        'not':group('NOT',group('OR',a,b)), 'null':leaf(session,'name','IS_NULL',None),
        'not_null':leaf(session,'name','IS_NOT_NULL',None), 'not_like':leaf(session,'name','NOT_LIKE','A%'),
        'not_or_null':group('NOT',group('OR',leaf(session,'name','IS_NULL',None),leaf(session,'name',value="O'Reilly"))),
        'double_not':group('NOT',group('NOT',a)), 'quote':group('OR',b,leaf(session,'name',value="O'Reilly"))}[case]
    p=m.DetailRowsPayload(source_entity=bind(session,'ENTITY','orders','SOURCE_ENTITY'),filters=expression,
        projection_spec=m.ProjectionSpec(items=[m.ProjectionItem(output_field_id='id',ref=bind(session,'ATTRIBUTE','id','PROJECTION_FIELD'),role='PROJECTION_FIELD',position=0)]))
    lower,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert result['success'] and lower.filter_contract and lower.asl['filters']==[]
    columns,rows=run_fixture(result['sql'])
    assert sorted(r[columns[0]] for r in rows)==expected


def test_boolean_filters_and_ranking_share_both_receipts(provider):
    session=current(provider); p=ranked(session,limit=2)
    p.filters=group('OR',leaf(session,'name'),leaf(session,'name',value='B'))
    lower,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    assert result['filter_contract_hash'] and result['ordering_contract_hash']
    _,rows=run_fixture(result['sql'])
    assert [r[lower.output_bindings[0].sql_alias] for r in rows]==['A','B']
    assert [r[lower.output_bindings[-1].sql_alias] for r in rows]==[60,20]


def test_governed_metric_filter_is_not_an_or_branch(provider):
    from test_pinned_catalog import seal
    from test_catalog_publication import embed
    data=provider[1]; data['documents'][0]['metrics'][0]['calculation_rule']['global_filters']=[dict(condition='orders.id != 5')]
    seal(data)
    provider[0].publish(81,[205],embed_fn=embed,publication_id='filter-global',producer_revision='fixture',embedding_contract='fixture')
    session=current(provider)
    p=payload(session,filters=group('OR',leaf(session,'name'),leaf(session,'name',value='B')))
    lower,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    columns,rows=run_fixture(result['sql'])
    assert rows==[{columns[0]:30}]


@pytest.mark.parametrize('fault',['receipt','changed_receipt','authority','scope','field'])
def test_filter_tree_never_bypasses_current_pin_or_contract(provider,fault):
    session=current(provider); p=payload(session,filters=group('OR',leaf(session,'name'),leaf(session,'name',value='B')))
    def planner(pin,scope,asl,**policy):
        if fault=='authority':
            from test_pinned_catalog import seal
            provider[1]['source_identity_hash']='changed';seal(provider[1])
        if fault=='field':
            import json
            policy['filter_contract']=json.loads(json.dumps(policy['filter_contract']))
            policy['filter_contract']['expression']['children'][1]['field']='foreign.secret'
        result=sql(pin,scope,asl,**policy)
        if fault=='receipt':result.pop('filter_contract_hash')
        if fault=='changed_receipt':result['filter_contract_hash']='changed'
        if fault=='scope':result['authorized_scope_fingerprint']='changed'
        return result
    with pytest.raises(ValueError,match='ASL2_'):session.compile_asl2(sql_planner=planner,**args(session,p))


def test_filter_contract_is_immutable_and_clear_has_no_stale_policy(provider):
    session=current(provider)
    lower,_=session.compile_asl2(sql_planner=sql,**args(session,payload(session,filters=group('NOT',leaf(session,'name')))))
    with pytest.raises(TypeError):lower.filter_contract['expression']['operator']='AND'
    session=current(provider)
    cleared,result=session.compile_asl2(sql_planner=sql,**args(session,payload(session)))
    assert cleared.filter_contract is None and 'filter_contract_hash' not in result and 'WHERE' not in result['sql']


def test_nullable_group_is_not_silently_removed_by_flat_leaf_heuristics(provider):
    session=current(provider)
    p=payload(session,'GROUPED_AGGREGATE',filters=group('OR',leaf(session,'name','IS_NULL',None),leaf(session,'name',value='B')))
    lower,result=session.compile_asl2(sql_planner=sql,**args(session,p))
    _,rows=run_fixture(result['sql'])
    assert {r[lower.output_bindings[0].sql_alias] for r in rows}=={None,'B'}


@pytest.mark.parametrize('value',['a\\b','a\x00b'])
def test_sql_mode_dependent_literals_cannot_bypass_private_guard_in_simple_and(provider,value):
    session=current(provider)
    def planner(pin,scope,asl,**policy):
        assert policy.get('filter_contract')
        result=sql(pin,scope,asl,**policy)
        assert result['error_code']=='PINNED_FILTER_LITERAL_SQL_MODE_UNPROVEN'
        return result
    with pytest.raises(ValueError,match='ASL2_PINNED_SQL_PLANNING_REJECTED'):
        session.compile_asl2(sql_planner=planner,**args(session,payload(session,filters=leaf(session,'name',value=value))))
