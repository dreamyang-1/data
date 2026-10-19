"""Small, deterministic delivery helpers; no narrative may stand in for data."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Any


def numeric_value(value):
    """Missing, boolean and non-finite values are not arithmetic operands."""
    if value is None or isinstance(value, bool):
        raise ValueError('计算输入缺少有效数值')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError('计算输入不是数值') from exc
    if not number.is_finite():
        raise ValueError('计算输入不是有限数值')
    return number


@dataclass(frozen=True)
class DivisionInput:
    numerator: Any
    numerator_rows: Any
    numerator_column: str
    denominator: Any
    denominator_column: str
    denominator_value: Decimal
    result_column: str
    percentage: bool

    def evidence(self):
        return {'type': 'divide',
                'numerator': {'dataset_id': self.numerator.dataset_id, 'column': self.numerator_column},
                'denominator': {'dataset_id': self.denominator.dataset_id, 'column': self.denominator_column},
                'result_field': self.result_column, 'format': 'percent' if self.percentage else 'number'}


def bind_division(task, references):
    """Resolve a bounded scalar-denominator operation; never infer from column order.

    Structured bindings take precedence. Legacy formulas may name actual columns;
    the old coverage case is retained only when both operands are unambiguous.
    """
    text = '\n'.join(filter(None, (task.question, task.expected_output)))
    spec = (task.extraction or {}).get('计算绑定')

    def resolve(binding):
        if not isinstance(binding, dict) or not isinstance(binding.get('column'), str):
            raise ValueError('计算缺少明确字段绑定')
        dataset_id = binding.get('dataset_id')
        task_id = binding.get('source_task_id')
        if task_id:
            if task_id not in task.depends_on:
                raise ValueError('计算引用未知依赖任务')
            selected = [references[task.depends_on.index(task_id)]]
        else:
            selected = references
        matches = [(ref, rows, binding['column']) for ref, rows in selected
                   if binding['column'] in ref.columns and (not dataset_id or dataset_id == ref.dataset_id)]
        if len(matches) != 1:
            raise ValueError('计算字段或来源不能唯一绑定')
        return matches[0]

    if spec is not None:
        if not isinstance(spec, dict) or spec.get('operation') != 'divide':
            raise ValueError('不支持的计算绑定')
        numerator = resolve(spec.get('numerator'))
        denominator = resolve(spec.get('denominator'))
        if spec.get('format', 'number') not in {'number', 'percent'}:
            raise ValueError('不支持的计算格式')
        percentage = spec.get('format') == 'percent'
        result_column = spec.get('result_field')
        if not isinstance(result_column, str) or not result_column.strip():
            raise ValueError('计算缺少输出字段')
    else:
        columns = sorted({column for ref, _ in references for column in ref.columns
                          if not column.startswith('_')}, key=len, reverse=True)
        names = '|'.join(re.escape(column) for column in columns)
        formula = re.search(rf'(?P<n>{names})\s*(?:除以|÷|/)\s*(?P<d>{names})', text) if names else None
        percentage = any(word in text for word in ('覆盖率', '占比', '比例', '百分比'))
        result_column = next((word for word in ('区域医院覆盖率', '覆盖率', '占比', '比例') if word in text), '计算结果')
        if formula:
            numerator = resolve({'column': formula['n']})
            denominator = resolve({'column': formula['d']})
        else:
            # A partially named formula must not fall back to a different metric.
            if not percentage or re.search(r'除以|÷|/', text):
                raise ValueError('计算缺少明确分子分母')
            totals = [(ref, rows, column) for ref, rows in references for column in ref.columns
                      if column.endswith('总数')]
            if len(totals) != 1:
                raise ValueError('计算缺少唯一总数输入')
            denominator = totals[0]
            candidates = []
            for ref, rows in references:
                if ref.dataset_id == denominator[0].dataset_id:
                    continue
                for column in ref.columns:
                    if column.startswith('_') or re.search(r'编码|编号|(?:^|_)id$|(?:^|_)code$', column, re.I):
                        continue
                    try:
                        if rows:
                            for row in rows:
                                numeric_value(row.get(column))
                            candidates.append((ref, rows, column))
                    except ValueError:
                        continue
            if len(candidates) != 1:
                raise ValueError('计算缺少唯一分子字段')
            numerator = candidates[0]
    nref, nrows, ncolumn = numerator
    dref, drows, dcolumn = denominator
    def query_components(ref):
        return next((item.get('fingerprint_components', {}) for item in reversed(ref.transformation_log)
                     if isinstance(item, dict) and item.get('type') == 'query_provenance'), {})
    nshape, dshape = query_components(nref), query_components(dref)
    if any(word in text for word in ('覆盖率', '占比')):
        if ('time_range' in nshape and 'time_range' in dshape
                and nshape['time_range'] != dshape['time_range']):
            raise ValueError('比例输入统计期间不一致')
    if percentage:
        def unit(shape, column):
            metrics = shape.get('metrics') or []
            return next((item.get('unit') for item in metrics if item.get('name') == column), None)
        nunit, dunit = unit(nshape, ncolumn), unit(dshape, dcolumn)
        if nunit and dunit and nunit != dunit:
            raise ValueError('比例输入单位不兼容')
    if len(drows) != 1 or not nrows:
        raise ValueError('分母需要明确的单值输入，分子不能为空')
    denominator_value = numeric_value(drows[0].get(dcolumn))
    if denominator_value <= 0:
        raise ValueError('计算分母必须为正数')
    for row in nrows:
        numeric_value(row.get(ncolumn))
    if result_column in nref.columns:
        raise ValueError('计算输出不能覆盖来源字段')
    return DivisionInput(nref, nrows, ncolumn, dref, dcolumn,
                         denominator_value, result_column, percentage)


def aligned_input(references):
    """Align same-grain metric datasets on unique codes, not display names."""
    source, first = references[0]
    columns, rows = list(source.columns), [dict(row) for row in first]
    for reference, other_rows in references[1:]:
        common = set(columns).intersection(reference.columns)
        keys = [name for name in columns if name in common and any(
            tag in str(name).lower() for tag in ('编码', '编号', '_code', '_id', '（id）', '(id)'))]
        if not keys:
            raise ValueError('多份统计数据缺少共同的唯一编号，不能按姓名或行顺序对齐')
        def index(data):
            result = {}
            for row in data:
                key = tuple(row.get(column) for column in keys)
                if None in key or key in result:
                    raise ValueError('输入数据不是唯一编号分组，不能执行隐式多对多合并')
                result[key] = row
            return result
        left, right = index(rows), index(other_rows)
        if left.keys() != right.keys():
            raise ValueError('计算输入的对象范围不一致，不能默默丢弃或补零')
        merged = []
        for key, row in left.items():
            extra = right[key]
            if any(row.get(column) != extra.get(column) for column in common):
                raise ValueError('相同编号的共享属性不一致，需要核对统计范围')
            merged.append({**row, **extra})
        rows = merged
        columns.extend(column for column in reference.columns if column not in columns)
    visible = [name for name in columns if not str(name).startswith('_')]
    return visible, [{name: row.get(name) for name in visible} for row in rows]


def computation_material(question: str, reference, rows, operation: dict[str, Any], render):
    columns = [name for name in reference.columns if not str(name).startswith('_')]
    data = [{name: row.get(name) for name in columns} for row in rows]
    return {'question':question, 'intent':'统计分析',
        'summary':f'计算、筛选已完成，共 {len(data)} 条结果。', 'warnings':[],
        'presentation':{'table':render(columns, data[:20]), 'chart':'',
            'notes':([f'完整计算结果共 {len(data)} 条，当前展示前20条。'] if len(data)>20 else [])},
        'facts':{'computation':{'operation':operation,'row_count':len(data),'top':data[:10]},
            'delivery_operation':operation,
            'query_data':{'columns':columns,'rows':data,'returned_row_count':len(data),
                'total_row_count':len(data),'total_row_count_confirmed':True,'sample_only':False}}}
