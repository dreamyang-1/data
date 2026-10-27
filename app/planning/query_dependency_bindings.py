"""Read the declared predecessor column, preserving its executed field identity."""
from __future__ import annotations


def select_dependency_column(source_column: str, columns, column_bindings: dict[str, str],
                             projection_bindings: dict[str, str] | None = None) -> str:
    # No prose, entity-family heuristics or matching on row values. A qualified
    # field can also identify its exact executed projection (including aliases).
    if source_column in columns:
        return source_column
    field = (projection_bindings or {}).get(source_column, source_column)
    matches = [column for column in columns if column_bindings.get(column) == field]
    if matches:
        return matches[0]  # these aliases all refer to the very same physical field
    raise ValueError('DEPENDENCY_DECLARED_COLUMN_MISSING')


def executed_projection_bindings(transforms, column_bindings):
    """Only link labels whose resolved physical field was actually projected."""
    candidates = {}
    for item in transforms:
        if item.get('type') != 'STRUCTURED_PROJECTION_BINDING':
            continue
        label, field = item.get('field'), item.get('resolved_field')
        if not isinstance(label, str) or field not in column_bindings.values():
            continue
        labels = [label]
        if item.get('entity'):
            labels.append(str(item['entity']) + '.' + label)
        for name in labels:
            candidates.setdefault(name, set()).add(field)
    return {name: next(iter(fields)) for name, fields in candidates.items() if len(fields) == 1}


def inherited_projection_bindings(column_bindings, references):
    transforms = []
    for reference, _ in references.values():
        for item in reference.transformation_log:
            if item.get('type') == 'query_provenance':
                transforms.extend({'type': 'STRUCTURED_PROJECTION_BINDING',
                    'field': label, 'resolved_field': field}
                    for label, field in item.get('projection_bindings', {}).items())
    return executed_projection_bindings(transforms, column_bindings)
