"""Opt-in strict snapshot mode leaves the legacy executor contract unchanged."""
from unittest.mock import Mock

import pytest

from sql_translator_prod import SQLTranslatorProd
from test_execution_evidence import FakeCursor, FakeConnection


@pytest.mark.parametrize('failure',['read_only','snapshot','clock','clock_type'])
def test_required_snapshot_rejects_before_business_query(monkeypatch,failure):
    class Cursor(FakeCursor):
        def execute(self,statement):
            if (failure=='read_only' and statement=='SET TRANSACTION READ ONLY') or (
                failure=='snapshot' and statement=='START TRANSACTION WITH CONSISTENT SNAPSHOT') or (
                failure=='clock' and statement.startswith('SELECT UTC_TIMESTAMP')):
                raise RuntimeError('Synthetic snapshot failure')
            super().execute(statement)
        def fetchone(self):return {'data_as_of':'unverified'} if failure=='clock_type' else super().fetchone()
    cursor=Cursor();connection=FakeConnection(cursor)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',Mock(return_value=connection))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders',
        {'db_type':'mysql'},require_consistent_snapshot=True)
    assert result['error_code']=='READ_ONLY_SNAPSHOT_REQUIRED'
    assert not any(s.startswith('SELECT order_id') for s in cursor.executed) and connection.closed


def test_required_snapshot_preserves_successful_native_protocol(monkeypatch):
    connection=FakeConnection(FakeCursor())
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',Mock(return_value=connection))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders',
        {'db_type':'mysql'},require_consistent_snapshot=True)
    assert result['success'] and result['quality_checks']['read_only_transaction'] and connection.closed


@pytest.mark.parametrize('mode',[None,1,'false'])
def test_invalid_snapshot_option_cannot_enable_a_looser_mode(monkeypatch,mode):
    connect=Mock(side_effect=AssertionError('No connection'))
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',connect)
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT 1',{'db_type':'mysql'},require_consistent_snapshot=mode)
    assert result['error_code']=='SQL_SAFETY_REJECTED'
    connect.assert_not_called()


@pytest.mark.parametrize('value',[0,90_001,True,'30000'])
def test_invalid_execution_timeout_is_rejected_before_connect(monkeypatch,value):
    connect=Mock(side_effect=AssertionError('No connection'))
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',connect)
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT 1',{'db_type':'mysql'},
        require_consistent_snapshot=True,execution_timeout_ms=value)
    assert result['error_code']=='SQL_SAFETY_REJECTED'
    connect.assert_not_called()


def test_server_side_timeout_is_required_before_snapshot_and_business_query(monkeypatch):
    class Cursor(FakeCursor):
        def execute(self,statement):
            if statement.startswith('SET SESSION MAX_EXECUTION_TIME'):
                raise RuntimeError('unsupported')
            super().execute(statement)
    cursor=Cursor();connection=FakeConnection(cursor)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',Mock(return_value=connection))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders',
        {'db_type':'mysql'},require_consistent_snapshot=True,execution_timeout_ms=30_000)
    assert result['error_code']=='EXECUTION_TIMEOUT_REQUIRED'
    assert not any(s.startswith('SELECT order_id') for s in cursor.executed)
    assert connection.closed


def test_server_side_timeout_is_connection_local_and_reported(monkeypatch):
    cursor=FakeCursor();connection=FakeConnection(cursor)
    connect=Mock(return_value=connection)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',connect)
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders',
        {'db_type':'mysql'},require_consistent_snapshot=True,execution_timeout_ms=30_000)
    assert result['success'] and result['quality_checks']['statement_timeout_enforced']
    assert cursor.executed[:2]==['SET SESSION MAX_EXECUTION_TIME = 30000','SET TRANSACTION READ ONLY']
    assert connect.call_args.kwargs['read_timeout']==35 and connection.closed


def test_server_timeout_reports_submitted_unknown_outcome_without_retry(monkeypatch):
    class Cursor(FakeCursor):
        def execute(self,statement):
            if statement.startswith('SELECT order_id'):
                raise RuntimeError(3024,'maximum statement execution time exceeded')
            super().execute(statement)
    connection=FakeConnection(Cursor())
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',Mock(return_value=connection))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders',
        {'db_type':'mysql'},require_consistent_snapshot=True,execution_timeout_ms=30_000)
    assert result=={'success':False,'error':'Bound SQL execution timed out',
        'error_code':'SQL_EXECUTION_TIMEOUT','retryable':False,'business_query_submitted':True}
    assert connection.closed
