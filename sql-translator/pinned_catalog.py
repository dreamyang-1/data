"""Internal plan-only ASL 2.0 translation from a trusted live catalog pin.

This is deliberately absent from HTTP routing. A snapshot alone is not a pin:
the caller supplies the existing publication reader, whose finish operation
checks authoritative metadata, complete index inventory and active release.
No Redis/MySQL metadata fallback or business execution is available here.
"""
from copy import deepcopy
import hashlib
import json

from semantic_scope import RequestScope, ScopedTranslator, ScopeError
from sql_translator_prod import RedisDSLLoader, SQLTranslatorProd, SemanticCatalog


class PinnedCatalogError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise PinnedCatalogError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _index(rows, key):
    result = {}
    for row in rows:
        code = row.get(key)
        _require(isinstance(code, str) and bool(code.strip()) and code not in result,
                 'PINNED_CATALOG_CODE_AMBIGUOUS')
        result[code] = deepcopy(row)
    return result


def sql_projection_aliases(sql):
    """Read aliases from this generator's SELECT projection, not WHERE text.

    A small lexical scan handles commas/FROM inside functions and quoted
    literals. Unrecognized projection syntax has no binding evidence.
    """
    if not isinstance(sql, str) or not sql.lstrip().upper().startswith('SELECT '):
        return None
    text = sql.lstrip()[7:]
    parts, start, index, depth, quote = [], 0, 0, 0, None
    while index < len(text):
        char = text[index]
        if quote:
            if char == '\\':
                index += 2
                continue
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"', '`'):
            quote = char
        elif (char == '#' or text[index:index+2] in ('--','/*')):
            return None  # Comments are not binding evidence in this generated surface.
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and char == ',':
            parts.append(text[start:index]); start = index + 1
        elif (depth == 0 and text[index:index+4].upper() == 'FROM'
              and index > 0 and text[index-1].isspace()
              and index+4 < len(text) and text[index+4].isspace()):
            parts.append(text[start:index])
            break
        index += 1
    else:
        return None
    aliases = []
    for part in parts:
        part = part.strip()
        at = part.upper().rfind(' AS ')
        if at < 0:
            return None
        alias = part[at+4:].strip()
        if len(alias) < 3 or alias[0] != '`' or alias[-1] != '`' or '`' in alias[1:-1]:
            return None
        aliases.append(alias[1:-1])
    return aliases


class _SnapshotLoader(RedisDSLLoader):
    def __init__(self, scope, snapshot):
        # Do not initialize the network-backed parent or its process caches.
        self.scope = scope
        self.physical = snapshot['physical_catalog']
        sources = self.physical.get('sql_translation_sources', {})
        _require(sources.get('contract') == 'catalog-sql-sources-v1'
                 and sources.get('scope') == snapshot['scope'], 'PINNED_SQL_SOURCES_REQUIRED')
        docs = snapshot['documents']
        for doc in docs:
            _require(doc.get('semantic_model', {}).get('id') == scope.semantic_model_id,
                     'PINNED_CATALOG_SCOPE_MISMATCH')
            domain = (doc.get('business_domain') or {}).get('id')
            _require(not scope.business_domain_ids or domain in scope.business_domain_ids,
                     'PINNED_CATALOG_SCOPE_MISMATCH')
        self.entities = _index([e for d in docs for e in d.get('entities', [])], 'entity_code')
        self.metrics = _index([m for d in docs for m in d.get('metrics', [])], 'metric_code')
        raw_dims = _index([m for d in docs for m in d.get('dimensions', [])], 'dim_code')
        entity_sources = _index(sources['entities'], 'entity_code')
        metric_sources = _index(sources['metrics'], 'metric_code')
        _require(set(entity_sources) == set(self.entities) and set(metric_sources) == set(self.metrics),
                 'PINNED_SQL_SOURCE_INVENTORY_MISMATCH')
        self.tables = {}
        self.fields = set()
        self.temporal_fields = set()
        for table in [*self.physical['tables'], *sources.get('tables', [])]:
            name = table['table_name']
            if self.tables.get(name) == table:
                continue
            _require(name not in self.tables and table['semantic_model_id'] == scope.semantic_model_id,
                     'PINNED_PHYSICAL_TABLE_AMBIGUOUS')
            self.tables[name] = table
            for field in table['fields']:
                _require(field['semantic_model_id'] == scope.semantic_model_id
                         and field['data_source_id'] == table['data_source_id']
                         and field['table_id'] == table['table_id'],
                         'PINNED_PHYSICAL_SOURCE_MISMATCH')
                path = name + '.' + field['field_name']
                _require(path not in self.fields, 'PINNED_PHYSICAL_FIELD_AMBIGUOUS')
                self.fields.add(path)
                if str(field.get('data_type', '')).upper() in ('DATE', 'DATETIME', 'TIMESTAMP'):
                    self.temporal_fields.add(path)
        self._table_to_entity, self._entity_id_to_code = {}, {}
        for code, entity in self.entities.items():
            source = entity_sources[code]
            _require(str(entity['entity_id']) == str(source['entity_id'])
                     and entity.get('business_domain') == source['business_domain_id']
                     and (not scope.business_domain_ids or source['business_domain_id'] in scope.business_domain_ids),
                     'PINNED_SQL_SOURCE_OWNER_MISMATCH')
            table = self.tables.get(source['main_table_name'])
            _require(table is not None and table['data_source_id'] == source['data_source_id'],
                     'PINNED_SQL_SOURCE_TABLE_MISMATCH')
            entity['physical_table_join'] = {'base_table': source['main_table_name']}
            entity['data_source_id'] = source['data_source_id']
            entity['sub_table_mappings'] = deepcopy(source['sub_table_mappings'])
            for attribute in entity.get('attributes', []):
                self.require_field(attribute.get('field_mapping'))
            for mapping in entity['sub_table_mappings']:
                sub = self.tables.get(mapping['sub_table_name'])
                _require(sub is not None and sub['data_source_id'] == source['data_source_id'],
                         'PINNED_SQL_SOURCE_TABLE_MISMATCH')
                self.require_field(source['main_table_name'] + '.' + mapping['main_join_column'])
                self.require_field(mapping['sub_table_name'] + '.' + mapping['sub_join_column'])
            self._table_to_entity.setdefault(source['main_table_name'], []).append(code)
            key = str(source['entity_id'])
            _require(key not in self._entity_id_to_code, 'PINNED_SQL_SOURCE_OWNER_MISMATCH')
            self._entity_id_to_code[key] = code
        self.unresolved_relationships = []
        for entity in self.entities.values():
            for relation in entity.get('relations', []):
                target = relation.get('target_entity')
                target = target if target in self.entities else self._entity_id_to_code.get(str(target))
                join = relation.get('join_key')
                # Oagnet's actual DSL preserves the two declared physical
                # endpoints as an object. Never infer an omitted endpoint.
                if (isinstance(join, dict) and set(join) == {'source_field', 'target_field'}
                        and all(isinstance(v, str) and v in self.fields for v in join.values())
                        and target in self.entities):
                    relation['join_key'] = join['source_field'] + ' = ' + join['target_field']
                    relation['target_entity'] = target
                else:
                    self.unresolved_relationships.append(relation.get('relation_code'))
                    # Keep unresolved declarations out of executable path
                    # search. An unrelated single-table query can still plan.
                    relation['join_key'] = ''
        for code, metric in list(self.metrics.items()):
            source = metric_sources[code]
            _require(source['business_domain_id'] == metric.get('business_domain'),
                     'PINNED_SQL_SOURCE_OWNER_MISMATCH')
            rule = metric['calculation_rule']
            filters = rule.get('global_filters') or []
            if isinstance(filters, str):
                filters = json.loads(filters)
            _require(isinstance(filters, list) and all(isinstance(f, str) or
                     (isinstance(f, dict) and ('condition' in f or 'filterCondition' in f)) for f in filters),
                     'PINNED_METRIC_FILTER_UNSUPPORTED')
            self.metrics[code] = self._adapt_metric(dict(code=code, name=metric.get('metric_name'),
                semantic_model_id=scope.semantic_model_id,
                # The SQL generator dispatches dependency expansion using a
                # textual marker. The governed dependency list establishes
                # that operation; do not guess meanings for numeric levels.
                metric_level='复合指标' if source['dependency_codes'] else str(metric.get('metric_level') or ''),
                calc_formula=rule.get('calc_formula'), global_filters=filters,
                depend_atom_metric=source['dependency_codes'],
                bind_entity=metric.get('source_dependency', {}).get('bind_entity', [])))
        self.dimensions = {}
        for code, dimension in raw_dims.items():
            bindings = dimension.get('bind_entities') or []
            _require(bool(bindings), 'PINNED_DIMENSION_MAPPING_REQUIRED')
            for binding in bindings:
                entity = self.entities.get(self._entity_id_to_code.get(str(binding.get('entity'))))
                _require(entity is not None, 'PINNED_DIMENSION_OWNER_MISMATCH')
                field = str(binding.get('mappingTable')) + '.' + str(binding.get('mappingColumn'))
                self.require_field(field)
                _require(any(str(a.get('attribute_id')) == str(binding.get('attr'))
                             and a.get('field_mapping') == field for a in entity.get('attributes', [])),
                         'PINNED_DIMENSION_ATTRIBUTE_MISMATCH')
            self.dimensions[code] = self._adapt_dimension({**dimension, 'entity_attribute': bindings,
                'semantic_model_id': scope.semantic_model_id}, scope.semantic_model_id)

    @property
    def redis(self):
        raise PinnedCatalogError('PINNED_EXTERNAL_CATALOG_READ_FORBIDDEN')

    def require_field(self, field):
        _require(field in self.fields, 'PINNED_PHYSICAL_FIELD_MISSING')

    def _build_table_index(self, model_id=None):
        self.scope.check_model(model_id)

    def get_entity(self, code, model_id=None):
        self.scope.check_model(model_id)
        return deepcopy(self.entities.get(code))

    def get_metric(self, code, model_id=None):
        self.scope.check_model(model_id)
        return deepcopy(self.metrics.get(code))

    def get_dimension(self, code, model_id=None):
        self.scope.check_model(model_id)
        return deepcopy(self.dimensions.get(code))

    def _iter_kind(self, kind, model_id=None):
        self.scope.check_model(model_id)
        return deepcopy(list({'entity': self.entities, 'metric': self.metrics,
                              'dimension': self.dimensions}[kind].values()))

    def iter_scoped_dimensions(self, model_id=None):
        return self.iter_dimensions(model_id)


class _SnapshotCatalog:
    def __init__(self, loader):
        self.loader = loader

    def _query(self, *args, **kwargs):
        raise PinnedCatalogError('PINNED_EXTERNAL_CATALOG_READ_FORBIDDEN')

    def registered_temporal_fields(self, model_id):
        self.loader.scope.check_model(model_id)
        return sorted(self.loader.temporal_fields)

    def attribute_metadata(self, model_id, fields):
        entities = self.loader.iter_entities(model_id)
        return {field: [deepcopy(a) for e in entities for a in e.get('attributes', [])
                        if a.get('field_mapping') == field] for field in fields}

    def resolve_metrics(self, model_id, names):
        # A display alias cannot replace an already canonical pinned metric.
        self.loader.scope.check_model(model_id)
        return {'status': 'UNRESOLVED', 'metrics': [], 'unresolved': names}

    def infer_unique_bridge_endpoint(self, model_id, bridge_table, entity_code):
        self.loader.scope.check_model(model_id)
        # No legacy recovery by matching column names across unrelated tables.
        return None


class _PinnedTranslator(SQLTranslatorProd):
    def __init__(self, scope, snapshot):
        self.scope = scope
        self.loader = _SnapshotLoader(scope, snapshot)
        self.catalog = _SnapshotCatalog(self.loader)

    def _get_entity(self, code, model_id=None):
        return self.loader.get_entity(code, model_id)

    def _get_metric(self, code, model_id=None):
        return self.loader.get_metric(code, model_id)

    def _get_dimension(self, code, model_id=None):
        return self.loader.get_dimension(code, model_id)

    _plan_sources = ScopedTranslator._plan_sources

    def required_tables(self, ast, model_id):
        """An omitted JOIN must not leave a projected/filter field unbound."""
        subject = (ast.get('subject') or {}).get('entity')
        if not subject and ast.get('metrics'):
            subject = self._get_bind_entity(ast['metrics'][0]['name'], model_id)
        base = self._get_entity_base_table(subject, model_id)
        required = {base}
        for dimension in ast.get('dimensions', []):
            name = dimension['name']
            definition = self._get_dimension(name, model_id)
            time = ast.get('time_context') or {}
            if time.get('anchor') and (name in {'dim_date', 'date', '统计日期'}
                    or definition and '时间' in str(definition.get('dim_type', ''))):
                field = time['anchor']
            else:
                field = self._get_dimension_field(name, dimension.get('attr'), model_id, subject) or name
            required.add(field.split('.', 1)[0] if '.' in field else base)
        for item in ast.get('filters', []):
            required.add(item['field'].split('.', 1)[0] if '.' in item['field'] else base)
        if ast.get('time_context'):
            required.add(ast['time_context']['anchor'].split('.', 1)[0])
        for metric in ast.get('metrics', []):
            formula = self._expanded_metric_formula(metric['name'], model_id)
            for field in SemanticCatalog._formula_fields(formula):
                self.loader.require_field(field)
                required.add(field.split('.', 1)[0])
            for rule in self._get_metric_global_filters(metric['name'], model_id):
                for field in SemanticCatalog._formula_fields(rule['condition']):
                    self.loader.require_field(field)
                    required.add(field.split('.', 1)[0])
        return required

    def _validate_ast_contract(self, ast, model_id):
        normalized = super()._validate_ast_contract(ast, model_id)
        registered = self._registered_physical_fields(normalized)
        subject = (ast.get('subject') or {}).get('entity')
        if not subject and ast.get('metrics'):
            subject = self._get_bind_entity(ast['metrics'][0]['name'], normalized)
        base = self._get_entity_base_table(subject, normalized) if subject else None
        for item in ast.get('filters', []):
            field = item.get('field')
            _require(isinstance(field, str), 'PINNED_FILTER_FIELD_REQUIRED')
            path = field if '.' in field else str(base) + '.' + field
            _require(path in registered, 'PINNED_FILTER_FIELD_UNREGISTERED')
        time = ast.get('time_context')
        if time:
            _require(time.get('anchor') in registered, 'PINNED_TIME_ANCHOR_UNREGISTERED')
        return normalized

    def _deny_execution(self, *args, **kwargs):
        raise PinnedCatalogError('PINNED_TRANSLATION_PLAN_ONLY')

    fetch_data_source = _deny_execution
    execute_query = _deny_execution
    execute_sql_only = _deny_execution
    execute_sql_on_data_source = _deny_execution


def translate_pinned_catalog(pin, request_scope, asl):
    """Accept SQL only after the trusted live pin finishes successfully.

    Scope must be rebuilt from this request's trusted upstream grant. The
    internal result preserves the translator result fields and adds receipts;
    no public request/response contract or runtime route calls this function.
    """
    try:
        _require(isinstance(request_scope, RequestScope), 'REQUEST_SCOPE_INVALID')
        scope = RequestScope.from_request(request_scope.payload())
        # Revalidate a manually constructed dataclass too (legacy model aliases
        # may coerce strings; the internal authorized model must be strict).
        _require(type(request_scope.semantic_model_id) is int and request_scope.semantic_model_id > 0,
                 'REQUEST_SCOPE_INVALID')
        identity = deepcopy(pin.identity)
        snapshot = deepcopy(pin.snapshot)
        _require(snapshot.get('contract_version') == 'catalog-release-v1'
                 and snapshot.get('catalog_version') == _digest({k: v for k, v in snapshot.items() if k != 'catalog_version'}),
                 'PINNED_CATALOG_DIGEST_MISMATCH')
        expected = {k: scope.payload()[k] for k in ('semantic_model_id', 'business_domain_ids', 'scope_mode')}
        _require(snapshot['scope'] == expected and identity.get('scope') == expected
                 and identity.get('catalog_version') == snapshot['catalog_version'], 'PINNED_CATALOG_SCOPE_MISMATCH')
        _require(all(identity.get(k) for k in ('catalog_publish_id', 'vector_index_version',
                 'activation_id', 'target_identity_hash')), 'PINNED_PUBLICATION_REQUIRED')
        ast = json.loads(asl) if isinstance(asl, str) else deepcopy(asl)
        _require(isinstance(ast, dict) and ast.get('version') == '2.0' and ast.get('intent') == 'query',
                 'PINNED_ASL_VERSION_UNSUPPORTED')
        # ASL is plan data. It cannot carry a second authorization grant.
        for key in ('model_id', 'semantic_model_id'):
            if key in ast:
                _require(type(ast[key]) is int and ast[key] == scope.semantic_model_id, 'PINNED_CATALOG_SCOPE_MISMATCH')
        _require(not any(k in ast for k in ('business_domain_id', 'business_domain_ids',
                 'authorized_semantic_scope', 'database_id', 'knowledge_base_names')), 'PINNED_ASL_SCOPE_OVERRIDE')
        translator = _PinnedTranslator(scope, snapshot)
        source = translator._plan_sources(ast, scope.semantic_model_id)
        # Existing database selection maps the platform database ID to its
        # data-source ID. Do not infer an alternative from a historical plan.
        _require(scope.database_id is None or str(scope.database_id) == source, 'DATA_SOURCE_SCOPE_MISMATCH')
        result = translator.translate_only(json.dumps(ast), scope.semantic_model_id)
        _require(result.get('success'), result.get('error_code', 'PINNED_ASL_TRANSLATION_FAILED'))
        translator.validate_read_only_sql(result['sql'])
        tables = translator._sql_involved_tables(result['sql'])
        _require(bool(tables) and tables.issubset(translator.loader.tables), 'PINNED_SQL_TABLE_SCOPE_MISMATCH')
        _require(translator.required_tables(ast, scope.semantic_model_id).issubset(tables),
                 'PINNED_SQL_REQUIRED_JOIN_MISSING')
        _require(all(str(translator.loader.tables[t]['data_source_id']) == source for t in tables)
                 and str(result.get('data_source_id')) == source, 'DATA_SOURCE_SCOPE_MISMATCH')
        receipt = pin.finish()
        _require(receipt == identity, 'PINNED_PUBLICATION_CHANGED_DURING_TRANSLATION')
        return {**result, **scope.evidence(), 'catalog_pin': deepcopy(receipt),
                'sql_projection_aliases': sql_projection_aliases(result['sql'])}
    except Exception as exc:
        # Private metadata, SQL and business text never leak through a failed
        # pin/read. Typed local errors contain bounded codes only.
        code = str(exc) if isinstance(exc, PinnedCatalogError) else getattr(exc, 'code', 'PINNED_TRANSLATION_REJECTED')
        return {'success': False, 'sql': None, 'error': code, 'error_code': code, 'retryable': False}
