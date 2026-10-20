"""Execute bounded calculation contracts over complete, scoped task results."""
from __future__ import annotations

import copy
import re

from minio_followup_store import apply_followup_operation, dataset_source_complete
from app.services.computation_delivery import aligned_input, has_group_identity, numeric_value


def _provenance(reference):
    return next((item for item in reference.transformation_log
                 if item.get('type') == 'query_provenance'), {})


def _column(reference, declared, *, role=None):
    if isinstance(declared, str) and declared in reference.columns:
        return declared
    roles = _provenance(reference).get('calculation_columns', {})
    candidates = roles.get('month', []) if role == 'month' else []
    if len(candidates) == 1 and candidates[0] in reference.columns:
        return candidates[0]
    raise ValueError('计算字段未出现在指定任务的实际结果中：' + str(declared))


def _numeric_column(reference, rows, wording):
    from app.planning.dependency_contract import key_kind
    candidates = []
    for column in reference.columns:
        if str(column).startswith('_') or key_kind(str(column)) != 'display':
            continue
        try:
            for row in rows:
                numeric_value(row.get(column))
        except ValueError:
            continue
        candidates.append(column)
    mentioned = [column for column in candidates if column in wording]
    choices = mentioned or candidates
    if len(choices) != 1:
        raise ValueError('计算输入有多个数值字段，必须明确指定字段')
    return choices[0]


def _legacy_division(task, inputs):
    """Compatibility for explicit old division wording; never pick the last column."""
    wording = task.question
    divisions = re.split(r'除以|÷|/', wording)
    if len(divisions) == 2:
        operands = []
        for part in divisions:
            matches = [(name, column) for name, (ref, _) in inputs.items()
                       for column in ref.columns if column in part and not column.startswith('_')]
            if not matches:
                return None
            longest = max(len(column) for _, column in matches)
            matches = [item for item in matches if len(item[1]) == longest]
            if len(matches) != 1:
                return None
            name, column = matches[0]
            operands.append({'source_task_id': name, 'column': column})
    elif any(word in wording for word in ('覆盖率', '占比', '比例')) and len(inputs) == 2:
        scalar = [(name, data) for name, data in inputs.items() if len(data[1]) == 1]
        multi = [(name, data) for name, data in inputs.items() if len(data[1]) > 1]
        if len(scalar) != 1 or len(multi) != 1:
            return None
        operands = [{'source_task_id': name, 'column': _numeric_column(*data, wording)}
                    for name, data in [multi[0], scalar[0]]]
    else:
        return None
    label = next((name for name in ('区域医院覆盖率', '覆盖率', '占比', '比例')
                  if name in wording), None)
    if label is None:
        match = re.search(r'计算([^，,。；;（(]{2,60})', wording)
        if match is None:
            return None
        label = match[1]
    return {'operation': 'divide', 'numerator': operands[0], 'denominator': operands[1],
            'result_field': label, 'format': 'percent' if re.search('率|占比|比例', label) else 'number'}


def _check_compatible(first, second, *, same_unit):
    left = _provenance(first).get('fingerprint_components', {})
    right = _provenance(second).get('fingerprint_components', {})
    if left and right:
        if left.get('time_range') != right.get('time_range'):
            raise ValueError('计算输入的时间范围不一致')
        left_units = {m.get('unit') for m in left.get('metrics', []) if isinstance(m, dict) and m.get('unit')}
        right_units = {m.get('unit') for m in right.get('metrics', []) if isinstance(m, dict) and m.get('unit')}
        if same_unit and left_units and right_units and left_units != right_units:
            raise ValueError('计算输入的单位不兼容')


def _divide(binding, inputs):
    if binding.get('operation') != 'divide' or binding.get('format', 'number') not in {'number', 'percent'}:
        raise ValueError('不支持的计算公式或输出格式')
    roles = {}
    for role in ('numerator', 'denominator'):
        operand = binding.get(role)
        if not isinstance(operand, dict) or set(operand) - {'source_task_id', 'column'}:
            raise ValueError('计算操作数必须绑定前序任务和字段')
        name = operand.get('source_task_id')
        if name not in inputs:
            raise ValueError('计算引用了非依赖任务')
        reference, rows = inputs[name]
        column = _column(reference, operand.get('column'))
        roles[role] = (reference, rows, column)
    first, left, left_column = roles['numerator']
    second, right, right_column = roles['denominator']
    _check_compatible(first, second, same_unit=binding.get('format') == 'percent')
    label = binding.get('result_field')
    if not isinstance(label, str) or not label.strip() or label.startswith('_'):
        raise ValueError('缺少计算结果字段名')
    if first.dataset_id == second.dataset_id:
        columns, rows = list(first.columns), [dict(row) for row in left]
        operands = [(row[left_column], row[right_column]) for row in rows]
    elif ((len(left) == 1 and not has_group_identity(first))
          or (len(right) == 1 and not has_group_identity(second))):
        left_scalar = len(left) == 1 and not has_group_identity(first)
        right_scalar = len(right) == 1 and not has_group_identity(second)
        main, source_rows = (second, right) if left_scalar and not right_scalar else (first, left)
        columns, rows = list(main.columns), [dict(row) for row in source_rows]
        operands = [(left[0 if len(left) == 1 else index][left_column],
                     right[0 if len(right) == 1 else index][right_column]) for index in range(len(rows))]
    else:
        columns, rows = aligned_input([(first, left), (second, right)])
        operands = [(row[left_column], row[right_column]) for row in rows]
    if label in columns:
        raise ValueError('计算结果不能覆盖输入字段')
    for row, (left_value, right_value) in zip(rows, operands):
        numerator, denominator = numeric_value(left_value), numeric_value(right_value)
        if denominator <= 0:
            raise ValueError('计算分母必须大于零')
        row['_calculation_left'], row['_calculation_right'] = float(numerator), float(denominator)
    operation = {'type': 'derive', 'operator': 'divide', 'left_field': '_calculation_left',
                 'right_field': '_calculation_right', 'result_field': label}
    result_columns, result_rows = apply_followup_operation(
        [*columns, '_calculation_left', '_calculation_right'], rows, operation)
    visible = [column for column in result_columns if not column.startswith('_')]
    rows = [{column: row.get(column) for column in visible} for row in result_rows]
    evidence = {role: {'source_task_id': binding[role]['source_task_id'], 'dataset_id': ref.dataset_id,
                       'column': column} for role, (ref, _, column) in roles.items()}
    operation.update(left_field=left_column, right_field=right_column)
    return visible, rows, operation, evidence


def _steps(columns, rows, operation):
    from app.planning.calculation_bindings import validate_result_operation
    for step in validate_result_operation(operation):
        if step['type'] == 'monthly_growth':
            from app.services.period_computation import monthly_growth
            columns, rows = monthly_growth(columns, rows, step)
            continue
        if step['type'] == 'derive':
            for row in rows:
                numeric_value(row.get(step.get('left_field')))
                denominator = numeric_value(row.get(step.get('right_field')))
                if step.get('operator') == 'divide' and denominator == 0:
                    raise ValueError('计算分母为零')
        columns, rows = apply_followup_operation(columns, rows, step)
    return list(columns), rows


def calculate(task, inputs, legacy_resolver):
    """Bindings choose operands; row count can only choose alignment/broadcast."""
    if not inputs or set(inputs) != set(task.depends_on):
        raise ValueError('计算缺少指定的依赖任务')
    for reference, rows in inputs.values():
        if not rows or not dataset_source_complete(reference, len(rows)):
            raise ValueError('计算输入不是完整数据集')
        if _provenance(reference).get('source_quality_status') == 'FAIL':
            raise ValueError('计算输入未通过质量检查')
    structured = task.extraction or {}
    binding, spec = structured.get('计算绑定'), structured.get('结果操作')
    if binding is not None and spec is not None:
        raise ValueError('计算公式与结果操作不能同时定义')
    legacy = binding is None and spec is None
    binding = binding or (_legacy_division(task, inputs) if legacy else None)
    percent_label = None
    if binding is not None:
        columns, rows, operation, input_binding = _divide(binding, inputs)
        if binding.get('format') == 'percent':
            percent_label = binding['result_field']
        if legacy and not structured.get('排序') and structured.get('限制') is None:
            old_operation = legacy_resolver(task.question, columns, rows, source_complete=True)
            if isinstance(old_operation, dict):
                old_steps = old_operation.get('operations', []) if old_operation.get('type') == 'pipeline' else [old_operation]
                ranking = [step for step in old_steps if step.get('type') in {'sort', 'sort_limit', 'limit'}]
                if ranking:
                    columns, rows = _steps(columns, rows, {'type': 'pipeline', 'operations': ranking})
                    operation = {'type': 'pipeline', 'operations': [operation, *ranking]}
    elif spec is not None:
        if not isinstance(spec, dict) or set(spec) - {'source_task_id', 'operation'}:
            raise ValueError('结果操作必须绑定指定任务，不能覆盖数据集身份')
        name = spec.get('source_task_id')
        if name not in inputs or len(inputs) != 1:
            raise ValueError('结果操作必须明确绑定唯一依赖任务')
        source, original_rows = inputs[name]
        from app.planning.calculation_bindings import normalize_result_operation
        operation = normalize_result_operation(spec.get('operation'))
        input_binding = {'source_task_id': name, 'dataset_id': source.dataset_id}
        steps = operation.get('operations', []) if isinstance(operation, dict) and operation.get('type') == 'pipeline' else [operation]
        for step in steps:
            if isinstance(step, dict) and step.get('type') == 'monthly_growth':
                declared = step.get('period_field')
                actual = _column(source, declared, role='month')
                step['period_field'] = actual
                roles = _provenance(source).get('calculation_columns', {}).get('dimension_identity', {})
                step['group_by'] = [roles.get(group, group) for group in step.get('group_by', [])]
                input_binding['period_column'] = {'declared': declared, 'actual': actual}
        columns, rows = _steps(list(source.columns), [dict(row) for row in original_rows], operation)
    else:
        columns, rows = aligned_input(list(inputs.values()))
        operation = legacy_resolver(task.question, columns, rows, source_complete=True)
        if operation is None:
            raise ValueError('任务没有可执行的计算公式或结果操作')
        columns, rows = _steps(columns, rows, operation)
        input_binding = {name: {'dataset_id': ref.dataset_id} for name, (ref, _) in inputs.items()}
    # Structured ranking belongs to the same calculation, even when its prose
    # omits TopN. Keep values numeric until ordering has completed.
    final_steps = []
    for sort in structured.get('排序') or []:
        field = sort.get('field')
        if field not in columns or str(sort.get('order', 'desc')).lower() not in {'asc', 'desc'}:
            raise ValueError('计算排序字段或方向无效')
        final_steps.append({'type': 'sort', 'field': field, 'descending': str(sort.get('order', 'desc')).lower() == 'desc'})
    if structured.get('限制') is not None:
        count = structured['限制']
        if type(count) is not int or not 1 <= count <= 10000:
            raise ValueError('计算结果人数限制无效')
        final_steps.append({'type': 'limit', 'count': count})
    if final_steps:
        columns, rows = _steps(columns, rows, {'type': 'pipeline', 'operations': final_steps})
        operation = {'type': 'pipeline', 'operations': [operation, *final_steps]}
    if percent_label:
        for row in rows:
            row[percent_label] = f'{numeric_value(row[percent_label])*100:.2f}%'
    operation = {**operation, 'input_binding': input_binding}
    quality = 'WARN' if any(_provenance(ref).get('source_quality_status') == 'WARN' for ref, _ in inputs.values()) else 'PASS'
    return columns, rows, operation, input_binding, quality
