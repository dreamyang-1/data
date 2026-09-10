"""Export existing structured-filter operand contracts for model generation.

This is a schema view, not an edit repair. The runtime still validates targets,
current evidence, field ownership, membership, Boolean placement and scope.
"""
from copy import deepcopy


def filter_operation_schema(schema, values):
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
    return schema
