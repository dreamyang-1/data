"""Export existing structured-filter operand contracts for model generation.

This is a schema view, not an edit repair. The runtime still validates targets,
current evidence, field ownership, membership, Boolean placement and scope.
"""
from copy import deepcopy


def filter_operation_schema(schema, values, parse=None, *, context_relation=None):
    """ADD/new takes a predicate; targeted edits take values or delete a subtree.

Reuse the existing handle-based value schemas. Copy only reachable definitions
so the generation contract does not duplicate the complete TaskState schema.
"""
    definitions = values['$defs']
    copied = set()

    def include(node):
        if isinstance(node, dict):
            ref = node.get('$ref')
            if ref and ref.startswith('#/$defs/'):
                name = ref.rsplit('/', 1)[1]
                if name not in copied:
                    copied.add(name)
                    definition = deepcopy(definitions[name])
                    if name in schema['$defs'] and schema['$defs'][name] != definition:
                        raise ValueError('FILTER_GENERATION_SCHEMA_DEFINITION_CONFLICT')
                    schema['$defs'][name] = definition
                    include(definition)
            for child in node.values():
                include(child)
        elif isinstance(node, list):
            for child in node:
                include(child)

    predicate = {'anyOf': [{'$ref': '#/$defs/' + name} for name in ('Predicate', 'AliasedPredicate')]}
    value = deepcopy(definitions['Predicate']['properties']['value'])
    include(predicate)
    for name in ('Predicate', 'AliasedPredicate'):
        properties = schema['$defs'][name]['properties']
        properties['source'] = {'const': 'USER_EXPLICIT', 'type': 'string'}
        properties['scope'] = {'const': 'CURRENT_TASK', 'type': 'string'}
    edit = schema['$defs']['FilterEditDraft']
    edit['oneOf'] = [
        {'required': ['value'], 'properties': {
            'operation': {'const': 'ADD'}, 'target_handle': {'type': 'null'}, 'value': predicate}},
        {'required': ['target_handle', 'value'], 'properties': {
            'operation': {'enum': ['ADD', 'REPLACE']}, 'target_handle': {'type': 'string'}, 'value': value}},
        {'required': ['target_handle'], 'properties': {
            'operation': {'const': 'REMOVE'}, 'target_handle': {'type': 'string'},
            'value': {'anyOf': [value, {'type': 'null'}]}}},
        {'required': ['target_handle'], 'properties': {
            'operation': {'const': 'CLEAR'}, 'target_handle': {'type': 'string'}, 'value': {'type': 'null'}}},
    ]
    # The whole-slot channel carries one complete tree, including OR/NOT.
    # It is not the structured ADD/member-edit channel. Export the same native
    # type instead of leaving its operand as unrestricted JsonValue.
    expression = deepcopy(values['properties']['filter_expression'])
    include(expression)
    slot = schema['$defs']['SlotEditDraft']
    slot.setdefault('allOf', []).append({
        'if': {'properties': {'slot_path': {'const': 'filter_expression'}}},
        'then': {'properties': {'value': expression}},
    })
    whole_filter = {'required': ['slot_path'], 'properties': {'slot_path': {'const': 'filter_expression'}}}
    whole_channel = {'required': ['edits'], 'properties': {'edits': {'contains': whole_filter}}}
    schema.setdefault('allOf', []).extend([
        {'if': whole_channel, 'then': {'properties': {'filter_edits': {'maxItems': 0}}}},
        {'if': {'required': ['source_value_requests'],
                'properties': {'source_value_requests': {'minItems': 1}}},
         'then': {'anyOf': [whole_channel, {'required': ['filter_edits'],
             'properties': {'filter_edits': {'minItems': 1}}}]}},
    ])
    schema['properties']['source_value_requests']['description'] = (
        'Lookup dependencies, not applied filters. Every request_id must be referenced as '
        'value_request_id inside a current filter operand; value_field_request_id refers to '
        'that same selected field. Preserve the requested predicate operator and Boolean tree. '
        'Multiple field hypotheses belong to one request, not extra conditions. '
        'Do not emit requests for unrequested conditions or leave unused lookup requests.')
    schema['properties']['edits']['description'] = (
        'Whole filter assignment uses one filter_expression SET with the complete Predicate/Boolean tree. '
        'Use this channel for a declared filter SET, never a filter_edits ADD. '
        'Do not mix whole-filter and structured filter edits. Match current operation markers; '
        'do not change a declared collection ADD to SET merely because this is a new task.')
    schema['properties']['filter_edits']['description'] = (
        'Structured filter operations: ADD/no target supplies a complete new Predicate; '
        'targeted edits modify only the offered subtree/member values. This channel has no SET. '
        'Source-value operands must reference their declared request_id. '
        'Use whole filter_expression SET when the current declaration is SET.')
    # Narrow only uniform declarations on a newly resolved task. Mixed
    # operations remain representable; the runtime checks each current mention.
    # Existing-task operations and their established adapters are unchanged.
    if context_relation == 'NEW_TASK' and parse is not None:
        by_slot = {}
        for marker in parse.operation_markers:
            by_slot.setdefault(marker.slot_name, set()).add(marker.operation_hint)
        for name in ('metrics', 'dimensions', 'filter_expression'):
            declared = by_slot.get(name, set())
            if len(declared) != 1:
                continue
            operation = next(iter(declared))
            slot['allOf'].append({'if': {'properties': {'slot_path': {'const': name}}},
                'then': {'properties': {'operation': {'const': operation}}}})
            if name == 'filter_expression':
                if operation == 'SET':
                    schema['properties']['filter_edits']['maxItems'] = 0
                elif operation in edit['properties']['operation']['enum']:
                    edit['properties']['operation'] = {'type': 'string', 'const': operation}
                    # Structured slots do not accept a generic ADD/REMOVE;
                    # expose the already valid structured lowering instead.
                    if operation in {'ADD', 'REMOVE'}:
                        slot['allOf'].append({'not': whole_filter})
    return schema
