"""Small, deterministic delivery helpers; no narrative may stand in for data."""
from typing import Any
from decimal import Decimal, InvalidOperation
import re


def numeric_value(value):
    if value is None or isinstance(value, bool):
        raise ValueError('计算输入不是有效数值')
    text = str(value).replace(',', '')
    percent = text.endswith('%')
    if percent:
        text = text[:-1]
    if '%' in text:
        raise ValueError('计算输入不是有效数值')
    try:
        result = Decimal(text)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('计算输入不是有效数值') from exc
    if not result.is_finite():
        raise ValueError('计算输入必须是有限数值')
    return result / 100 if percent else result


def identifier_field(field):
    if not isinstance(field, str):
        return False
    # Executed-query bindings may use either salesperson_code or a bare id.
    # Alignment still compares the complete binding, including its table.
    column = field.strip().rsplit('.', 1)[-1].strip('`"[]').lower()
    return column == 'id' or column.endswith(('_code', '_id', '_no', '_number', '_key'))


def has_group_identity(reference):
    provenance = next((item for item in getattr(reference, 'transformation_log', ())
                       if item.get('type') == 'query_provenance'), {})
    bindings = provenance.get('column_bindings', {})
    return any(identifier_field(bindings.get(name)) or any(
        tag in str(name).lower() for tag in ('编码', '编号', '_code', '_id', '（id）', '(id)'))
        for name in reference.columns)


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
            if left_name not in columns or not identifier_field(physical):
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
                    and identifier_field(field))
        keys = [name for name in columns if name in common and proven_identity(name)]
        if not keys and (has_group_identity(source) or has_group_identity(reference)):
            raise ValueError('多份统计数据缺少共同的唯一编号，不能按姓名或行顺序对齐')
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


def _distribution_fields(operation):
    if operation.get('type')=='pipeline':
        return list(dict.fromkeys(field for step in operation.get('operations',[])
            for field in _distribution_fields(step)))
    if operation.get('type')=='derive' and operation.get('result_field'):
        return [operation['result_field']]
    if operation.get('type')=='aggregate':
        return [item['alias'] for item in operation.get('aggregations',[]) if item.get('alias')]
    return []


def metric_distributions(rows, fields):
    """Describe computed row samples; never substitute a ratio of totals."""
    result={}
    for field in fields:
        values=[]
        for row in rows:
            try:values.append(numeric_value(row.get(field)))
            except ValueError:pass
        if not values:
            continue
        values.sort();count=len(values);average=sum(values)/count
        def quantile(fraction):
            rank=Decimal(count-1)*fraction;left=int(rank);right=min(left+1,count-1)
            return values[left]+(values[right]-values[left])*(rank-left)
        low,high=values[0],values[-1]
        bin_count=min(5,count) if low!=high else 1
        width=(high-low)/bin_count if low!=high else Decimal(0)
        counts=[0]*bin_count
        for value in values:
            index=min(int((value-low)/width),bin_count-1) if width else 0
            counts[index]+=1
        result[field]={'sample_count':count,'missing_or_non_numeric_count':len(rows)-count,
            'mean':float(average),'median':float(quantile(Decimal('0.5'))),
            'min':float(low),'max':float(high),'q1':float(quantile(Decimal('0.25'))),
            'q3':float(quantile(Decimal('0.75'))),
            'population_stddev':float((sum((value-average)**2 for value in values)/count).sqrt()),
            'mean_method':'arithmetic_mean_of_computed_row_samples',
            'quantile_method':'linear_interpolation_at_(n-1)*p',
            'histogram_method':'equal_width_up_to_five_bins; left_closed_right_open_except_last',
            'histogram':[{'lower':float(low+width*index),'upper':float(low+width*(index+1)),
                'upper_inclusive':index==bin_count-1,'count':amount,'percentage':amount/count*100}
                for index,amount in enumerate(counts)]}
    return result


def computation_material(question: str, reference, rows, operation: dict[str, Any], render, *, columns=None):
    columns = [name for name in (columns if columns is not None else reference.columns) if not str(name).startswith('_')]
    data = [{name: row.get(name) for name in columns} for row in rows]
    material={'question':question, 'intent':'统计分析',
        'summary':f'计算、筛选已完成，共 {len(data)} 条结果。', 'warnings':[],
        'presentation':{'table':render(columns, data[:20]), 'chart':'',
            'notes':([f'完整计算结果共 {len(data)} 条，当前展示前20条。'] if len(data)>20 else [])},
        'facts':{'computation':{'operation':operation,'row_count':len(data),'top':data[:10]},
            'delivery_operation':operation,
            'query_data':{'columns':columns,'rows':data,'returned_row_count':len(data),
                'total_row_count':len(data),'total_row_count_confirmed':True,'sample_only':False}}}
    steps=operation.get('operations',[]) if operation.get('type')=='pipeline' else [operation]
    notes=[]
    for step in steps:
        for spec in step.get('aggregations',[]) if isinstance(step,dict) else []:
            function=spec.get('function','')
            if function in {'variance','stddev','variance_population','stddev_population','covariance_population'}:
                notes.append('统计使用样本口径（除以n-1）。' if spec.get('ddof')==1 else '统计使用总体口径（除以n）。')
            elif function in {'variance_sample','stddev_sample','covariance_sample'}:
                notes.append('统计使用样本口径（除以n-1）。')
            if function in {'median','quantile','percentile'}:
                notes.append('分位数采用(n-1)×q位置的线性插值。')
            if function in {'correlation','covariance_population','covariance_sample'}:
                notes.append('成对统计仅使用两列均非空的成对样本。')
    if notes:
        notes=list(dict.fromkeys(notes));material['presentation']['notes'].extend(notes)
        material['summary']+=' '+' '.join(notes)
    if re.search(r'分布|分散|离散|集中|区间',question):
        distributions=metric_distributions(data,[field for field in _distribution_fields(operation) if field in columns])
        if distributions:
            material['facts']['metric_distributions']=distributions
            summaries=[{'指标':field,'样本数':stats['sample_count'],'最小值':stats['min'],'最大值':stats['max'],
                '算术平均值':stats['mean'],'中位数':stats['median']} for field,stats in distributions.items()]
            material['presentation']['table']=render(list(summaries[0]),summaries)+'\n\n'+material['presentation']['table']
    return material
