"""Finite, catalog-grounded shared-attribute scopes; no business word triggers."""
from query_binding_review import _metadata, _object, _field


def related_fields(ast):
    fields = []
    for item in ast.get('related_filters') or []:
        fields.extend(item.get(k, '') for k in ('outer_field', 'target_field', 'bridge_key', 'shared_field'))
        fields.extend(p.get('field', '') for p in item.get('target_filters') or [])
    return fields


def shared_scope_options(ast, knowledge):
    entities = [_metadata(e) for e in knowledge.get('entities', [])]
    allowed = set(knowledge.get('_vector_authorized_fields') or [])
    fields, owners, labels, edges = {}, {}, {}, set()
    for e in entities:
        code = e.get('entity_code')
        fields[code] = set()
        for a in _object(e.get('attributes')) or []:
            f = _field(a.get('field_mapping'))
            if f and f in allowed:
                fields[code].add(f)
                owners[f] = code
                labels[f] = a.get('attr_name') or a.get('name') or f
        for r in _object(e.get('relations')) or []:
            join = _object(r.get('join_key'))
            if isinstance(join, dict):
                a, b = _field(join.get('source_field')), _field(join.get('target_field'))
                if a in allowed and b in allowed:
                    edges.add(tuple(sorted((a, b))))
    subject = (ast.get('subject') or {}).get('entity')
    predicates = ast.get('filters') or []
    result = []
    for edge in sorted(edges):
        for outer, target in (edge, edge[::-1]):
            target_entity = owners.get(target)
            if owners.get(outer) != subject or target_entity in (None, subject):
                continue
            for link in sorted(edges):
                if target not in link:
                    continue
                bridge_key = link[0] if link[1] == target else link[1]
                bridge_entity = owners.get(bridge_key)
                if bridge_entity in (None, subject, target_entity):
                    continue
                for shared_edge in sorted(edges):
                    for shared, label_key in (shared_edge, shared_edge[::-1]):
                        if (owners.get(shared) != bridge_entity or shared == bridge_key
                                or owners.get(label_key) in (None, subject, target_entity, bridge_entity)):
                            continue
                        # Target modifiers may live on direct dimension neighbors;
                        # hospital/outer-fact filters must stay in the outer query.
                        target_fields = set(fields[target_entity])
                        for a, b in edges:
                            if owners.get(a) == target_entity and owners.get(b) not in (subject, bridge_entity):
                                target_fields.update(fields.get(owners.get(b), set()))
                            if owners.get(b) == target_entity and owners.get(a) not in (subject, bridge_entity):
                                target_fields.update(fields.get(owners.get(a), set()))
                        indices = [i for i, p in enumerate(predicates)
                                   if p.get('field') in target_fields or p.get('field') == outer]
                        if not indices:
                            continue
                        option = dict(kind='shared_attribute', outer_entity=subject, outer_field=outer,
                                      target_entity=target_entity, target_field=target,
                                      bridge_entity=bridge_entity, bridge_key=bridge_key,
                                      shared_field=shared, shared_label=labels.get(label_key, label_key),
                                      target_filter_indices=indices)
                        if option not in result:
                            result.append(option)
    return result


def apply_shared_scope(ast, options, decision, question):
    if not isinstance(decision, dict) or decision.get('mode') != 'shared_attribute':
        return []
    index, quote = decision.get('option_index'), decision.get('evidence')
    if (type(index) is not int or not 0 <= index < len(options)
            or not isinstance(quote, str) or len(quote.strip()) < 4 or quote not in question):
        return []
    option = options[index]
    moved = []
    indices = option['target_filter_indices']
    for i in indices:
        item = dict(ast['filters'][i])
        if item['field'] == option['outer_field']:
            item['field'] = option['target_field']
        moved.append(item)
    relation = {k: v for k, v in option.items() if k not in {'target_filter_indices', 'shared_label'}}
    relation['target_filters'] = moved
    # Explanation is deterministic, not a model-authored assertion of actual
    # clinical transactions. The metric aggregates qualifying outer records.
    relation['scope_note'] = (
        f"按与目标对象具有共同{option['shared_label']}的已销售对象筛选，"
        "按保留的医院等条件内符合关联范围的销售记录汇总排名；"
        "这是对象属性关联口径，不证明订单实际成交到该科室或部门。"
    )
    ast['related_filters'] = [relation]
    ast['filters'] = [p for i, p in enumerate(ast['filters']) if i not in indices]
    return [{'type': 'SHARED_ATTRIBUTE_SCOPE', 'evidence': quote, 'scope_note': relation['scope_note']}]
