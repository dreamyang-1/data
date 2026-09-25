from unittest.mock import Mock

import pymysql
import pytest
from pymysql.constants import SERVER_STATUS

from bound_sql import BoundSQLInvalid, parameterize_sql, validate_bound_sql, statement_fingerprint
from sql_translator_prod import SQLTranslatorProd
from test_execution_evidence import FakeCursor, FakeConnection


@pytest.mark.parametrize('mode,value,expected',[
    (0,'a\\b',"SELECT 'a\\\\b' AS value"),
    (1,'a\\b',"SELECT 'a\\b' AS value"),
    (0,"O'Reilly","SELECT 'O\\'Reilly' AS value"),
    (1,"O'Reilly","SELECT 'O''Reilly' AS value"),
    (0,'a\x00b',"SELECT 'a\\0b' AS value"),
    (1,'a\x00b',"SELECT 'a\x00b' AS value")])
def test_installed_pymysql_binds_for_the_actual_connection_mode_without_connecting(mode,value,expected):
    connection=pymysql.connect(defer_connect=True)
    connection.server_status=SERVER_STATUS.SERVER_STATUS_NO_BACKSLASH_ESCAPES if mode else 0
    query=parameterize_sql('SELECT __v2_bind_0__ AS value',{'v2_p0':value})
    assert connection.cursor().mogrify(query,{'v2_p0':value})==expected
    assert connection._sock is None


@pytest.mark.parametrize('sql,params',[
    ("SELECT '%(v2_p0)s'",{'v2_p0':'value'}),
    ('SELECT %(v2_p0)s + %(v2_p0)s',{'v2_p0':1}),
    ('SELECT %(v2_p1)s',{'v2_p0':1}),('SELECT 1',{'v2_p0':1}),
    ('SELECT %s',{'v2_p0':1}),('SELECT %(v2_p0)d',{'v2_p0':1}),
    ('SELECT 1 # %(v2_p0)s',{'v2_p0':1}),
    ('SELECT 1 /* %(v2_p0)s */',{'v2_p0':1}),
    ("SELECT 'a\\b', %(v2_p0)s",{'v2_p0':1}),
    ('SELECT %(v2_p0)s',{'v2_p0':float('nan')}),
    ('SELECT %(v2_p0)s',{'v2_p0':['nested']}),
    ('SELECT %(wrong)s',{'wrong':'value'})])
def test_quoted_missing_repeated_or_untyped_parameters_are_rejected(sql,params):
    with pytest.raises(BoundSQLInvalid):validate_bound_sql(sql,params)


def test_literal_percent_modulo_and_values_are_not_format_instructions():
    params={'v2_p0':'50% %(v2_p0)s'}
    sql=parameterize_sql("SELECT 5 % 2 AS value FROM t WHERE name LIKE 'A%' AND other=__v2_bind_0__",params)
    assert '5 %% 2' in sql and "'A%%'" in sql
    connection=pymysql.connect(defer_connect=True);connection.server_status=0
    rendered=connection.cursor().mogrify(sql,params)
    assert "5 % 2" in rendered and "name LIKE 'A%'" in rendered
    assert "other='50% %(v2_p0)s'" in rendered


@pytest.mark.parametrize('sql',["SELECT '__v2_bind_0__'",'SELECT __v2_bind_0__,__v2_bind_0__','SELECT __v2_bind_1__'])
def test_compiler_marker_cannot_collide_with_catalog_text(sql):
    with pytest.raises(BoundSQLInvalid):parameterize_sql(sql,{'v2_p0':'value'})


class ParameterCursor(FakeCursor):
    def __init__(self, original=None, fail=False):
        super().__init__();self.bound=[];self.original=original;self.fail=fail
    def execute(self,sql,parameters=None):
        if parameters is not None:
            if self.fail:raise RuntimeError('private value '+str(parameters))
            self.bound.append((sql,dict(parameters)))
        elif self.original is not None:
            self.original['v2_p0']='mutated outside'
        super().execute(sql)


def execute(monkeypatch,params,*,cursor=None,fingerprint=None):
    cursor=cursor or ParameterCursor();connection=FakeConnection(cursor)
    monkeypatch.setattr(pymysql,'connect',Mock(return_value=connection))
    query='SELECT order_id, amount FROM orders WHERE name=%(v2_p0)s'
    result=SQLTranslatorProd.execute_sql_on_data_source(query,{'db_type':'mysql'},parameters=params,
        parameter_fingerprint=fingerprint if fingerprint is not None else statement_fingerprint(query,params))
    return query,result,cursor


def test_existing_executor_passes_copied_values_to_driver_and_binds_snapshot(monkeypatch):
    params={'v2_p0':"a\\'b"};cursor=ParameterCursor(original=params)
    query,result,cursor=execute(monkeypatch,params,cursor=cursor)
    assert result['success'] and result['quality_status']=='PASS'
    assert cursor.bound==[(query,{'v2_p0':"a\\'b"})]
    _,other,_=execute(monkeypatch,{'v2_p0':'different'})
    assert result['snapshot_id']!=other['snapshot_id']


def test_changed_parameter_receipt_is_rejected_before_connect(monkeypatch):
    _,result,_=execute(monkeypatch,{'v2_p0':'value'},fingerprint='stale')
    assert result['error_code']=='SQL_SAFETY_REJECTED'
    pymysql.connect.assert_not_called()


def test_parameter_error_does_not_echo_business_values(monkeypatch):
    _,result,_=execute(monkeypatch,{'v2_p0':'private-fixture'},cursor=ParameterCursor(fail=True))
    assert result['error_code']=='SQL_PARAMETER_EXECUTION_FAILED' and 'private-fixture' not in str(result)


@pytest.mark.parametrize('op,value',[
    ('=',"a\\'b"),('!=','%s'),('IN',['A','B']),('NOT IN',[None]),('BETWEEN',[10,20]),
    ('LIKE','A%'),('NOT LIKE','B_'),('IS NULL',None)])
def test_real_pinned_consumer_binds_each_literal_and_keeps_original_pin(op,value):
    from test_pinned_catalog import Pin,asl
    from pinned_catalog import translate_pinned_catalog,_digest
    from semantic_scope import RequestScope
    pin=Pin();query=asl();query['filters']=[dict(field='orders.name',operator=op,value=value)]
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),query,parameterized=True)
    assert result['success'],result
    expected=[] if op=='IS NULL' else value if isinstance(value,list) else [value]
    assert list(result['sql_parameters'].values())==expected
    assert result['sql_parameter_contract']['asl_fingerprint']==_digest(query)
    assert result['sql_parameter_contract']['statement_fingerprint']==statement_fingerprint(result['sql'],result['sql_parameters'])
    pin.finish.assert_called_once()


def test_parameterized_query_still_checks_final_native_pin(monkeypatch):
    from test_pinned_catalog import Pin,asl
    from pinned_catalog import translate_pinned_catalog
    from semantic_scope import RequestScope
    pin=Pin();pin.finish.side_effect=ValueError('changed catalog')
    result=translate_pinned_catalog(pin,RequestScope(81,(205,)),asl(),parameterized=True)
    assert not result['success'] and 'sql_parameters' not in result


def test_parameter_mode_is_not_truthy_configuration():
    from test_pinned_catalog import Pin,asl
    from pinned_catalog import translate_pinned_catalog
    from semantic_scope import RequestScope
    result=translate_pinned_catalog(Pin(),RequestScope(81,(205,)),asl(),parameterized='true')
    assert result['error_code']=='PINNED_PARAMETER_MODE_INVALID'
