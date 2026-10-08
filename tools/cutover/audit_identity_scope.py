"""Explicit read-only audit authorized for scope 81/[205].
No raw business values, credentials or service locators are printed. Writes only
aggregate/metadata receipts to the supplied private evidence directory.
The identity audit expects private_catalog_snapshot.json in that directory.
"""
from pathlib import Path
import argparse


def run(evidence_dir, service_root):
    import contextlib,io,json,logging,sys,re
    from pathlib import Path
    from datetime import datetime,timezone
    from collections import defaultdict
    root=evidence_dir
    sys.path.insert(0,str(service_root))
    logging.disable(logging.CRITICAL)
    import mysql_tool as mysql
    from catalog_release import validate_snapshot,capture_catalog,digest
    from catalog_value_sources import _definition_rows,field_identity,_port
    s=json.loads((root/'private_catalog_snapshot.json').read_text(encoding='utf-8'))
    validate_snapshot(s)
    assert s['scope']=={'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'}
    scope=s['scope']; physical=s['physical_catalog']; definitions=physical['sql_translation_sources']['entities']
    entities={e['entity_code']:e for d in s['documents'] for e in d['entities']}
    aliases={e['entity_id']:e['entity_code'] for e in definitions}
    tables={e['entity_code']:e['main_table_name'] for e in definitions}
    catalog_tables={t['table_name']:t for t in [*physical['tables'],*physical['sql_translation_sources']['tables']]}
    requested={
     'salesperson':[('id',),('salesperson_code',)],'project':[('id',),('project_code',)],
     'sales_company':[('id',),('guoyao_code',)],'product_line':[('product_line_id',),('id',)],
     'sales_order':[('id',),('order_key',),('order_key','rn'),('order_key','rn','product_code')],
     'main_data_domain_ent_manufacturer':[('manufacturer_code',),('id',)],
     'hospital':[('hospital_id',),('hospital_code',),('id',)],'department':[('dept_code',)],
     'product_category':[('category_id',),('id',)],'dealer':[('dealer_code',),('id',)],
     'city':[('city_id',)],'province':[('province_id',)],'product':[('product_code',),('id',)],
     'product_dept_relation':[('id',),('product_code','dept_code','relation_type')],
    }
    result={'observed_at':datetime.now(timezone.utc).isoformat(),'scope':scope,'catalog_version':s['catalog_version'],
     'snapshot_file_sha256':__import__('hashlib').sha256((root/'private_catalog_snapshot.json').read_bytes()).hexdigest(),
     'production_writes':0,'raw_business_values_returned':0,'relations':[],'entities':[],'sources':[]}
    def save():
     (root/'identity_profile.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    def ident(v):
     if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',v):raise ValueError('UNSAFE_IDENTIFIER')
     return '`'+v+'`'
    def resolve(owner,field):
     if owner not in tables or not isinstance(field,str):return []
     ent=next(e for e in definitions if e['entity_code']==owner)
     owned={tables[owner],*(m['sub_table_name'] for m in ent['sub_table_mappings'])}
     matches=[]
     for table in owned:
      for f in catalog_tables.get(table,{}).get('fields',[]):
       if field in (f['field_name'],table+'.'+f['field_name']):matches.append(table+'.'+f['field_name'])
     return sorted(set(matches))
    for owner,e in entities.items():
     for rel in e['relations']:
      target=aliases.get(rel['target_entity'],rel['target_entity']);join=rel.get('join_key') or {}
      src=resolve(owner,join.get('source_field'));dst=resolve(target,join.get('target_field'))
      result['relations'].append({'relation_code':rel['relation_code'],'source':owner,'target':target,
       'declared_join_key':join,'source_matches':src,'target_matches':dst,'resolved':len(src)==len(dst)==1})
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
     with mysql.consistent_catalog_read():
      routes=_definition_rows(scope,credentials=True)
      flags=mysql._query('''SELECT e.code AS entity_code,a.code AS attr_code,a.mapping_table,a.mapping_column,a.is_primary_key,a.is_unique
        FROM semantic_model_business_domain b JOIN semantic_model_entity_type e ON e.business_domain_id=b.id
        AND (e.semantic_model_id=b.semantic_model_id OR e.semantic_model_id IS NULL) AND COALESCE(e.is_deleted,0)=0 AND e.status=1
        JOIN semantic_model_attribute_config a ON a.entity_type_id=e.id AND a.semantic_model_id=b.semantic_model_id AND COALESCE(a.is_deleted,0)=0
        WHERE b.semantic_model_id=%s AND b.id=%s AND COALESCE(b.is_deleted,0)=0 ORDER BY e.code,a.code''',(81,205))
    expected={f['attribute_id']:f for f in physical['entity_value_sources']['fields']}
    assert len(routes)==len(expected)
    for row in routes:assert field_identity(row,scope)==expected[row['attribute_id']]
    result['catalog_raw_flags']=flags
    by_source=defaultdict(list)
    for e in definitions:by_source[e['data_source_id']].append(e)
    for source_id,source_entities in by_source.items():
     source=next(r for r in routes if r['data_source_id']==source_id)
     source_receipt={'data_source_id':source_id,'route_hash':field_identity(source,scope)['route']['locator_hash'],
      'started_at':datetime.now(timezone.utc).isoformat(),'transaction':'REPEATABLE READ / WITH CONSISTENT SNAPSHOT / READ ONLY'}
     result['sources'].append(source_receipt)
     connection=None
     try:
      assert source['db_type'].lower() in ('mysql','mariadb')
      connection=mysql.pymysql.connect(host=source['host'],port=_port(source['port']),user=source['username'],password=source.get('password') or '',
       database=source['db_name'],charset=mysql.MYSQL_CHARSET,connect_timeout=8,read_timeout=20,write_timeout=8,autocommit=False,cursorclass=mysql.pymysql.cursors.DictCursor)
      with connection.cursor() as cur:
       cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
       cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
       cur.execute('SELECT @@server_uuid AS server_uuid,DATABASE() AS database_name,@@session.time_zone AS session_time_zone,@@system_time_zone AS system_time_zone')
       observed=cur.fetchone();source_receipt['server_identity_hash']=digest({k:observed[k] for k in ('server_uuid','database_name')})
       source_receipt['runtime_timezones']={k:observed[k] for k in ('session_time_zone','system_time_zone')}
       for e in source_entities:
        code=e['entity_code'];table=e['main_table_name'];cat=entities[code]
        row={'entity_code':code,'base_table':table,'data_source_id':source_id,'entity_primary_key':cat.get('primary_key'),
         'catalog_attributes':[{k:a.get(k) for k in ('attr_code','attr_name','is_primary_key','is_unique','field_mapping')} for a in cat['attributes']],
         'candidates':[],'status':'INSPECTING'}
        result['entities'].append(row)
        cur.execute('SELECT COLUMN_NAME AS column_name,DATA_TYPE AS data_type,IS_NULLABLE AS is_nullable,COLUMN_KEY AS column_key,COLLATION_NAME AS collation_name FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION',(table,))
        columns=cur.fetchall();row['physical_columns']=columns;names={c['column_name'] for c in columns}
        cur.execute('SELECT INDEX_NAME AS index_name,NON_UNIQUE AS non_unique,SEQ_IN_INDEX AS seq_in_index,COLUMN_NAME AS column_name,SUB_PART AS sub_part FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX',(table,))
        row['physical_indexes']=cur.fetchall()
        cur.execute('SELECT ENGINE AS engine,TABLE_TYPE AS table_type FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(table,))
        row['physical_table']=cur.fetchone();assert row['physical_table'] and row['physical_table']['engine']=='InnoDB'
        candidates=list(requested[code]);indexes=defaultdict(list)
        for idx in row['physical_indexes']:
         if idx['non_unique']==0 and idx['sub_part'] is None:indexes[idx['index_name']].append(idx['column_name'])
        for fields in indexes.values():
         if fields and tuple(fields) not in candidates:candidates.append(tuple(fields))
        for fields in candidates:
         candidate={'identity_candidate':list(fields),'exists':set(fields)<=names,
          'relation_join_usage':[r['relation_code'] for r in result['relations'] if r['resolved'] and any(table+'.'+f in (r['source_matches'] if r['source']==code else [])+(r['target_matches'] if r['target']==code else []) for f in fields)],
          'physical_schema_evidence':[name for name,cols in indexes.items() if tuple(cols)==fields],
          'catalog_evidence':[a for a in flags if a['entity_code']==code and a['mapping_table']==table and a['mapping_column'] in fields]}
         row['candidates'].append(candidate)
         if not candidate['exists']:candidate['status']='FIELD_ABSENT';continue
         cols=','.join(map(ident,fields));nonnull=' AND '.join(ident(f)+' IS NOT NULL' for f in fields)
         blank=' OR '.join('TRIM(CAST('+ident(f)+' AS CHAR))=\'\'' for f in fields)
         cur.execute(f'SELECT /*+ MAX_EXECUTION_TIME(15000) */ COUNT(*) AS total_count,SUM(CASE WHEN {nonnull} THEN 1 ELSE 0 END) AS non_null_count,COUNT(DISTINCT {cols}) AS distinct_count,SUM(CASE WHEN {blank} THEN 1 ELSE 0 END) AS blank_count FROM {ident(table)}')
         stats={k:int(v or 0) for k,v in cur.fetchone().items()};candidate.update(stats)
         cur.execute(f'SELECT /*+ MAX_EXECUTION_TIME(15000) */ COUNT(*) AS duplicate_groups,COALESCE(SUM(n-1),0) AS duplicate_count FROM (SELECT COUNT(*) AS n FROM {ident(table)} WHERE {nonnull} GROUP BY {cols} HAVING COUNT(*)>1) AS identity_duplicates')
         candidate.update({k:int(v) for k,v in cur.fetchone().items()})
         candidate['non_null_rate']=stats['non_null_count']/stats['total_count'] if stats['total_count'] else None
         candidate['uniqueness_verified']=bool(stats['total_count']>0 and stats['total_count']==stats['non_null_count']==stats['distinct_count'] and not stats['blank_count'] and candidate['duplicate_count']==0)
         candidate['status']='SNAPSHOT_UNIQUE' if candidate['uniqueness_verified'] else 'EMPTY_SNAPSHOT' if not stats['total_count'] else 'NOT_UNIQUE_OR_NULL'
         candidate['same_name_different_identity_groups']={}
         if candidate['uniqueness_verified']:
          for name in [a['mapping_column'] for a in flags if a['entity_code']==code and a['mapping_table']==table and a['mapping_column'].endswith('_name') and a['mapping_column'] in names]:
           cur.execute(f'SELECT /*+ MAX_EXECUTION_TIME(15000) */ COUNT(*) AS collision_groups FROM (SELECT 1 FROM {ident(table)} WHERE {ident(name)} IS NOT NULL GROUP BY {ident(name)} HAVING COUNT(DISTINCT {cols})>1) AS same_name_ids')
           candidate['same_name_different_identity_groups'][name]=int(cur.fetchone()['collision_groups'])
         save()
        row['status']='AGGREGATE_READ_COMPLETE';save()
        print(json.dumps({'entity':code,'candidates':[{k:c.get(k) for k in ('identity_candidate','status','total_count','distinct_count','duplicate_count')} for c in row['candidates']]}),flush=True)
       source_receipt['status']='READ_ONLY_SNAPSHOT_COMPLETE'
     except Exception as exc:
      source_receipt.update(status='READ_FAILED',error_type=type(exc).__name__,error_number=exc.args[0] if exc.args and type(exc.args[0]) is int else None)
     finally:
      if connection:
       try:connection.rollback()
       finally:connection.close()
      source_receipt['finished_at']=datetime.now(timezone.utc).isoformat();save()
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
     current=capture_catalog(81,[205])
    result['catalog_unchanged_after_read']=current['catalog_version']==s['catalog_version']
    result['finished_at']=datetime.now(timezone.utc).isoformat();save()
    print(json.dumps({'sources':result['sources'],'entity_count':len(result['entities']),'catalog_unchanged':result['catalog_unchanged_after_read']}))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--service-root',type=Path,required=True)
    args=parser.parse_args()
    if not args.evidence_dir.is_dir():
        parser.error('Private evidence directory must already exist')
    run(args.evidence_dir,args.service_root)
