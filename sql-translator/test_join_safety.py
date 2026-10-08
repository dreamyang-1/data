"""Disconnected joins never reach execution; normal bridge paths stay usable."""
from copy import deepcopy
import json
import sqlite3
from threading import BoundedSemaphore
from unittest.mock import Mock

import pytest

from sql_join_safety import SQLJoinError, validate_join_connectivity
from sql_translator_prod import SQLTranslatorProd
from test_execution_evidence import FakeConnection, FakeCursor
from test_sql_translator_hardening import translator


def bridge_fixture(reverse_key=False, source_mapping=True, target_mapping=True):
    t = translator()
    key = 'hospital.id = bridge.hospital_id' if not reverse_key else 'bridge.dept_code = department.code'
    t.loader.entities = {
        'hospital': {'entity_code': 'hospital', 'physical_table_join': {'base_table': 'hospital'},
                     'attributes': [], 'relations': [{'target_entity': 'department', 'join_key': key}],
                     'sub_table_mappings': ([{'sub_table_name': 'bridge', 'main_join_column': 'id',
                                             'sub_join_column': 'hospital_id'}] if source_mapping else [])},
        'department': {'entity_code': 'department', 'physical_table_join': {'base_table': 'department'},
                       'attributes': [], 'relations': [],
                       'sub_table_mappings': ([{'sub_table_name': 'bridge', 'main_join_column': 'code',
                                               'sub_join_column': 'dept_code'}] if target_mapping else [])}}
    return t


@pytest.mark.parametrize('reverse_key', [False, True])
@pytest.mark.parametrize('direction', [('hospital', 'department'), ('department', 'hospital')])
def test_shared_bridge_mapping_uses_the_correct_endpoint(reverse_key, direction):
    t = bridge_fixture(reverse_key)
    before = deepcopy(t.loader.entities)
    path = t._find_join_path(*direction, '113')
    assert path and path.count('JOIN bridge ') == 1
    sql = 'SELECT hospital.id, department.code FROM ' + direction[0] + ' ' + path
    validate_join_connectivity(sql)
    with sqlite3.connect(':memory:') as db:
        db.executescript('CREATE TABLE hospital(id INTEGER); CREATE TABLE department(code TEXT); '
                         'CREATE TABLE bridge(hospital_id INTEGER,dept_code TEXT); '
                         "INSERT INTO hospital VALUES (1); INSERT INTO department VALUES ('A'),('B'); "
                         "INSERT INTO bridge VALUES (1,'A');")
        rows = db.execute(sql).fetchall()
    assert (1, 'B') not in rows and (1, 'A') in rows
    assert t.loader.entities == before


def test_missing_other_endpoint_never_replays_the_existing_edge():
    t = bridge_fixture(target_mapping=False)
    assert t._find_join_path('hospital', 'department', '113') is None


def test_bad_shortcut_does_not_hide_a_longer_declared_path():
    t = bridge_fixture()
    t.loader.entities['hospital']['relations'].insert(0, {
        'target_entity': 'department', 'join_key': 'hospital.id = hospital.id'})
    path = t._find_join_path('hospital', 'department', '113')
    assert 'JOIN bridge' in path and 'hospital.id = hospital.id' not in path


BAD_SQL = 'SELECT department.code FROM hospital LEFT JOIN bridge ON hospital.id=bridge.hospital_id LEFT JOIN department ON hospital.id=bridge.hospital_id'


@pytest.mark.parametrize('sql', [BAD_SQL,
    'SELECT a.id FROM a JOIN b ON a.id = a.parent_id',
    'SELECT a.id FROM a JOIN b ON b.id = b.parent_id',
    'SELECT a.id FROM a JOIN b ON c.id = b.id',
    'SELECT a.id FROM a JOIN b ON c.id=b.id JOIN c ON a.id=c.id',
    'SELECT a.id FROM a JOIN b ON a.id=b.id OR 1=1',
    'SELECT a.id FROM a JOIN b ON (a.id=b.id OR b.active=1)',
    'SELECT a.id FROM a JOIN b ON a.x=a.y AND b.x=b.y',
    'SELECT a.id FROM a JOIN b ON 1=1',
    'SELECT a.id FROM a, b', 'SELECT a.id FROM a CROSS JOIN b',
    'SELECT a.id FROM a JOIN b', 'SELECT a.id FROM a NATURAL JOIN b',
    'SELECT a.id FROM a JOIN b ON a.id=b.id JOIN b ON a.id=b.id',
    'SELECT a.id FROM a JOIN b ON SELECT a.id FROM a',
    "SELECT a.id FROM a JOIN b ON a.id=a.id AND 'b.id=a.id'= 'b.id=a.id'",
    'SELECT a.id FROM a JOIN b AS x ON b.id=a.id',
    'SELECT 1 UNION SELECT a.id FROM a JOIN b ON a.id=a.id',
    'SELECT /*+ MAX_EXECUTION_TIME(99999999) */ 1',
    'SELECT /*!50000 SQL_NO_CACHE */ 1',
    'SELECT /*+ SET_VAR(max_execution_time=0) */ 1',
])
def test_invalid_relation_rejected_before_connect(monkeypatch, sql):
    connect = Mock(side_effect=AssertionError('must not connect'))
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', connect)
    with pytest.raises(SQLJoinError): validate_join_connectivity(sql)
    result = SQLTranslatorProd.execute_sql_on_data_source(sql, {'db_type': 'mysql'})
    assert not result['success'] and not result.get('business_query_submitted')
    connect.assert_not_called()


@pytest.mark.parametrize('sql', [
    'SELECT a.id FROM a LEFT JOIN b ON b.id=a.id',
    'SELECT a.id FROM a JOIN b ON (a.id=b.id AND b.active=1)',
    'SELECT a.id FROM a JOIN b ON a.id=b.id OR a.other_id=b.id',
    'SELECT a.id FROM a JOIN b ON COALESCE(b.id,0)=a.id',
    'SELECT a.id FROM a JOIN b ON a.date BETWEEN b.start_date AND b.end_date',
    'SELECT a.id FROM a JOIN b USING (id)',
    'SELECT a.id FROM a JOIN b ON a.id=b.id JOIN c ON b.id=c.id',
    'SELECT a.id FROM db.a AS a LEFT JOIN db.a AS b ON b.parent_id=a.id',
    'SELECT a.id FROM `a` a LEFT OUTER JOIN `b` b ON `a`.`id`=`b`.`id`',
    "SELECT 'JOIN x ON x.id=y.id' AS text FROM a JOIN b ON a.id=b.id",
    'SELECT /* regular note */ a.id FROM a JOIN b ON a.id=b.id',
    'SELECT 1 UNION ALL SELECT a.id FROM a JOIN b ON a.id=b.id',
    'SELECT a.id FROM a WHERE a.id IN (SELECT b.id FROM b JOIN c ON c.id=b.id)',
])
def test_valid_join_shapes_and_independent_subqueries_remain_valid(sql):
    validate_join_connectivity(sql)


def test_execute_entry_rejects_bad_sql_before_data_source_lookup():
    t = SQLTranslatorProd.__new__(SQLTranslatorProd)
    t.fetch_data_source = Mock(side_effect=AssertionError('No metadata access'))
    result = t.execute_sql_only(BAD_SQL, '113')
    assert result['error_code'] == 'SQL_JOIN_INVALID'
    t.fetch_data_source.assert_not_called()


def test_translation_error_is_actionable_without_echoing_private_text():
    result = SQLTranslatorProd._controlled_translation_failure(
        SQLJoinError('SQL_JOIN_INVALID: private metadata here'), False)
    assert result['error_code'] == 'SQL_JOIN_INVALID'
    assert '关联' in result['error'] and 'private metadata' not in json.dumps(result)


def test_normal_public_execution_always_sets_server_and_network_limits(monkeypatch):
    cursor = FakeCursor(); conn = FakeConnection(cursor); connect = Mock(return_value=conn)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', connect)
    result = SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id, amount FROM orders', {'db_type': 'mysql'})
    assert result['success'] and result['quality_checks']['statement_timeout_enforced']
    assert cursor.executed[0] == 'SET SESSION MAX_EXECUTION_TIME = 60000'
    assert 'SET SESSION lock_wait_timeout = 60' in cursor.executed
    assert connect.call_args.kwargs['read_timeout'] == 65 and connect.call_args.kwargs['connect_timeout'] == 10
    assert conn.closed


def test_mariadb_uses_its_server_timeout_setting(monkeypatch):
    cursor = FakeCursor(); conn = FakeConnection(cursor)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', Mock(return_value=conn))
    result = SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id FROM orders', {'db_type': 'MariaDB'})
    assert result['success'] and cursor.executed[0] == 'SET SESSION max_statement_time = 60'


@pytest.mark.parametrize('setting', ['MAX_EXECUTION_TIME', 'lock_wait_timeout'])
def test_no_unbounded_fallback_if_server_timeout_cannot_be_set(monkeypatch, setting):
    class Cursor(FakeCursor):
        def execute(self, statement):
            if setting in statement: raise RuntimeError('unsupported')
            super().execute(statement)
    cursor=Cursor(); conn=FakeConnection(cursor)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', Mock(return_value=conn))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id FROM orders', {'db_type':'mysql'})
    assert result['error_code']=='EXECUTION_TIMEOUT_REQUIRED' and conn.closed
    assert not any(s.startswith('SELECT order_id') for s in cursor.executed)


@pytest.mark.parametrize('code,message', [(3024,'statement expired'),(1969,'statement expired'),
                                       (1205,'lock wait expired'),(2013,'timed out')])
def test_timeout_stops_query_without_retry_and_releases_connection(monkeypatch, code, message):
    class Cursor(FakeCursor):
        def execute(self, statement):
            if statement.startswith('SELECT order_id'): raise RuntimeError(code,message)
            super().execute(statement)
    conn=FakeConnection(Cursor())
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', Mock(return_value=conn))
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT order_id FROM orders', {'db_type':'mysql'})
    assert result['error_code']=='SQL_EXECUTION_TIMEOUT' and not result['retryable'] and conn.closed


def test_query_capacity_is_bounded_and_slot_is_released_on_failure(monkeypatch):
    slots=BoundedSemaphore(1); slots.acquire()
    monkeypatch.setattr('sql_translator_prod._QUERY_SLOTS', slots)
    connect=Mock(side_effect=RuntimeError('connect failed'))
    monkeypatch.setattr('sql_translator_prod.pymysql.connect', connect)
    result=SQLTranslatorProd.execute_sql_on_data_source('SELECT 1', {'db_type':'mysql'})
    assert result['error_code']=='SQL_QUERY_CAPACITY_EXCEEDED';connect.assert_not_called()
    slots.release()
    assert not SQLTranslatorProd.execute_sql_on_data_source('SELECT 1', {'db_type':'mysql'})['success']
    assert slots.acquire(blocking=False)
