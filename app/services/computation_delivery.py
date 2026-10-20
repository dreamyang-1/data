"""Small, deterministic delivery helpers; no narrative may stand in for data."""
from typing import Any
from decimal import Decimal, InvalidOperation


def numeric_value(value):
    if value is None or isinstance(value, bool):
        raise ValueError('计算输入不是有效数值')
    try:
        result = Decimal(str(value).replace(',', ''))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('计算输入不是有效数值') from exc
    if not result.is_finite():
        raise ValueError('计算输入必须是有限数值')
    return result


def aligned_input(references):
    """Align same-grain metric datasets on unique codes, not display names."""
    source, first = references[0]
    columns, rows = list(source.columns), [dict(row) for row in first]
    for reference, other_rows in references[1:]:
        def bindings(ref):
            return next((item.get('column_bindings', {}) for item in getattr(ref, 'transformation_log', ())
                         if item.get('type') == 'query_provenance'), {})
        left_bindings, right_bindings = bindings(source), bindings(reference)
        # The generator may label the same proven code as “业务员” in one
        # result and “业务员（编码）” in another. Match the executed SQL field,
        # never a display name or a guessed semantic alias.
        renames = {}
        for left_name, physical in left_bindings.items():
            if left_name not in columns or not str(physical).lower().endswith(('_code', '_id')):
                continue
            matches = [name for name, field in right_bindings.items() if field == physical and name in reference.columns]
            if len(matches) == 1 and matches[0] != left_name:
                renames[matches[0]] = left_name
        right_columns = list(dict.fromkeys(renames.get(name, name) for name in reference.columns))
        if renames:
            renamed_rows = []
            for original in other_rows:
                extra = dict(original)
                for old, new in renames.items():
                    if new in extra and extra[new] != extra[old]:
                        raise ValueError('结果字段别名冲突，不能对齐计算输入')
                    extra[new] = extra.pop(old)
                renamed_rows.append(extra)
            other_rows = renamed_rows
            right_bindings = {renames.get(name, name): field for name, field in right_bindings.items()}
        common = set(columns).intersection(right_columns)
        def proven_identity(name):
            field = left_bindings.get(name)
            return (isinstance(field, str) and field == right_bindings.get(name)
                    and field.lower().endswith(('_code', '_id')))
        keys = [name for name in columns if name in common and any(
            tag in str(name).lower() for tag in ('编码', '编号', '_code', '_id', '（id）', '(id)'))
            or name in common and proven_identity(name)]
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
        columns.extend(column for column in right_columns if column not in columns)
    visible = [name for name in columns if not str(name).startswith('_')]
    return visible, [{name: row.get(name) for name in visible} for row in rows]


def computation_material(question: str, reference, rows, operation: dict[str, Any], render, *, columns=None):
    columns = [name for name in (columns if columns is not None else reference.columns) if not str(name).startswith('_')]
    data = [{name: row.get(name) for name in columns} for row in rows]
    return {'question':question, 'intent':'统计分析',
        'summary':f'计算、筛选已完成，共 {len(data)} 条结果。', 'warnings':[],
        'presentation':{'table':render(columns, data[:20]), 'chart':'',
            'notes':([f'完整计算结果共 {len(data)} 条，当前展示前20条。'] if len(data)>20 else [])},
        'facts':{'computation':{'operation':operation,'row_count':len(data),'top':data[:10]},
            'delivery_operation':operation,
            'query_data':{'columns':columns,'rows':data,'returned_row_count':len(data),
                'total_row_count':len(data),'total_row_count_confirmed':True,'sample_only':False}}}
