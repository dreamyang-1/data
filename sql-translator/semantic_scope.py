"""Request-local single-domain planning over the existing governed catalog.

There is no role/permission lookup here. Scope is supplied by the trusted caller.
The model-wide translator remains unchanged; explicit requests never share its
adapted-object caches or mutable relationship indexes.
"""
from dataclasses import dataclass
import copy
import hashlib
import json
import re

from sql_translator_prod import SemanticCatalog, RedisDSLLoader, SQLTranslatorProd, _positive_int

CONTRACT_VERSION = 'single-domain-v1'


class ScopeError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RequestScope:
    semantic_model_id: int
    business_domain_ids: tuple = ()
    database_id: int | None = None
    knowledge_base_names: tuple = ()

    @classmethod
    def from_request(cls, body):
        def domains(value):
            items = value.get('business_domain_ids', [])
            if not isinstance(items, list) or any(type(x) is not int or x <= 0 for x in items):
                raise ValueError('Invalid business_domain_ids')
            items = tuple(sorted(set(items)))
            legacy = value.get('business_domain_id')
            if legacy is not None:
                if type(legacy) is not int or legacy <= 0:
                    raise ValueError('Invalid business_domain_id')
                if 'business_domain_ids' in value and items != (legacy,):
                    raise ValueError('Business domain aliases disagree')
                items = (legacy,)
            return items

        try:
            if not isinstance(body, dict):
                raise ValueError('Request must be an object')
            grant = body.get('authorized_semantic_scope')
            if grant is not None and not isinstance(grant, dict):
                raise ValueError('Invalid authorized scope')
            models = [_positive_int(body[k]) for k in ('semantic_model_id', 'modelId', 'model_id') if k in body]
            if grant is not None:
                model = grant.get('semantic_model_id')
                if type(model) is not int or model <= 0:
                    raise ValueError('Authorized model must be a strict positive integer')
                models.append(model)
            if not models or len(set(models)) != 1:
                raise ValueError('Missing or conflicting request model')
            requested = domains(body)
            if grant is not None:
                granted = domains(grant)
                if any(k in body for k in ('business_domain_id', 'business_domain_ids')) and requested != granted:
                    raise ValueError('Request and authorized domains disagree')
                requested = granted
                mode = 'EXPLICIT_DOMAINS' if granted else 'MODEL_WIDE'
                if grant.get('scope_mode') != mode:
                    raise ValueError('Scope mode disagrees with domains')
            if len(requested) > 1:
                raise ScopeError('EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED', 'Multiple explicit domains are unsupported')
            source = grant or body
            database = source.get('database_id')
            if database is not None and (type(database) is not int or database <= 0):
                raise ValueError('Invalid database_id')
            knowledge = source.get('knowledge_base_names', [])
            if not isinstance(knowledge, list) or any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 128 for x in knowledge):
                raise ValueError('Invalid knowledge base names')
            return cls(models[0], requested, database, tuple(sorted({x.strip() for x in knowledge})))
        except ScopeError:
            raise
        except (TypeError, ValueError):
            raise ScopeError('REQUEST_SCOPE_INVALID', 'Semantic scope is invalid or inconsistent') from None

    def payload(self):
        return dict(semantic_model_id=self.semantic_model_id, business_domain_ids=list(self.business_domain_ids),
                    scope_mode='EXPLICIT_DOMAINS' if self.business_domain_ids else 'MODEL_WIDE',
                    database_id=self.database_id, knowledge_base_names=list(self.knowledge_base_names),
                    source='TRUSTED_UPSTREAM_BACKEND')

    def evidence(self):
        fingerprint = hashlib.sha256(json.dumps(self.payload(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return dict(scope_contract_version=CONTRACT_VERSION, authorized_scope_fingerprint=fingerprint,
                    semantic_model_id=self.semantic_model_id, business_domain_ids=list(self.business_domain_ids))

    def check_model(self, model):
        if str(model) != str(self.semantic_model_id):
            raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Model differs from the current request')


class ScopedCatalog(SemanticCatalog):
    # This pattern operates on the finite, internal SELECT templates in
    # SemanticCatalog, never on user SQL or natural language. Every referenced
    # table becomes a filtered derived table before the database reads it.
    _catalog_table = re.compile(r'\b(FROM|JOIN)\s+(semantic_model_[a-z_]+)\b', re.I)
    _clause_words = {'WHERE', 'ON', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'JOIN', 'ORDER', 'GROUP', 'LIMIT', 'UNION'}

    def __init__(self, scope, db_config=None):
        super().__init__(db_config)
        self.scope = scope

    def _restricted_table(self, table):
        model = self.scope.semantic_model_id
        domain, = self.scope.business_domain_ids
        base = f's.semantic_model_id={model} AND s.is_deleted=0'
        entity = (f'e.semantic_model_id={model} AND e.business_domain_id={domain} '
                  'AND e.is_deleted=0 AND e.status=1')
        if table in {'semantic_model_indicator', 'semantic_model_entity_type'}:
            predicate = f'{base} AND s.business_domain_id={domain}'
            if table == 'semantic_model_entity_type':
                predicate += ' AND s.status=1'
        elif table == 'semantic_model_dimension':
            # Dimensions have no business_domain_id. The published entity
            # bindings establish membership; there is no implicit shared grant.
            predicate = (f'{base} AND EXISTS (SELECT 1 FROM semantic_model_entity_type e WHERE {entity} '
                         "AND JSON_CONTAINS(JSON_EXTRACT(s.entity_attribute, '$[*].entity'), JSON_QUOTE(CAST(e.id AS CHAR))))")
        elif table == 'semantic_model_relation_config':
            predicate = f'{base} AND s.business_domain_id={domain} AND s.status=1'
            for endpoint in ('source_entity_type_id', 'target_entity_type_id'):
                predicate += f' AND EXISTS (SELECT 1 FROM semantic_model_entity_type e WHERE {entity} AND e.id=s.{endpoint})'
        elif table in {'semantic_model_attribute_config', 'semantic_model_entity_sub_table_mapping', 'semantic_model_entity_bind_indicator'}:
            owner = 'e.code=s.entity_code' if table.endswith('bind_indicator') else 'e.id=s.entity_type_id'
            predicate = f'{base} AND EXISTS (SELECT 1 FROM semantic_model_entity_type e WHERE {entity} AND {owner})'
            if table.endswith('bind_indicator'):
                predicate += (f' AND EXISTS (SELECT 1 FROM semantic_model_indicator i WHERE i.semantic_model_id={model}'
                              f' AND i.business_domain_id={domain} AND i.is_deleted=0 AND i.indicator_code=s.indicator_code)')
        elif table in {'semantic_model_table', 'semantic_model_field'}:
            physical = 's' if table.endswith('_table') else 't'
            owned = (f'EXISTS (SELECT 1 FROM semantic_model_entity_type e WHERE {entity} '
                     f'AND e.data_source_id={physical}.data_source_id AND '
                     f'(e.main_table_name={physical}.name OR EXISTS ('
                     f'SELECT 1 FROM semantic_model_entity_sub_table_mapping m WHERE m.semantic_model_id={model} '
                     f'AND m.entity_type_id=e.id AND m.is_deleted=0 AND m.sub_table_name={physical}.name)))')
            if table.endswith('_field'):
                owned = (f'EXISTS (SELECT 1 FROM semantic_model_table t WHERE t.semantic_model_id={model} '
                         f'AND t.id=s.table_id AND t.is_deleted=0 AND {owned})')
            predicate = f'{base} AND {owned}'
        else:
            raise ScopeError('SCOPED_CATALOG_TABLE_UNSUPPORTED', 'Unreviewed semantic table cannot be read in an explicit scope')
        return f'(SELECT s.* FROM {table} s WHERE {predicate})'

    def scoped_query(self, sql):
        def replace(match):
            table = match[2].lower()
            suffix = sql[match.end():].lstrip()
            token = suffix.split(None, 1)[0].upper() if suffix else ''
            has_alias = bool(token and token not in self._clause_words and token[0].isalpha())
            alias = '' if has_alias else ' ' + table
            return match[1] + ' ' + self._restricted_table(table) + alias
        if not sql.lstrip().upper().startswith('SELECT ') or not self._catalog_table.search(sql):
            raise ScopeError('SCOPED_CATALOG_QUERY_UNSUPPORTED', 'Only reviewed semantic catalog reads are supported')
        return self._catalog_table.sub(replace, sql)

    def _query(self, sql, params=()):
        return super()._query(self.scoped_query(sql), params)

    def definition(self, metric_id, version, model_id=None):
        self.scope.check_model(metric_id.split(':', 1)[0])
        result = super().definition(metric_id, version, model_id)
        allowed = {r['name'] for r in self._query(
            'SELECT name FROM semantic_model_table WHERE semantic_model_id=%s', (self.scope.semantic_model_id,))}
        tables = {field.split('.', 1)[0] for field in self._formula_fields(result.get('calculation_formula') or '')}
        if not tables.issubset(allowed):
            raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Metric formula references a table outside the current domain')
        for code in result.get('depend_metrics', []):
            self._metric_row(self.scope.semantic_model_id, code)
        return result

    def dimension_metadata(self, model_id, dim_code):
        self.scope.check_model(model_id)
        row = super().dimension_metadata(model_id, dim_code)
        if row is None:
            return None
        owners = self.entity_relationship_metadata(model_id)
        ids = {str(e['entity_id']) for e in owners.values()}
        row['entity_attribute'] = [b for b in row.get('entity_attribute', [])
            if isinstance(b, dict) and str(b.get('entity')) in ids]
        # Names and common formatting belong to the model-wide dimension;
        # physical projections are limited to this request's owned bindings.
        return row if row['entity_attribute'] else None

    def entity_relationship_metadata(self, model_id):
        self.scope.check_model(model_id)
        graph = super().entity_relationship_metadata(model_id)
        rows = self._query('SELECT code,data_source_id FROM semantic_model_entity_type WHERE semantic_model_id=%s', (int(model_id),))
        for row in rows:
            if row['code'] in graph:
                graph[row['code']]['data_source_id'] = row['data_source_id']
        return graph


class ScopedLoader(RedisDSLLoader):
    def __init__(self, scope, base, catalog=None):
        super().__init__(base.redis_config)
        self.scope = scope
        self._redis = base.redis
        self.catalog = catalog or ScopedCatalog(scope)

    def _payload_matches_model(self, data, model_id):
        self.scope.check_model(model_id)
        return (super()._payload_matches_model(data, model_id)
                and str(data.get('business_domain_id')) == str(self.scope.business_domain_ids[0])
                and data.get('is_deleted') not in (1, True, '1'))

    def _get_redis_key(self, type_prefix, code, model_id=None):
        self.scope.check_model(model_id)
        return super()._get_redis_key(type_prefix, code, model_id)

    def _iter_kind(self, kind, model_id=None):
        self.scope.check_model(model_id)
        yield from super()._iter_kind(kind, model_id)

    def iter_scoped_dimensions(self, model_id):
        return self.iter_dimensions(model_id)

    def get_dimension(self, code, model_id=None):
        self.scope.check_model(model_id)
        key = (str(model_id), code)
        cached = self._dimension_cache.get(key)
        if cached is not None:
            return cached
        row = self.catalog.dimension_metadata(model_id, code)
        if row is None:
            return None
        value = self._adapt_dimension(row, model_id)
        self._dimension_cache[key] = value
        return value

    def iter_dimensions(self, model_id=None):
        self.scope.check_model(model_id)
        rows = self.catalog._query('SELECT dim_code FROM semantic_model_dimension WHERE semantic_model_id=%s', (int(model_id),))
        return [value for r in rows if (value := self.get_dimension(r['dim_code'], model_id)) is not None]

    def _build_table_index(self, model_id=None):
        self.scope.check_model(model_id)
        if self._index_built_model_id == model_id:
            return
        self._table_to_entity.clear()
        self._entity_id_to_code.clear()
        for key in self.redis.scan_iter(match=f'semantic_model:{model_id}:entity:*', count=1000):
            raw = self.redis.get(key)
            try:
                data = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                continue
            if not self._payload_matches_model(data, model_id):
                continue
            code, identifier = data.get('code'), data.get('id')
            table = data.get('main_table_name') or data.get('mapping_table')
            if code and table:
                self._table_to_entity.setdefault(table, []).append(code)
            if code and identifier:
                self._entity_id_to_code[str(identifier)] = code
        self._index_built_model_id = model_id

    def _adapt_entity(self, data, model_id=None):
        data = copy.deepcopy(data)
        # Relation objects carry their own scope and cannot borrow their
        # parent's grant. Unknown/foreign endpoints cannot enter a join graph.
        data['semantic_model_relation_config'] = [r for r in data.get('semantic_model_relation_config', []) or []
            if str(r.get('business_domain_id')) == str(self.scope.business_domain_ids[0])
            and self._find_entity_code_by_id(r.get('target_entity_type_id'), model_id)]
        return super()._adapt_entity(data, model_id)


class ScopedTranslator(SQLTranslatorProd):
    def __init__(self, scope, base):
        self.scope = scope
        self.catalog = ScopedCatalog(scope, base.catalog.db_config)
        self.loader = ScopedLoader(scope, base.loader, self.catalog)
        self._metric_definitions = {}

    def _get_entity(self, code, model_id=None):
        value = self.loader.get_entity(code, model_id)
        if not value:
            return None
        current = self.catalog.entity_relationship_metadata(model_id).get(code)
        if not current:
            return None
        value = copy.deepcopy(value)
        value['physical_table_join']['base_table'] = current['base_table']
        value['relations'] = current['relations']
        value['sub_table_mappings'] = current['sub_table_mappings']
        value['data_source_id'] = current['data_source_id']
        return value

    def _get_dimension(self, code, model_id=None):
        return self.loader.get_dimension(code, model_id)

    def _get_metric(self, code, model_id=None):
        value = super()._get_metric(code, model_id)
        if value:
            if code not in self._metric_definitions:
                self._metric_definitions[code] = self.catalog.definition(f'{model_id}:{code}', 'current')
            definition = self._metric_definitions[code]
            value = copy.deepcopy(value)
            value['calculation_rule']['calc_formula'] = definition['calculation_formula']
            value['calculation_rule']['depend_metrics'] = definition['depend_metrics']
            value['calculation_rule']['global_filters'] = self.loader._adapt_metric(
                {'global_filters': definition['global_filters']}, model_id)['calculation_rule']['global_filters']
            value['source_dependency']['bind_entity'] = [e['resolved_entity_code'] for e in definition['bound_entities']]
            for entity in value.get('source_dependency', {}).get('bind_entity', []):
                if not self._get_entity(entity, model_id):
                    raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Metric binds an unavailable or out-of-scope entity')
        return value

    def _plan_sources(self, ast, model_id):
        sources, seen, active = set(), set(), set()
        def entity_source(code):
            entity = self._get_entity(code, model_id)
            if not entity or entity.get('data_source_id') is None:
                raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Plan entity is unavailable in the current domain')
            sources.add(str(entity['data_source_id']))
        def visit(code):
            if code in active:
                raise ScopeError('SCOPED_METRIC_DEPENDENCY_INVALID', 'Cyclic metric dependency')
            if code in seen:
                return
            metric = self._get_metric(code, model_id)
            if not metric:
                raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Plan metric is unavailable in the current domain')
            active.add(code)
            for entity in metric['source_dependency']['bind_entity']:
                entity_source(entity)
            for dependency in metric['calculation_rule']['depend_metrics']:
                visit(dependency)
            active.remove(code)
            seen.add(code)
        for metric in ast.get('metrics', []):
            visit(metric.get('name'))
        if (ast.get('subject') or {}).get('entity'):
            entity_source(ast['subject']['entity'])
        if ast.get('data_source_id') is not None:
            sources.add(str(ast['data_source_id']))
        if len(sources) != 1:
            raise ScopeError('DATA_SOURCE_SCOPE_MISMATCH', 'Scoped plan must reference exactly one data source')
        return next(iter(sources))

    def translate_only(self, asl_str, model_id=None):
        self.scope.check_model(model_id)
        try:
            ast = json.loads(asl_str)
            if not isinstance(ast, dict):
                raise ValueError('ASL must be an object')
            for key in ('model_id', 'semantic_model_id'):
                if key in ast:
                    self.scope.check_model(ast[key])
            if any(k in ast for k in ('business_domain_id', 'business_domain_ids', 'authorized_semantic_scope')):
                claimed = RequestScope.from_request(dict(modelId=model_id, **ast))
                if claimed.business_domain_ids != self.scope.business_domain_ids:
                    raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'ASL cannot change the request domain')
            planned_source = self._plan_sources(ast, model_id)
            result = super().translate_only(asl_str, model_id)
            if result.get('success'):
                self.validate_read_only_sql(result['sql'])
                allowed = {row['name'] for row in self.catalog._query(
                    'SELECT name FROM semantic_model_table WHERE semantic_model_id=%s', (self.scope.semantic_model_id,))}
                tables = self._sql_involved_tables(result['sql'])
                if not tables or not tables.issubset(allowed):
                    raise ScopeError('SEMANTIC_SCOPE_MISMATCH', 'Generated SQL references a table outside the domain')
                if not result.get('data_source_id'):
                    raise ScopeError('DATA_SOURCE_SCOPE_MISMATCH', 'A scoped plan must resolve one data source')
                if str(result['data_source_id']) != planned_source:
                    raise ScopeError('DATA_SOURCE_SCOPE_MISMATCH', 'Translator changed the semantic plan source')
            result.update(self.scope.evidence())
            return result
        except (ValueError, TypeError) as exc:
            return self.failure(getattr(exc, 'code', 'INVALID_ASL'), str(exc))

    def failure(self, code, message):
        return dict(success=False, sql=None, error_code=code, code=code, error=message, retryable=False, **self.scope.evidence())

    def fetch_data_source(self, model_id, data_source_id=None):
        self.scope.check_model(model_id)
        if not data_source_id:
            raise ScopeError('DATA_SOURCE_SCOPE_MISMATCH', 'Scoped execution requires the planned data source')
        raw = self.loader.redis.get(f'semantic_model:data_source:{data_source_id}')
        data = json.loads(raw) if raw else None
        if (not isinstance(data, dict) or str(data.get('semantic_model_id')) != str(model_id)
                or str(data.get('id')) != str(data_source_id) or data.get('is_deleted') in (1, True, '1')):
            raise ScopeError('DATA_SOURCE_SCOPE_MISMATCH', 'Planned data source is missing or belongs to another model')
        return data

    def execute_scoped(self, asl, sql, model_id, data_source_id=None):
        # Revalidate at execution time, including publication changes since
        # translation. No persistent receipt cache or mutable global grant.
        if not isinstance(asl, (str, dict)):
            return self.failure('SCOPED_EXECUTION_ASL_REQUIRED', 'Explicit execution requires its semantic plan')
        plan = self.translate_only(asl if isinstance(asl, str) else json.dumps(asl), model_id)
        if not plan.get('success'):
            return plan
        if sql != plan['sql']:
            return self.failure('SCOPED_SQL_PLAN_MISMATCH', 'SQL differs from the current scoped semantic plan')
        if data_source_id is not None and str(data_source_id) != str(plan['data_source_id']):
            return self.failure('DATA_SOURCE_SCOPE_MISMATCH', 'Execution source differs from the scoped plan')
        result = super().execute_sql_only(sql, model_id, plan['data_source_id'])
        result.update(self.scope.evidence())
        return result

    def execute_sql_only(self, sql, model_id=None, data_source_id=None):
        return self.failure('SCOPED_EXECUTION_ASL_REQUIRED', 'Use execute_scoped with the semantic plan')

    def execute_query(self, asl, model_id=None):
        plan = self.translate_only(asl, model_id)
        if not plan.get('success'):
            return plan
        return self.execute_scoped(asl, plan['sql'], model_id, plan['data_source_id'])
