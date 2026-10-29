"""Explicit monthly growth on complete, already grouped monthly observations."""
from calendar import monthrange
from datetime import date
from decimal import Decimal
import re

from app.services.computation_delivery import numeric_value


def month_index(value):
    if isinstance(value,date):
        value=value.isoformat()
    match=re.fullmatch(r'(\d{4})-(\d{2})(?:-01(?:[T ]00:00:00(?:\+00:00)?)?)?',str(value))
    if match is None or not 1<=int(match[2])<=12 or not 1<=int(match[1])<=9999:
        raise ValueError('月份必须是明确的YYYY-MM或月初日期')
    return int(match[1])*12+int(match[2])-1


def month_label(index):
    year,month=divmod(index,12)
    return f'{year:04d}-{month+1:02d}'


def monthly_window(operation):
    start,end=month_index(operation.get('period_start')),month_index(operation.get('period_end'))
    if start>end or start<13:
        raise ValueError('月度增长计算区间无效')
    return start,end


def resolve_monthly_window(operation, wording):
    """Resolve calendar months with the existing business clock, not the LLM."""
    if operation.get('period_start') is not None or operation.get('period_end') is not None:
        monthly_window(operation)
        return dict(operation)
    from app.intent.classifier import RuleBasedIntentClassifier
    import app.intent.classifier as classifier
    complete=re.search(r'(?:近|最近)\s*(\d+)\s*个?\s*完整自然月',wording)
    if complete:
        count=int(complete[1])
        if count<1:
            raise ValueError('完整自然月数量必须为正数')
        today=classifier.date.today()
        end=today.year*12+today.month-2
        start=end-count+1
    else:
        period=RuleBasedIntentClassifier._time_range(wording)
        if period is None or period.start.day!=1 or period.end_exclusive.day!=1:
            return None
        start=period.start.year*12+period.start.month-1
        end=period.end_exclusive.year*12+period.end_exclusive.month-2
    return {**operation,'period_start':month_label(start),'period_end':month_label(end)}


def comparison_query_range(operation):
    start,end=monthly_window(operation)
    last=monthrange(end//12,end%12+1)[1]
    return f'{month_label(start-1)}-01至{month_label(end)}-{last:02d}'


def monthly_growth(columns,rows,operation):
    """Reject missing months/zero bases rather than inventing fill/exclusion rules."""
    start,end=monthly_window(operation)
    method=operation.get('method')
    if method not in {'arithmetic','compound'}:
        raise ValueError('请确认平均月度增长率采用算术平均还是复合平均')
    field,period,label=operation.get('field'),operation.get('period_field'),operation.get('result_field')
    groups=operation.get('group_by',[])
    if (not isinstance(groups,list) or len(groups)!=len(set(groups))
            or any(name not in columns for name in [field,period,*groups])
            or field==period or field in groups or period in groups
            or not isinstance(label,str) or not label.strip() or label in groups):
        raise ValueError('月度增长计算缺少明确字段、分组或输出名称')
    output_format=operation.get('format','percent')
    if output_format not in {'percent','number'}:
        raise ValueError('增长率输出格式只能是percent或number')
    grouped={}
    for row in rows:
        key=tuple(row.get(name) for name in groups)
        if any(value is None for value in key):
            raise ValueError('月度增长计算存在缺失分组身份')
        month=month_index(row.get(period))
        if not start-1<=month<=end:
            continue
        observations=grouped.setdefault(key,{})
        if month in observations:
            raise ValueError('同一分组月份重复，不能猜测应求和还是去重')
        observations[month]=numeric_value(row.get(field))
    if not grouped:
        raise ValueError('没有可计算的月度序列')
    result=[]
    count=end-start+1
    for key,observations in grouped.items():
        if start-1 not in observations:
            raise ValueError(f'缺少比较基期{month_label(start-1)}，不能计算目标区间全部环比')
        missing=[month_label(month) for month in range(start,end+1) if month not in observations]
        if missing:
            raise ValueError('缺少月份：'+','.join(missing)+'；未授权补零或跳过')
        values=[observations[month] for month in range(start-1,end+1)]
        if any(value<=0 for value in values[:-1]) or values[-1]<0:
            raise ValueError('月度增长存在非正基数或负期末值，需要确认处理口径')
        if method=='arithmetic':
            ratio=sum((current/previous-1 for previous,current in zip(values,values[1:])),Decimal(0))/count
        else:
            ratio=(values[-1]/values[0])**(Decimal(1)/count)-1
        value=f'{ratio*100:.2f}%' if output_format=='percent' else float(ratio)
        result.append({**dict(zip(groups,key)),label:value})
    return (*groups,label),result
