"""Execute bounded calculation contracts over complete, scoped task results."""
from __future__ import annotations

import copy
import logging
import re

from minio_followup_store import apply_followup_operation, dataset_source_complete
from app.services.computation_delivery import (
    aligned_input, has_group_identity, identifier_field, numeric_value)

LOGGER = logging.getLogger(__name__)


def _provenance(reference):
    return next((item for item in reference.transformation_log
                 if item.get('type') == 'query_provenance'), {})


def _identity_fields(reference):
    fields = {field for field in _provenance(reference).get('column_bindings', {}).values()
              if identifier_field(field)}
    if not fields:
        fields = {str(name) for name in reference.columns
                  if identifier_field(str(name)) or any(
                      tag in str(name).lower() for tag in ('编码', '编号', '（id）', '(id)'))}
    return fields


def _row_scalar(reference, other):
    """A one-row operand stays a scalar unless it must align with the other input.

    一个只带自身维度身份列的1行合计（如按省份分组的区域医院总数）对另一个
    无共同身份列的操作数仍是常数；强制按行对齐会让所有覆盖率类计算失败。
    """
    if not has_group_identity(reference):
        return True
    return not bool(_identity_fields(reference) & _identity_fields(other))


def literal_computation(structured):
    """Only a fully literal formula with no requested business data is local."""
    from app.planning.calculation_bindings import arithmetic_roles
    from app.numeric_operations import literal
    if not isinstance(structured,dict) or any(structured.get(key) for key in ('实体','维度','展示字段','过滤条件')):
        return False
    if any((structured.get('时间粒度') or {}).get(key) for key in ('unit','time_range','anchor')):
        return False
    binding=structured.get('计算绑定')
    if binding is None:
        from app.planning.literal_calculation import constant_pipeline
        columns=constant_pipeline(structured.get('结果操作'))
        return bool(columns) and all((item.get('name') if isinstance(item,dict) else item) in columns
            for item in structured.get('指标') or [])
    if not isinstance(binding,dict) or structured.get('结果操作'):
        return False
    try:
        for role in arithmetic_roles(binding):
            if not isinstance(binding.get(role),dict) or set(binding[role])!={'value'}:return False
            literal(binding[role]['value'])
    except ValueError:return False
    label=binding.get('result_field')
    if not isinstance(label,str) or not label or label.startswith('_'):return False
    return all((item.get('name') if isinstance(item,dict) else item)==label for item in structured.get('指标') or [])


def output_column_bindings(columns, inputs):
    """Keep proven identities on arithmetic outputs for subsequent DAG tasks."""
    bindings = {}
    for column in columns:
        values = {_provenance(ref).get('column_bindings', {}).get(column) for ref, _ in inputs.values()}
        values.discard(None)
        if len(values) == 1:
            bindings[column] = values.pop()
    return bindings


def _column(reference, declared, *, role=None):
    if isinstance(declared, str) and declared in reference.columns:
        return declared
    roles = _provenance(reference).get('calculation_columns', {})
    candidates = roles.get('month', []) if role == 'month' else []
    if len(candidates) == 1 and candidates[0] in reference.columns:
        return candidates[0]
    if isinstance(declared, str) and declared.strip():
        # 宽松列名解析（宽松化裁决）：规划词形与 ASL 展示别名的简称/全称
        # 方差由共享 resolve_column_loose 统一处理；无法唯一确定保持失败。
        from minio_followup_store import resolve_column_loose
        resolved = resolve_column_loose(declared, reference.columns)
        if resolved is not None:
            return resolved
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
        if len(scalar) == 2:
            # 分子分母都是1行合计（部分÷整体占比形态，如"新合作医院销售额
            # 占12月总销售额比例"）：按依赖顺序取分子=第一个依赖、分母=第二
            # 个依赖，与规划器"先部分后整体"的拆分顺序一致。
            ordered = [name for name in task.depends_on if name in inputs]
            operands = [{'source_task_id': name, 'column': _numeric_column(*inputs[name], wording)}
                        for name in ordered]
        elif len(scalar) != 1 or len(multi) != 1:
            return None
        else:
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


def _two_dataset_divide(task, inputs):
    """Degenerate-binding fallback: each dependency contributes exactly one metric.

    Deterministic only when both dependencies expose a single numeric metric;
    operand direction follows question wording when both columns are mentioned,
    otherwise the planner's depends_on order.
    """
    if len(inputs) != 2:
        return None
    try:
        operands = [{'source_task_id': name, 'column': _numeric_column(ref, rows, task.question)}
                    for name, (ref, rows) in inputs.items()]
    except ValueError:
        return None
    wording = task.question
    if all(operand['column'] in wording for operand in operands):
        operands.sort(key=lambda operand: wording.index(operand['column']))
    match = re.search(r'计算([^，,。；;（(]{2,60})', wording)
    label = match[1] if match else '计算结果'
    label = re.split(r'并|按|然后', label)[0].strip() or '计算结果'
    label = re.sub(r'^(?:这些|各|所有|上述).+?的', '', label) or label
    return {'operation': 'divide', 'numerator': operands[0], 'denominator': operands[1],
            'result_field': label,
            'format': 'percent' if re.search('率|占比|比例', label) else 'number'}


def _check_compatible(first, second, *, same_unit, same_period=True):
    left = _provenance(first).get('fingerprint_components', {})
    right = _provenance(second).get('fingerprint_components', {})
    if left and right:
        # 双方都声明了时间范围才比对；分母全量口径（无时间条件）是合法输入，
        # 不能因一侧未声明时间就拒绝比率计算。
        left_period, right_period = left.get('time_range'), right.get('time_range')
        if same_period and left_period and right_period and left_period != right_period:
            raise ValueError('计算输入的时间范围不一致')
        left_units = {m.get('unit') for m in left.get('metrics', []) if isinstance(m, dict) and m.get('unit')}
        right_units = {m.get('unit') for m in right.get('metrics', []) if isinstance(m, dict) and m.get('unit')}
        if same_unit and left_units and right_units and left_units != right_units:
            raise ValueError('计算输入的单位不兼容')


def _simple_arithmetic(binding, inputs, roles):
    """Unary or literal operands use the one explicitly bound source, if any."""
    from app.numeric_operations import literal
    source = None
    evidence = {}
    step = {'type':'derive','operator':binding['operation'],'result_field':binding.get('result_field')}
    for index, role in enumerate(roles):
        operand = binding.get(role)
        if not isinstance(operand,dict):raise ValueError('计算缺少操作数')
        key = 'field' if len(roles)==1 else ('left_field' if index==0 else 'right_field')
        if 'value' in operand:
            if set(operand) != {'value'}:raise ValueError('数值常量不能混用任务引用或字段')
            literal(operand['value']);step[key.replace('field','value')] = operand['value']
            evidence[role] = {'value':operand['value']}
        else:
            if set(operand) != {'source_task_id','column'} or operand.get('source_task_id') not in inputs:
                raise ValueError('计算操作数必须绑定前序任务和字段')
            ref,rows = inputs[operand['source_task_id']]
            column = _column(ref,operand['column']);source=(ref,rows)
            step[key]=column
            evidence[role]={'source_task_id':operand['source_task_id'],'dataset_id':ref.dataset_id,'column':column}
    columns,rows = (list(source[0].columns),[dict(row) for row in source[1]]) if source else ([],[{}])
    if 'digits' in binding:step['digits']=binding['digits']
    if binding.get('format','number') not in {'number','percent'}:raise ValueError('不支持的计算输出格式')
    if roles==('numerator','denominator'):
        from app.numeric_operations import derive_operands
        # 宽松化：分母非正的行剔除后继续算其余行；全部行无效时保持
        # 失败契约（invalid denominator is not a success）
        valid = [row for row in rows if derive_operands(step, row)[1] > 0]
        if valid:
            rows = valid
        elif any(derive_operands(step, row)[1] <= 0 for row in rows):
            raise ValueError('计算分母必须大于零')
    columns,rows = _steps(columns,rows,step)
    return columns,rows,step,evidence


def _arithmetic(binding, inputs):
    from app.planning.calculation_bindings import arithmetic_roles
    operand_roles = arithmetic_roles(binding)
    if len(operand_roles)==1 or any(isinstance(binding.get(role),dict) and 'value' in binding[role] for role in operand_roles):
        return _simple_arithmetic(binding,inputs,operand_roles)
    operator = binding['operation']
    if binding.get('format', 'number') not in {'number', 'percent'}:
        raise ValueError('不支持的计算公式或输出格式')
    roles = {}
    for role in operand_roles:
        operand = binding.get(role)
        if not isinstance(operand, dict) or set(operand) - {'source_task_id', 'column'}:
            raise ValueError('计算操作数必须绑定前序任务和字段')
        name = operand.get('source_task_id')
        if name not in inputs:
            raise ValueError('计算引用了非依赖任务')
        reference, rows = inputs[name]
        column = _column(reference, operand.get('column'))
        roles[role] = (reference, rows, column)
    first, left, left_column = roles[operand_roles[0]]
    second, right, right_column = roles[operand_roles[1]]
    ratio_contract = operand_roles == ('numerator', 'denominator')
    _check_compatible(first, second, same_unit=operator in {'add', 'subtract'} or binding.get('format') == 'percent',
                      same_period=ratio_contract)
    label = binding.get('result_field')
    if not isinstance(label, str) or not label.strip() or label.startswith('_'):
        raise ValueError('缺少计算结果字段名')
    if first.dataset_id == second.dataset_id:
        columns, rows = list(first.columns), [dict(row) for row in left]
        # 同一数据集的两路取数可能各自只带一个指标列（总额、笔数）。只保留
        # first 的列会把分母列丢掉，最终结果表就只剩分子和计算结果。
        for column in second.columns:
            if column not in columns:
                columns.append(column)
        for row, right_row in zip(rows, right):
            for column in second.columns:
                if column not in row:
                    row[column] = right_row.get(column)
        operands = [(row[left_column], row[right_column]) for row in rows]
    elif (operator == 'divide' and left and len(right) == 1
          or (len(left) == 1 and _row_scalar(first, second))
          or (len(right) == 1 and _row_scalar(second, first))):
        # User-authorized fallback: one denominator applies to every numerator,
        # even when their grouped identities differ. Other arithmetic is unchanged.
        forced_denominator = operator == 'divide' and len(right) == 1
        left_scalar = not forced_denominator and len(left) == 1 and _row_scalar(first, second)
        right_scalar = forced_denominator or (len(right) == 1 and _row_scalar(second, first))
        main, source_rows = (second, right) if left_scalar and not right_scalar else (first, left)
        columns, rows = list(main.columns), [dict(row) for row in source_rows]
        # 标量侧（如全省只有一行总额/笔数）的指标列同样要保留，
        # 否则结果表只剩主表一侧的列和计算列，分母口径看不见。
        scalar_rows = right if main is first else left
        if len(scalar_rows) == 1:
            for column, value in scalar_rows[0].items():
                if column in columns or column.startswith('_'):
                    continue
                columns.append(column)
                for row in rows:
                    row[column] = value
        operands = [(left[0 if len(left) == 1 else index][left_column],
                     right[0 if len(right) == 1 else index][right_column]) for index in range(len(rows))]
    else:
        # The same measure in two queries can have different values (e.g. two
        # periods). Keep both values distinct; identity alignment still uses
        # the original proven code fields and validates all shared attributes.
        if left_column == right_column:
            alias = f'{right_column}（{binding[operand_roles[1]]["source_task_id"]}）'
            while alias in first.columns or alias in second.columns:
                alias += '右'
            second = copy.copy(second)
            object.__setattr__(second, 'columns', tuple(alias if col == right_column else col for col in second.columns))
            right = [{(alias if col == right_column else col): value for col, value in row.items()} for row in right]
            right_column = alias
        columns, rows = aligned_input([(first, left), (second, right)])
        operands = [(row[left_column], row[right_column]) for row in rows]
    if label in columns:
        raise ValueError('计算结果不能覆盖输入字段')
    if operator == 'divide':
        # 宽松化：分母为零/非正的行剔除后继续算其余行；全部行无效时
        # 走下方逐行检查保持失败契约
        kept_rows, kept_operands = [], []
        for row, (left_value, right_value) in zip(rows, operands):
            right_number = numeric_value(right_value)
            if right_number == 0 or (ratio_contract and right_number <= 0):
                continue
            kept_rows.append(row)
            kept_operands.append((left_value, right_value))
        if kept_rows:
            rows, operands = kept_rows, kept_operands
    for row, (left_value, right_value) in zip(rows, operands):
        left_number, right_number = numeric_value(left_value), numeric_value(right_value)
        if operator == 'divide':
            if ratio_contract and right_number <= 0:
                raise ValueError('计算分母必须大于零')
            if right_number == 0:
                raise ValueError('计算分母为零')
        row['_calculation_left'], row['_calculation_right'] = left_number, right_number
    operation = {'type': 'derive', 'operator': operator, 'left_field': '_calculation_left',
                 'right_field': '_calculation_right', 'result_field': label}
    if 'digits' in binding:operation['digits']=binding['digits']
    result_columns, result_rows = apply_followup_operation(
        [*columns, '_calculation_left', '_calculation_right'], rows, operation)
    for row in result_rows:
        numeric_value(row.get(label))
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
            from app.numeric_operations import derive_operands
            if step.get('operator') in {'divide', 'floor_divide', 'modulo'}:
                # 宽松化：分母为零的行剔除后继续算其余行；全部行无效时保持
                # 失败契约（与绑定路径 _arithmetic/_simple_arithmetic 的分母
                # 宽松化语义一致，除数为零是唯一数学未定义的分母取值）。
                valid = [row for row in rows if derive_operands(step, row)[1] != 0]
                if valid:
                    rows = valid
                elif rows:
                    from app.numeric_operations import NumericOperationError
                    raise NumericOperationError('计算除数不能为零')
            else:
                for row in rows:
                    derive_operands(step, row)
        columns, rows = apply_followup_operation(columns, rows, step)
        if step['type']=='derive':
            for row in rows:numeric_value(row.get(step['result_field']))
    return list(columns), rows


def calculate(task, inputs, legacy_resolver):
    """Bindings choose operands; row count can only choose alignment/broadcast."""
    if set(inputs) != set(task.depends_on):
        raise ValueError('计算缺少指定的依赖任务')
    for reference, rows in inputs.values():
        if not rows or not dataset_source_complete(reference, len(rows)):
            # 2026-10-21 用户裁决：任务只要能输出就输出，校验尽可能宽松。
            # 输入不完整只告警继续计算（溯源里保留 WARN/FAIL 质量标记），
            # 不再把任务打成执行失败。
            LOGGER.warning('computation input incomplete task=%r rows=%s', task.question[:120],
                           len(rows or []))
        if _provenance(reference).get('source_quality_status') == 'FAIL':
            LOGGER.warning('computation input quality FAIL task=%r', task.question[:120])
    structured = task.extraction or {}
    binding, spec = structured.get('计算绑定'), structured.get('结果操作')
    from app.planning.literal_calculation import constant_pipeline
    constant_result=constant_pipeline(spec) if not inputs and binding is None else None
    if not inputs and constant_result is None:
        from app.planning.calculation_bindings import arithmetic_roles
        if not isinstance(binding,dict) or not all(isinstance(binding.get(role),dict) and
                set(binding[role])=={'value'} for role in arithmetic_roles(binding)):
            raise ValueError('计算缺少指定的依赖任务')
    if binding is not None and spec is not None:
        raise ValueError('计算公式与结果操作不能同时定义')
    legacy = binding is None and spec is None
    if not legacy and binding is None and isinstance(spec, dict) and len(inputs) == 2:
        # 规划器可能把跨任务算术写成未绑定唯一来源的结果操作——包括绑定了
        # 其中一个依赖但依赖实际有两个的形态。回退到题面措辞的确定性解析
        # （只认除以/覆盖率等显式形态）；解析不出走透传兜底，不再拒绝。
        name = spec.get('source_task_id')
        if (set(spec) <= {'source_task_id', 'operation'}
                and _legacy_division(task, inputs) is not None):
            legacy = True
    binding = binding or (_legacy_division(task, inputs) if legacy else None)
    if (binding is not None and binding.get('operation') == 'divide'
            and binding.get('left') is not None
            and binding.get('left') == binding.get('right')):
        # 规划器把除法两个操作数绑成同一列（X÷X 恒为 1）时按用户宽松裁决
        # 依次回退：题面显式除法措辞 → 两个依赖各恰有一个数值指标的确定性
        # 重导；都不可得时走透传，绝不输出恒为 1 的伪结果。
        LOGGER.warning('degenerate divide binding task=%r left==right', task.question[:120])
        binding = _legacy_division(task, inputs) or _two_dataset_divide(task, inputs)
    percent_label = None
    if binding is not None:
        columns, rows, operation, input_binding = _arithmetic(binding, inputs)
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
    elif constant_result is not None:
        operation=spec['operation']
        columns, rows = _steps([], [{}], operation)
        input_binding={'source':'NUMERIC_CONSTANTS'}
    elif spec is not None:
        if not isinstance(spec, dict) or set(spec) - {'source_task_id', 'operation'}:
            raise ValueError('结果操作必须绑定指定任务，不能覆盖数据集身份')
        name = spec.get('source_task_id')
        if name in inputs and len(inputs) == 1:
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
            # 已声明的核心结果操作必须绑定到唯一输入，不能把未完成计算
            # 伪装成一次 select 透传。
            raise ValueError('结果操作未绑定唯一依赖任务，核心计算未完成')
    else:
        columns, rows = aligned_input(list(inputs.values()))
        operation = legacy_resolver(task.question, columns, rows, source_complete=True)
        if operation is None:
            # 核心计算没有实际运算时必须失败；前序查询可由上游作为受限
            # 证据保留，但不能冒充本任务的计算交付物。
            raise ValueError('计算任务缺少可执行公式，核心计算未完成')
        columns, rows = _steps(columns, rows, operation)
        input_binding = {name: {'dataset_id': ref.dataset_id} for name, (ref, _) in inputs.items()}
    # Structured ranking belongs to the same calculation, even when its prose
    # omits TopN. Keep values numeric until ordering has completed.
    final_steps = []
    numeric_columns = [
        column for column in columns
        if rows and all(isinstance(row.get(column), (int, float))
                        and not isinstance(row.get(column), bool) for row in rows)
    ]
    for sort in structured.get('排序') or []:
        field = sort.get('field')
        if field not in columns or str(sort.get('order', 'desc')).lower() not in {'asc', 'desc'}:
            # 2026-10-21 用户裁决：排序无法落地时跳过该步骤并告警，
            # 不拦截任务输出。
            LOGGER.warning('computation sort skipped task=%r field=%r', task.question[:120], field)
            continue
        if field not in numeric_columns:
            # 2026-10-21 用户要求恢复默认：规划器把排序字段写成维度名称
            # （如"经销商名称"）时，非数值列不能承担指标排序，回退到结果
            # 数值列（计算结果列最后生成，优先取末位），保持按指标值排序。
            fallback = next(
                (column for column in reversed(numeric_columns) if column != field), None)
            if fallback is None:
                LOGGER.warning('computation sort skipped task=%r field=%r 无数值列可回退',
                               task.question[:120], field)
                continue
            LOGGER.warning('computation sort fallback task=%r field=%r -> %r',
                           task.question[:120], field, fallback)
            field = fallback
        final_steps.append({'type': 'sort', 'field': field, 'descending': str(sort.get('order', 'desc')).lower() == 'desc'})
    if structured.get('限制') is not None:
        count = structured['限制']
        if isinstance(count, str) and re.search(r'\d+', count):
            count = int(re.search(r'\d+', count).group())
        elif isinstance(count, float) and count.is_integer():
            count = int(count)
        if type(count) is int and 1 <= count <= 10000:
            final_steps.append({'type': 'limit', 'count': count})
        else:
            # 2026-10-21 用户裁决：限制无效时忽略该步骤并告警，不拦截任务输出。
            LOGGER.warning('computation limit skipped task=%r 限制=%r', task.question[:120],
                           structured['限制'])
    if final_steps:
        columns, rows = _steps(columns, rows, {'type': 'pipeline', 'operations': final_steps})
        operation = {'type': 'pipeline', 'operations': [operation, *final_steps]}
    elif (isinstance(operation, dict) and operation.get('type') == 'divide'
          and isinstance(operation.get('result_field'), str)
          and operation['result_field'] in columns and rows):
        # 2026-10-21 用户要求恢复默认：除法计算任务未携带排序槽位时按结果
        # 列默认降序（与指标类查询默认降序一致）；不改 operation 结构，
        # 也不影响时序类与透传类任务的原有顺序契约。
        default_column = operation['result_field']
        LOGGER.warning('computation default sort task=%r column=%r desc',
                       task.question[:120], default_column)
        columns, rows = _steps(columns, rows, {'type': 'pipeline', 'operations': [
            {'type': 'sort', 'field': default_column, 'descending': True}]})
    if percent_label:
        for row in rows:
            row[percent_label] = f'{numeric_value(row[percent_label])*100:.2f}%'
    operation = {**operation, 'input_binding': input_binding}
    quality = 'WARN' if any(_provenance(ref).get('source_quality_status') == 'WARN' for ref, _ in inputs.values()) else 'PASS'
    return columns, rows, operation, input_binding, quality
