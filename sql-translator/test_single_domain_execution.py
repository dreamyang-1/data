"""Real catalog SQL and translator/API tests; only storage/transport is local."""
import copy
import io
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import api_server_prod as api
from semantic_scope import RequestScope, ScopedCatalog, ScopedLoader, ScopedTranslator, ScopeError
from sql_translator_prod import SemanticCatalog, SQLTranslatorProd
from test_semantic_scope_contract import ScopeRedis


@pytest.fixture
def catalog_db():
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.create_function('JSON_EXTRACT',2,lambda text,path:json.dumps([b.get('entity') for b in json.loads(text or '[]')]) if path=='$[*].entity' else None)
    db.create_function('JSON_QUOTE',1,json.dumps)
    db.create_function('JSON_CONTAINS',2,lambda text,value:int(json.loads(value) in json.loads(text or '[]')))
    schemas = {
        'entity_type': 'id TEXT, code TEXT, name TEXT, main_table_name TEXT, data_source_id INTEGER, status INTEGER',
        'indicator': 'id INTEGER, indicator_code TEXT, indicator_name TEXT, synonyms TEXT, unit TEXT, calculation_formula TEXT, indicator_level INTEGER, global_filters TEXT',
        'dimension': 'id INTEGER, dim_code TEXT, dim_name TEXT, dim_type TEXT, entity_attribute TEXT, update_time TEXT',
        'attribute_config': 'entity_type_id TEXT, mapping_table TEXT, mapping_column TEXT, is_main_attribute INTEGER, is_primary_key INTEGER, is_unique INTEGER, is_required INTEGER',
        'relation_config': 'code TEXT, type TEXT, source_entity_type_id TEXT, target_entity_type_id TEXT, source_table_column_name TEXT, target_table_column_name TEXT, status INTEGER',
        'entity_sub_table_mapping': 'entity_type_id TEXT, sub_table_name TEXT, main_join_column TEXT, sub_join_column TEXT',
        'entity_bind_indicator': 'entity_code TEXT, indicator_code TEXT, indicator_logic TEXT',
        'table': 'id INTEGER, name TEXT, data_source_id INTEGER, description TEXT, comment TEXT',
        'field': 'id INTEGER, table_id INTEGER, name TEXT, type TEXT, null_flag INTEGER, description TEXT, comment TEXT',
    }
    for name, columns in schemas.items():
        domain_column='' if name=='dimension' else 'business_domain_id INTEGER,'
        db.execute(f'CREATE TABLE semantic_model_{name} (semantic_model_id INTEGER, {domain_column} is_deleted INTEGER DEFAULT 0, {columns})')
    def put(kind, **row):
        db.execute(f"INSERT INTO semantic_model_{kind} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    for model, domain, code, source in [(81,205,'sales',10),(81,206,'inventory',11),(82,205,'foreign_sales',12),(81,-1,'shared',13)]:
        common=dict(semantic_model_id=model,business_domain_id=domain)
        put('entity_type',**common,id=code,code=code,name=code,main_table_name=code,data_source_id=source,status=1)
        put('indicator',**common,id=source,indicator_code=code+'_amount',indicator_name=code+' amount',synonyms=json.dumps(['销售额'] if code=='sales' else []),calculation_formula=f'SUM({code}.amount)',indicator_level=1)
        put('entity_bind_indicator',**common,entity_code=code,indicator_code=code+'_amount',indicator_logic='SUM')
        put('attribute_config',**common,entity_type_id=code,mapping_table=code,mapping_column='amount',is_required=1)
        put('dimension',semantic_model_id=model,id=source,dim_code=code+'_date',dim_name=code+' date',dim_type='time',update_time='2026-09-08',entity_attribute=json.dumps([{'entity':code,'mappingTable':code,'mappingColumn':'created_at'}]))
        put('table',**common,id=source,name=code,data_source_id=source)
        put('field',**common,id=source,table_id=source,name='created_at',type='DATETIME')
    put('relation_config',semantic_model_id=81,business_domain_id=205,code='bad_join',type='N:1',source_entity_type_id='sales',target_entity_type_id='inventory',source_table_column_name='sales-id',target_table_column_name='inventory-id',status=1)
    yield db
    db.close()


@pytest.fixture
def backend(catalog_db, monkeypatch):
    sql_reads=[]
    def query(self, sql, params=()):
        sql_reads.append((sql,params))
        return [dict(r) for r in catalog_db.execute(sql.replace('%s','?'),params)]
    monkeypatch.setattr(SemanticCatalog,'_query',query)
    base=SQLTranslatorProd()
    payloads={}
    for model,domain,code,source in [(81,205,'sales',10),(81,206,'inventory',11),(82,205,'foreign_sales',12),(81,-1,'shared',13)]:
        common=dict(semantic_model_id=str(model),business_domain_id=str(domain))
        payloads[f'semantic_model:{model}:entity:{code}']=dict(common,id=code,code=code,name=code,main_table_name=code,data_source_id=str(source),
            semantic_model_attribute_config=[dict(code='amount',mapping_table=code,mapping_column='amount')])
        payloads[f'semantic_model:{model}:metric:{code}_amount']=dict(common,code=code+'_amount',name=code+' amount',calc_formula=f'SUM({code}.amount)',bind_entity=[code])
        payloads[f'semantic_model:{model}:dimension:{code}_date']=dict(common,dim_code=code+'_date',dim_name=code+' date',dim_type='time',entity_attribute=[dict(entity=code,mappingTable=code,mappingColumn='created_at')])
        payloads[f'semantic_model:data_source:{source}']=dict(id=str(source),semantic_model_id=str(model),name='offline')
    base.loader._redis=ScopeRedis(payloads)
    executed=[]
    def execute(sql, config, watermark=None):
        executed.append((sql,config['id'],watermark))
        return dict(success=True,columns=['sales_amount'],data=[{'sales_amount':123}],row_count=1)
    monkeypatch.setattr(SQLTranslatorProd,'execute_sql_on_data_source',staticmethod(execute))
    monkeypatch.setattr(api,'get_translator',lambda:base)
    monkeypatch.setattr(api,'log',lambda _:None)
    return base,sql_reads,executed


def grant(domains=(205,),model=81,**kwargs):
    return RequestScope(model,domains,**kwargs)


def asl(entity='sales',metric='sales_amount'):
    return json.dumps(dict(subject={'entity':entity},metrics=[{'name':metric}],dimensions=[],filters=[],ambiguity=[]))


def request_api(method,body,*,path='/offline',arguments=()):
    handler=object.__new__(api.APIHandler)
    encoded=json.dumps(body).encode()
    handler.path=path
    handler.headers={'Content-Length':str(len(encoded))}
    handler.rfile=io.BytesIO(encoded)
    out=[]
    handler._send_response=lambda *v:out.append(v)
    getattr(handler,method)(*arguments)
    assert len(out)==1
    return out[0]


def test_actual_single_domain_translation_and_execution(backend):
    base,queries,executed=backend
    scope=grant()
    payload=dict(modelId='81',business_domain_ids=[205],authorized_semantic_scope=scope.payload(),asl=asl())
    status,plan=request_api('_handle_translate',payload)
    assert status==200 and plan['success'],plan
    assert 'sales' in plan['sql'] and 'inventory' not in plan['sql']
    assert plan['authorized_scope_fingerprint']==scope.evidence()['authorized_scope_fingerprint']
    status,result=request_api('_handle_execute',dict(payload,sql=plan['sql'],dataSourceId=plan['dataSourceId']))
    assert status==200 and result['success'],result
    assert len(executed)==1 and executed[0][1]=='10'
    assert result['business_domain_ids']==[205]
    assert base.loader._metric_cache=={} and base.loader._entity_cache=={}
    assert all('business_domain_id=205' in sql for sql,_ in queries)


@pytest.mark.parametrize('entity,metric',[('inventory','inventory_amount'),('foreign_sales','foreign_sales_amount'),('shared','shared_amount'),('sales','inventory_amount')])
def test_foreign_and_shared_semantic_references_never_execute(backend,entity,metric):
    base,_,executed=backend
    translator=ScopedTranslator(grant(),base)
    result=translator.execute_query(asl(entity,metric),'81')
    assert result['success'] is False,result
    assert executed==[]


@pytest.mark.parametrize('tamper',['sql','source','missing_asl','model'])
def test_split_execution_requires_current_semantic_proof(backend,tamper):
    base,_,executed=backend
    translator=ScopedTranslator(grant(),base)
    plan=translator.translate_only(asl(),'81')
    assert plan['success'],plan
    body=dict(modelId='81',business_domain_ids=[205],asl=asl(),sql=plan['sql'],dataSourceId='10')
    if tamper=='sql':body['sql']='SELECT amount FROM inventory'
    if tamper=='source':body['dataSourceId']='11'
    if tamper=='missing_asl':del body['asl']
    if tamper=='model':body['modelId']='82'
    _,result=request_api('_handle_execute',body)
    assert result['success'] is False,result
    assert executed==[]


def test_scoped_cache_indexes_and_watermarks_do_not_cross_domains(backend):
    base,_,_=backend
    first=ScopedTranslator(grant(),base)
    other=ScopedTranslator(grant((206,)),base)
    assert first.loader.get_entity('sales','81')
    assert not other.loader.get_entity('sales','81')
    assert first.loader._find_entity_code_by_id('inventory','81') is None
    assert [d['dim_code'] for d in first.loader.iter_scoped_dimensions('81')]==['sales_date']
    assert [d['dim_code'] for d in other.loader.iter_scoped_dimensions('81')]==['inventory_date']
    assert first.loader._entity_cache is not other.loader._entity_cache
    assert base.loader._entity_cache=={}


def test_catalog_queries_filter_all_roles_before_storage_returns_rows(backend):
    base,queries,_=backend
    c=ScopedTranslator(grant(),base).catalog
    assert c.resolve_metrics(81,['sales amount'])['status']=='RESOLVED'
    assert c.resolve_metrics(81,['inventory amount'])['status']=='NOT_FOUND'
    assert c.dimension_metadata(81,'inventory_date') is None
    assert c.registered_temporal_fields(81)==['sales.created_at']
    assert set(c.entity_relationship_metadata(81))=={'sales'}
    assert c.entity_relationship_metadata(81)['sales']['relations']==[]
    assert c.attribute_metadata(81,['sales.amount','inventory.amount'])['inventory.amount']==[]
    assert c.definition('81:sales_amount','current')['business_domain_id']==205
    assert c.lineage('81:sales_amount','current')['business_lineage'][0]=='业务域:205'
    c.infer_unique_bridge_endpoint(81,'inventory','sales')
    assert all('business_domain_id=205' in sql for sql,_ in queries)


def test_unknown_catalog_table_fails_closed(backend):
    c=ScopedTranslator(grant(),backend[0]).catalog
    with pytest.raises(ScopeError,match='Unreviewed'):
        c._query('SELECT * FROM semantic_model_future_table WHERE semantic_model_id=%s',(81,))


@pytest.mark.parametrize('domain',[None,-1,206])
@pytest.mark.parametrize('kind',['entity','metric'])
def test_cache_payload_without_exact_domain_is_not_admitted(backend,domain,kind):
    base,_,_=backend
    code={'entity':'sales','metric':'sales_amount','dimension':'sales_date'}[kind]
    key=f'semantic_model:81:{kind}:{code}'
    base.loader.redis.payloads[key]['business_domain_id']=domain
    loader=ScopedLoader(grant(),base.loader)
    assert getattr(loader,'get_'+kind)(code,'81') is None


def test_request_local_indexes_survive_interleaved_domains(backend):
    base,_,_=backend
    def run(domain,code):
        loader=ScopedLoader(grant((domain,)),base.loader)
        for _ in range(5):
            assert loader.get_entity(code,'81')
            assert loader._find_entity_code_by_id('inventory' if domain==205 else 'sales','81') is None
        return set(loader._entity_id_to_code)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(run,205,'sales');b=pool.submit(run,206,'inventory')
        assert a.result()=={'sales'} and b.result()=={'inventory'}


def test_republish_between_translation_and_execution_invalidates_plan(backend):
    base,_,executed=backend
    first=ScopedTranslator(grant(),base).translate_only(asl(),'81')
    assert first['success'],first
    base.loader.redis.payloads['semantic_model:81:metric:sales_amount']['business_domain_id']='206'
    result=ScopedTranslator(grant(),base).execute_scoped(asl(),first['sql'],'81','10')
    assert result['success'] is False and executed==[]


def test_model_shared_dimension_projects_only_owned_entity_bindings(backend,catalog_db):
    catalog_db.execute('UPDATE semantic_model_dimension SET entity_attribute=? WHERE dim_code=?',
        (json.dumps([{'entity':'sales','mappingTable':'sales','mappingColumn':'created_at'},
                     {'entity':'inventory','mappingTable':'inventory','mappingColumn':'created_at'}]),'sales_date'))
    c=ScopedTranslator(grant(),backend[0]).catalog
    row=c.dimension_metadata(81,'sales_date')
    assert [b['entity'] for b in row['entity_attribute']]==['sales']
    assert 'business_domain_id' not in row
    assert [b['entity'] for b in ScopedTranslator(grant((206,)),backend[0]).catalog.dimension_metadata(81,'sales_date')['entity_attribute']]==['inventory']


@pytest.mark.parametrize('domain',[206,-1])
def test_current_mysql_publication_overrules_stale_redis_membership(backend,catalog_db,domain):
    base,_,executed=backend
    catalog_db.execute('UPDATE semantic_model_indicator SET business_domain_id=? WHERE indicator_code=?',(domain,'sales_amount'))
    result=ScopedTranslator(grant(),base).execute_query(asl(),'81')
    assert not result['success'] and not executed


def test_metric_formula_cannot_reference_foreign_domain_table(backend,catalog_db):
    catalog_db.execute('UPDATE semantic_model_indicator SET calculation_formula=? WHERE indicator_code=?',('SUM(inventory.amount)','sales_amount'))
    result=ScopedTranslator(grant(),backend[0]).translate_only(asl(),'81')
    assert result['code']=='SEMANTIC_SCOPE_MISMATCH'


@pytest.mark.parametrize('kind',['definition','lineage','relationship','combined'])
def test_single_domain_public_metadata_and_combined_endpoints(backend,kind):
    from urllib.parse import urlencode
    body=dict(semantic_model_id=81,modelId='81',business_domain_ids=[205])
    if kind=='definition':
        status,result=request_api('_handle_metric_definition',{},path='/definition?'+urlencode({'scope':json.dumps(grant().payload())}),arguments=('81:sales_amount','current'))
    elif kind=='lineage':
        status,result=request_api('_handle_metric_lineage',body,arguments=('81:sales_amount',))
    elif kind=='relationship':
        status,result=request_api('_handle_relationship_resolve',dict(body,source_entity='sales',target_entities=['inventory']))
    else:
        status,result=request_api('_handle_ast_to_sql',dict(body,asl=asl()))
        result=json.loads(result['result'])
    assert status==200,result
    assert result['business_domain_ids']==[205]
    if kind=='relationship':assert not result.get('paths')


def test_explicit_plan_rejects_more_than_one_physical_source(backend,catalog_db):
    base,_,executed=backend
    catalog_db.execute('UPDATE semantic_model_indicator SET business_domain_id=205 WHERE indicator_code=?',('inventory_amount',))
    catalog_db.execute('UPDATE semantic_model_entity_type SET business_domain_id=205 WHERE code=?',('inventory',))
    base.loader.redis.payloads['semantic_model:81:entity:inventory']['business_domain_id']='205'
    base.loader.redis.payloads['semantic_model:81:metric:inventory_amount']['business_domain_id']='205'
    body=json.loads(asl());body['metrics'].append({'name':'inventory_amount'})
    result=ScopedTranslator(grant(),base).execute_query(json.dumps(body),'81')
    assert result['code']=='DATA_SOURCE_SCOPE_MISMATCH' and not executed


@pytest.mark.parametrize('claim',[{'model_id':82},{'semantic_model_id':82},{'business_domain_ids':[]},{'business_domain_id':206}])
def test_asl_scope_claim_cannot_override_current_request(backend,claim):
    ast=json.loads(asl());ast.update(claim)
    result=ScopedTranslator(grant(),backend[0]).execute_query(json.dumps(ast),'81')
    assert not result['success'] and not backend[2]


def test_missing_planned_data_source_does_not_scan_for_substitute(backend):
    base,_,executed=backend
    del base.loader.redis.payloads['semantic_model:data_source:10']
    result=ScopedTranslator(grant(),base).execute_query(asl(),'81')
    assert not result['success'] and not executed
    assert 'semantic_model:data_source:*' not in base.loader.redis.scans


@pytest.mark.parametrize('operation',['group','filter','time','detail'])
def test_single_domain_common_query_shapes_use_registered_catalog(backend,operation):
    base,_,_=backend
    body=json.loads(asl())
    if operation=='group':body['dimensions']=[{'name':'sales_date'}]
    if operation=='filter':body['filters']=[{'field':'sales.amount','operator':'>','value':5}]
    if operation=='time':body['time_context']={'type':'year','value':2025,'anchor':'sales.created_at'}
    if operation=='detail':
        body['metrics']=[];body['dimensions']=[{'name':'sales.amount'}];body['limit']=5
    result=ScopedTranslator(grant(),base).translate_only(json.dumps(body),'81')
    assert result['success'],result
    assert 'inventory' not in result['sql'] and result['data_source_id']=='10'


@pytest.mark.parametrize('query',[{'business_domain_ids':'[205,206]'},{'business_domain_id':'205','business_domain_ids':'[]'},
    {'scope':json.dumps(grant(model=82).payload())},{'semantic_model_id':'82','business_domain_id':'205'}])
def test_definition_get_cannot_drop_or_override_scope_parameters(backend,query):
    from urllib.parse import urlencode
    _,reads,_=backend
    status,result=request_api('_handle_metric_definition',{},path='/definition?'+urlencode(query),arguments=('81:sales_amount','current'))
    assert status==400 and result['code'] in {'REQUEST_SCOPE_INVALID','EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'}
    assert reads==[]


def test_explicit_cache_refresh_does_not_mutate_global_model_cache(backend):
    base,_,_=backend
    base.loader._metric_cache[('81','keep')]={'untouched':True}
    status,result=request_api('_handle_cache_refresh',{'modelId':'81','business_domain_ids':[205]})
    assert status==200 and result['cache_mode']=='REQUEST_LOCAL'
    assert base.loader._metric_cache[('81','keep')]=={'untouched':True}
