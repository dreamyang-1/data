import sqlite3

import pytest

from app.semantic_v2 import models as m
from test_v2_asl2_lowering import provider,current,bind,args,sql


def detail(session,value):
    return m.DetailRowsPayload(source_entity=bind(session,'ENTITY','orders','SOURCE_ENTITY'),
        projection_spec=m.ProjectionSpec(items=[m.ProjectionItem(output_field_id='id',
            ref=bind(session,'ATTRIBUTE','id','PROJECTION_FIELD'),role='PROJECTION_FIELD',position=0)]),
        filters=m.Predicate(field_ref=bind(session,'ATTRIBUTE','name','FILTER_FIELD'),operator='EQ',
            value=m.StringValue(value=value),source='CURRENT_EXPLICIT',scope='CURRENT_TASK'))


@pytest.mark.parametrize('value',['normal','a\\b',"O'Reilly",'a\x00b','测试医院','%s %(v2_p0)s',"x' OR 1=1 --",'😀'])
def test_typed_value_stays_data_through_bound_sql_and_actual_fixture_execution(provider,value):
    session=current(provider)
    lower,result=session.compile_asl2(sql_planner=sql,**args(session,detail(session,value)))
    parameters=result['sql_parameters'];template=result['sql']
    assert parameters=={'v2_p0':value} and value not in template
    assert result['sql_parameter_contract']['parameter_count']==1
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE orders(id INTEGER,name TEXT,amount INTEGER,ordered_at TEXT)')
        db.executemany('INSERT INTO orders(id,name) VALUES (?,?)',[(1,value),(2,'unrelated')])
        statement=template % {key:':'+key for key in parameters}
        assert db.execute(statement,parameters).fetchall()==[(1,)]
    assert lower.can_execute_safely is False


@pytest.mark.parametrize('fault',['missing_values','missing_receipt','sql','value','asl','style','count'])
def test_changed_or_missing_binding_receipt_never_leaves_agent(provider,fault):
    session=current(provider)
    def planner(pin,scope,asl,**policy):
        result=sql(pin,scope,asl,**policy)
        if fault=='missing_values':result.pop('sql_parameters')
        elif fault=='missing_receipt':result.pop('sql_parameter_contract')
        elif fault=='sql':result['sql']+=' '
        elif fault=='value':result['sql_parameters']['v2_p0']='other'
        elif fault=='asl':result['sql_parameter_contract']['asl_fingerprint']='other'
        elif fault=='style':result['sql_parameter_contract']['style']='inline'
        elif fault=='count':result['sql_parameter_contract']['parameter_count']=2
        return result
    with pytest.raises(ValueError,match='ASL2_SQL_PARAMETER_CONTRACT_MISMATCH'):
        session.compile_asl2(sql_planner=planner,**args(session,detail(session,'bound value')))


def test_callback_cannot_downgrade_parameterized_request_to_inline_sql(provider):
    session=current(provider)
    def planner(pin,scope,asl,**policy):
        assert policy.pop('parameterized') is True
        return sql(pin,scope,asl,**policy)
    with pytest.raises(ValueError,match='ASL2_SQL_PARAMETER_CONTRACT_MISMATCH'):
        session.compile_asl2(sql_planner=planner,**args(session,detail(session,'bound value')))
