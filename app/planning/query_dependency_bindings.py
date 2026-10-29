"""Read the declared predecessor column, preserving its executed field identity."""
from __future__ import annotations

import copy


def remap_dependency_inputs(inputs, mapping):
    """Keep input provenance attached when planning inserts/removes tasks."""
    result = copy.deepcopy(inputs)
    for source in result or []:
        if 'source_task_index' not in source:
            continue
        old = source['source_task_index']
        if type(old) is not int or old not in mapping:
            raise ValueError('依赖输入必须引用已规划的前序任务')
        target = mapping[old]
        if isinstance(target, list):
            if len(target) != 1:
                raise ValueError('依赖输入无法唯一对应前序任务交付物')
            target = target[0]
        source['source_task_index'] = target
    return result


def materialize_dependency_inputs(inputs, *, task_index, dependencies, task_offset=0):
    """Freeze planned inputs into task state, independently of extraction."""
    from app.planning.task_dag import TaskInputBindingError
    if inputs is None:
        return None
    result = copy.deepcopy(inputs)
    for source in result:
        if not isinstance(source, dict) or not isinstance(source.get('source_column'), str) or not source['source_column'].strip():
            raise TaskInputBindingError('任务依赖输入缺少前序交付字段')
        if 'source_task_index' in source:
            index = source.pop('source_task_index')
            if type(index) is not int or not 0 <= index < task_index:
                raise TaskInputBindingError('任务依赖输入必须引用前序任务')
            source['source_task_id'] = f'task-{task_offset + index + 1}'
        elif 'source_task_id' not in source and len(dependencies) == 1:
            source['source_task_id'] = dependencies[0]
        if source.get('source_task_id') not in dependencies:
            raise TaskInputBindingError('任务依赖输入必须属于已声明依赖')
    return result


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
