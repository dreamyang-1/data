from copy import deepcopy
import pytest

import catalog_value_candidates as candidates
from catalog_release import CatalogEvidenceError
from test_catalog_value_sources import catalog, business, attribute_id, source, SCOPE
from catalog_value_sources import field_identity


class Connection:
    def __init__(self, *, index=True, access='range', rows=None):
        self.index=index; self.access=access; self.rows=rows or ['Alpha']; self.sql=[]; self.closed=False; self.rolled_back=False
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args=None): self.sql.append((sql,args))
    def fetchall(self):
        sql=self.sql[-1][0]
        if sql.startswith('SHOW'):
            return [('hospitals',1,'idx',1,'city','A',100,None,None,'','BTREE')] if self.index else []
        if sql.startswith('EXPLAIN'): return [(1,'SIMPLE','hospitals',None,self.access,'idx','idx')]
        return [(v,) for v in self.rows]
    def rollback(self): self.rolled_back=True
    def close(self): self.closed=True


@pytest.mark.parametrize('value', ['Alpha', 'A%_!', "X' OR 1=1 --"])
def test_targeted_reader_is_parameterized_indexed_scoped_and_readonly(monkeypatch, value):
    import mysql_tool as mysql
    row=source(); field=field_identity(row,SCOPE); connection=Connection()
    monkeypatch.setattr(candidates,'_definition_rows',lambda *a,**kw:[deepcopy(row)])
    monkeypatch.setattr(mysql.pymysql,'connect',lambda **kw:connection)
    candidates.observe_candidates(SCOPE,field,value)
    assert connection.rolled_back and connection.closed
    assert connection.sql[0][0]=='START TRANSACTION READ ONLY'
    sql,params=connection.sql[-1]
    assert value not in sql and 'WHERE `city` LIKE %s' in sql and 'LIMIT %s' in sql
    assert params==(value.replace('!','!!').replace('%','!%').replace('_','!_')+'%',9)
    assert len(connection.sql)==4


@pytest.mark.parametrize('fault', ['index', 'scan', 'scope', 'mapping'])
def test_targeted_query_fails_before_source_select_without_authority_or_index(monkeypatch, fault):
    import mysql_tool as mysql
    row=source(); field=field_identity(row,SCOPE); connection=Connection(index=fault!='index',access='ALL' if fault=='scan' else 'range')
    if fault=='scope': row['business_domain_id']=206
    if fault=='mapping': row['mapping_column']='other'
    monkeypatch.setattr(candidates,'_definition_rows',lambda *a,**kw:[deepcopy(row)])
    monkeypatch.setattr(mysql.pymysql,'connect',lambda **kw:connection)
    with pytest.raises(CatalogEvidenceError): candidates.observe_candidates(SCOPE,field,'Alpha')
    assert not any(sql.startswith('SELECT') for sql,_ in connection.sql)


@pytest.mark.parametrize('limit', [True,0,9,64])
def test_targeted_limit_not_expanded(monkeypatch, limit):
    with pytest.raises(CatalogEvidenceError,match='QUERY_INVALID'):
        candidates.observe_candidates(SCOPE,field_identity(source(),SCOPE),'Alpha',limit)


def test_pin_requires_exact_empty_scope_and_current_finish(monkeypatch, catalog, business):
    pin=catalog[0].pin(81,[205]); attribute=attribute_id(pin)
    with pytest.raises(CatalogEvidenceError,match='REQUIRES_EMPTY'): pin.search_entity_values(attribute,'Alpha')
    business[0].clear(); pin.lookup_entity_values(attribute,'Alpha')
    rows=['Alpha City']
    monkeypatch.setattr(candidates,'query_candidates',lambda *a:list(rows))
    assert pin.search_entity_values(attribute,'Alpha')['values']==rows
    rows.append('Alpha County')
    with pytest.raises(CatalogEvidenceError,match='CHANGED_DURING_READ'): pin.finish()


def test_pin_targeted_budget(monkeypatch, catalog, business):
    pin=catalog[0].pin(81,[205]); attribute=attribute_id(pin); business[0].clear()
    pin.lookup_entity_values(attribute,'Alpha'); calls=[]
    monkeypatch.setattr(candidates,'query_candidates',lambda *a:calls.append(a) or [])
    for _ in range(3): pin.search_entity_values(attribute,'Alpha')
    with pytest.raises(CatalogEvidenceError,match='BUDGET'): pin.search_entity_values(attribute,'Alpha')
    assert len(calls)==3
