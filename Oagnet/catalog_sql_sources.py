"""Private authoritative SQL metadata missing from the public semantic DSL.

Captured inside the existing catalog transaction. No Redis cache, business data
or credentials participate; no SQL or join key is invented here.
"""
from catalog_release import CatalogEvidenceError, catalog_scope
from dimension_scope import normalize_governed_id

CONTRACT = 'catalog-sql-sources-v1'


def _id(value):
    identifier = normalize_governed_id(value)
    if identifier is None:
        raise CatalogEvidenceError('CATALOG_SQL_SOURCE_ID_INVALID')
    return identifier


def _positive(value):
    if type(value) is not int or value <= 0:
        raise CatalogEvidenceError('CATALOG_SQL_SOURCE_ID_INVALID')
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise CatalogEvidenceError('CATALOG_SQL_SOURCE_METADATA_INVALID')
    return value


def capture_sql_sources(scope):
    import mysql_tool as mysql
    if scope != catalog_scope(scope.get('semantic_model_id'), scope.get('business_domain_ids')):
        raise CatalogEvidenceError('REQUEST_SCOPE_INVALID')
    domain = ' AND b.id=%s' if scope['business_domain_ids'] else ''
    params = (scope['semantic_model_id'], *scope['business_domain_ids'])
    owner = '''FROM semantic_model_business_domain b
        JOIN semantic_model_entity_type e ON e.business_domain_id=b.id
            AND (e.semantic_model_id=b.semantic_model_id OR e.semantic_model_id IS NULL)
            AND COALESCE(e.is_deleted,0)=0 AND e.status=1
        WHERE b.semantic_model_id=%s AND COALESCE(b.is_deleted,0)=0'''
    rows = mysql._query('''SELECT e.id AS entity_id,e.code AS entity_code,
        e.business_domain_id,e.data_source_id,e.main_table_name ''' + owner + domain + ' ORDER BY b.id,e.id', params)
    entities = {}
    for row in rows:
        identifier = _id(row.get('entity_id'))
        table = _text(row.get('main_table_name'))
        code = row.get('entity_code') or table
        if not mysql._SAFE_IDENTIFIER.fullmatch(table) or not isinstance(code, str) or not code.strip():
            raise CatalogEvidenceError('CATALOG_SQL_SOURCE_METADATA_INVALID')
        value = dict(entity_id=identifier,entity_code=code,business_domain_id=_positive(row.get('business_domain_id')),
            data_source_id=_positive(row.get('data_source_id')),main_table_name=table,sub_table_mappings=[])
        if identifier in entities:
            raise CatalogEvidenceError('CATALOG_SQL_SOURCE_ENTITY_AMBIGUOUS')
        entities[identifier] = value
    rows = mysql._query('''SELECT s.entity_type_id,s.sub_table_name,s.main_join_column,s.sub_join_column
        FROM semantic_model_business_domain b
        JOIN semantic_model_entity_type e ON e.business_domain_id=b.id
            AND (e.semantic_model_id=b.semantic_model_id OR e.semantic_model_id IS NULL)
            AND COALESCE(e.is_deleted,0)=0 AND e.status=1
        JOIN semantic_model_entity_sub_table_mapping s ON s.entity_type_id=e.id
            AND s.semantic_model_id=b.semantic_model_id AND COALESCE(s.is_deleted,0)=0
        WHERE b.semantic_model_id=%s AND COALESCE(b.is_deleted,0)=0''' + domain +
        ' ORDER BY b.id,e.id,s.sub_table_name,s.main_join_column,s.sub_join_column', params)
    for row in rows:
        identifier = _id(row.get('entity_type_id'))
        if identifier not in entities:
            raise CatalogEvidenceError('CATALOG_SQL_SOURCE_OWNER_MISSING')
        value = {k:_text(row.get(k)) for k in ('sub_table_name','main_join_column','sub_join_column')}
        if value in entities[identifier]['sub_table_mappings']:
            raise CatalogEvidenceError('CATALOG_SQL_SOURCE_SUBTABLE_AMBIGUOUS')
        entities[identifier]['sub_table_mappings'].append(value)
    rows = mysql._query('''SELECT indicator_code,business_domain_id,dependence_atomic_indicator
        FROM semantic_model_indicator WHERE semantic_model_id=%s AND COALESCE(is_deleted,0)=0''' +
        (' AND business_domain_id=%s' if scope['business_domain_ids'] else '') +
        ' ORDER BY business_domain_id,indicator_code', params)
    metrics = []; seen = set()
    for row in rows:
        code = _text(row.get('indicator_code')); key = (row.get('business_domain_id'), code)
        if key in seen:
            raise CatalogEvidenceError('CATALOG_SQL_SOURCE_METRIC_AMBIGUOUS')
        seen.add(key)
        raw = row.get('dependence_atomic_indicator')
        if raw in (None, ''):
            dependencies = []
        else:
            raw = mysql._parse_json(raw)
            dependencies = [v.strip() for v in raw.split(',') if v.strip()] if isinstance(raw, str) else raw
            if not isinstance(dependencies, list) or any(not isinstance(v, str) or not v.strip() for v in dependencies):
                raise CatalogEvidenceError('CATALOG_SQL_SOURCE_DEPENDENCY_INVALID')
        metrics.append(dict(metric_code=code,business_domain_id=row.get('business_domain_id'),dependency_codes=dependencies))
    # The legacy physical endpoint projects entity base tables only. Capture
    # explicitly governed sub-table registrations privately, using the same
    # model, domain and data source ownership as their declaring entity.
    subtable_owner = '''FROM semantic_model_business_domain b
        JOIN semantic_model_entity_type e ON e.business_domain_id=b.id
            AND (e.semantic_model_id=b.semantic_model_id OR e.semantic_model_id IS NULL)
            AND COALESCE(e.is_deleted,0)=0 AND e.status=1
        JOIN semantic_model_entity_sub_table_mapping s ON s.entity_type_id=e.id
            AND s.semantic_model_id=b.semantic_model_id AND COALESCE(s.is_deleted,0)=0
        JOIN semantic_model_data_source ds ON ds.id=e.data_source_id
            AND ds.semantic_model_id=b.semantic_model_id AND COALESCE(ds.is_deleted,0)=0 AND ds.status=1
        JOIN semantic_model_table t ON t.name=s.sub_table_name AND t.data_source_id=ds.id
            AND t.semantic_model_id=b.semantic_model_id AND COALESCE(t.is_deleted,0)=0
        '''
    where = ' WHERE b.semantic_model_id=%s AND COALESCE(b.is_deleted,0)=0' + domain
    table_rows = mysql._query('SELECT DISTINCT t.* ' + subtable_owner + where + ' ORDER BY t.id', params)
    field_rows = mysql._query('SELECT DISTINCT f.* ' + subtable_owner + '''
        JOIN semantic_model_field f ON f.table_id=t.id AND f.semantic_model_id=t.semantic_model_id
            AND f.data_source_id=t.data_source_id AND COALESCE(f.is_deleted,0)=0
        ''' + where + ' ORDER BY f.table_id,f.id', params)
    tables = []
    for row in table_rows:
        table = mysql._row_to_table_dict(row)
        table['fields'] = [mysql._row_to_field_dict(f) for f in field_rows if f['table_id'] == row['id']]
        tables.append(table)
    return dict(contract=CONTRACT,scope=dict(scope),entities=list(entities.values()),metrics=metrics,tables=tables)
