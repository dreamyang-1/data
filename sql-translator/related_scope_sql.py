"""Compile shared-attribute scopes as semi-joins, never multiplying fact rows."""
import re


def is_compiled_shared_scope_sql(sql):
    """Recognize only the compiler's materializable semi-join envelope.

    Input is already comment/literal-scrubbed by the read-only guard. All other
    subqueries remain forbidden; scoped execution still re-translates the ASL
    and checks every referenced physical table against the current grant.
    """
    identifier = r'[A-Za-z_]\w*'
    prefix = re.compile(
        rf'{identifier}\.{identifier}\s+IN\s*\(SELECT\s+_related_(?P<n>[0-3])\.(?P<key>{identifier})\s+'
        rf'FROM\s+(?P<table>{identifier})\s+_related_(?P=n)\s+'
        rf'JOIN\s+(?P=table)\s+_target_(?P=n)\s+'
        rf'ON\s+_target_(?P=n)\.(?P<shared>{identifier})\s*=\s*_related_(?P=n)\.(?P=shared)\s+'
        rf'WHERE\s+_target_(?P=n)\.(?P=key)\s+IN\s*\(', re.I | re.ASCII)
    remaining = sql
    count = 0
    while (match := prefix.search(remaining)) is not None:
        depth, end = 1, match.end()
        start = end
        while end < len(remaining) and depth:
            depth += (remaining[end] == '(') - (remaining[end] == ')')
            end += 1
        if depth:
            return False
        target = remaining[start:end-1].strip()
        target_match = re.match(rf'SELECT\s+({identifier})\.{identifier}\s+FROM\s+\1\b', target, re.I | re.ASCII)
        if (not target_match or re.search(r'\b(?:SELECT|UNION|WITH)\b', target[target_match.end():], re.I)
                or not re.search(r'\bWHERE\b', target, re.I)):
            return False
        suffix = re.match(r'\s*\)', remaining[end:])
        if not suffix:
            return False
        remaining = remaining[:match.start()] + '1=1' + remaining[end+suffix.end():]
        count += 1
        if count > 4:
            return False
    return count > 0 and not re.search(r'\(\s*SELECT\b', remaining, re.I)


def compile_related_filters(translator, ast, model_id):
    items = ast.get('related_filters') or []
    if not isinstance(items, list) or len(items) > 4:
        raise ValueError('related_filters must be an array with at most four items')
    conditions = []
    for number, item in enumerate(items):
        if not isinstance(item, dict) or item.get('kind') != 'shared_attribute':
            raise ValueError('unsupported related filter kind')
        names = ['outer_field', 'target_field', 'bridge_key', 'shared_field']
        for name in names:
            if not re.fullmatch(r'[A-Za-z_]\w*\.[A-Za-z_]\w*', str(item.get(name) or ''), re.ASCII):
                raise ValueError('related filter requires a registered table.column')
        outer, target, bridge, shared = [item[name] for name in names]
        entities = {}
        for key in ['outer_entity', 'target_entity', 'bridge_entity']:
            code = item.get(key)
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z_]\w*', code, re.ASCII):
                raise ValueError('related filter requires a scoped entity')
            entity = translator._get_entity(code, model_id)
            if not entity:
                raise ValueError('related filter entity is outside semantic scope')
            entities[key] = entity
        if item['outer_entity'] != (ast.get('subject') or {}).get('entity'):
            raise ValueError('related filter outer entity must be the query subject')
        for key, field in [('outer_entity', outer), ('target_entity', target), ('bridge_entity', bridge)]:
            base = entities[key].get('physical_table_join', {}).get('base_table')
            if base != field.split('.')[0]:
                raise ValueError('related filter field does not belong to its declared entity')
        if bridge.split('.')[0] != shared.split('.')[0] or bridge == shared:
            raise ValueError('shared field must be a different column of the bridge')
        edges = set()
        catalog = getattr(translator, 'catalog', None)
        graph_loader = getattr(catalog, 'entity_relationship_metadata', None)
        graph = graph_loader(model_id) if callable(graph_loader) else {}
        for entity in [*entities.values(), *graph.values()]:
            for relation in entity.get('relations') or []:
                match = re.fullmatch(r'\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*\.[A-Za-z_]\w*)\s*', str(relation.get('join_key') or ''), re.ASCII)
                if match:
                    edges.add(frozenset(match.groups()))
        if frozenset((outer, target)) not in edges or frozenset((target, bridge)) not in edges:
            raise ValueError('related filter join is not a published scoped relation')
        if not any(shared in edge for edge in edges):
            raise ValueError('shared attribute has no published relation')
        target_filters = item.get('target_filters')
        if not isinstance(target_filters, list) or not target_filters or len(target_filters) > 50:
            raise ValueError('related filter must preserve target predicates')
        fields = [outer, target, bridge, shared]
        for predicate in target_filters:
            if not isinstance(predicate, dict) or not re.fullmatch(r'[A-Za-z_]\w*\.[A-Za-z_]\w*', str(predicate.get('field') or ''), re.ASCII):
                raise ValueError('invalid related target field')
            fields.append(predicate['field'])
        catalog = getattr(translator, 'catalog', None)
        if catalog is None:
            raise ValueError('related filters require the scoped semantic catalog')
        authorized = catalog.attribute_metadata(model_id, fields)
        if any(not authorized.get(field) for field in fields):
            raise ValueError('related filter contains an unregistered scoped field')
        target_entity = item['target_entity']
        target_table = target.split('.')[0]
        from_clause = translator._build_from_clause(target_entity, model_id)
        joins = translator._detect_additional_joins(target_entity, [], target_filters, [], model_id)
        if joins:
            from_clause += ' ' + ' '.join(joins)
        present = translator._extract_tables_from_join(from_clause) | {target_table}
        if any(p['field'].split('.')[0] not in present for p in target_filters):
            raise ValueError('related target predicate has no executable scoped join')
        where = translator._build_filter_clause(target_filters, [], target_table)
        target_sql = f'SELECT {target} {from_clause} {where}'
        bridge_table, key_column = bridge.split('.')
        shared_column = shared.split('.')[1]
        left, right = f'_related_{number}', f'_target_{number}'
        # An uncorrelated membership set can be materialized once. Nested
        # correlated EXISTS caused repeated scans per fact row on live MySQL.
        # IN still counts each outer fact once, regardless of bridge fan-out.
        conditions.append(
            f'{outer} IN (SELECT {left}.{key_column} FROM {bridge_table} {left} '
            f'JOIN {bridge_table} {right} '
            f'ON {right}.{shared_column} = {left}.{shared_column} '
            f'WHERE {right}.{key_column} IN ({target_sql}))'
        )
    return conditions
