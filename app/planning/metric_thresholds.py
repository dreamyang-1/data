"""Compile declared catalog metric thresholds to bounded dataset filters."""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import math
import re


OPERATORS = {'>':'gt', '>=':'gte', '<':'lt', '<=':'lte', '=':'eq', '!=':'ne'}

# 语义目录里时间型指标的单位折算基准。语义文件只登记单位，不登记进制，
# 这里按通用折算：一周7天、一月30天、一季度90天、半年180天、一年365天。
_TIME_UNIT_DAYS = {
    '天': 1, '日': 1, '周': 7, '星期': 7,
    '月': 30, '个月': 30, '季度': 90, '季': 90,
    '半年': 180, '半月': 15, '年': 365,
}
# 金额数量级后缀与基础单位的折算系数
_AMOUNT_SCALES = {
    '元': 1, '千元': 1_000, '万元': 10_000,
    '百万元': 1_000_000, '千万元': 10_000_000, '亿元': 100_000_000,
}
_AMOUNT_SUFFIX = {
    '千': '千元', '万': '万元', '百万': '百万元',
    '千万': '千万元', '亿': '亿元',
}

# 相对时间表述：近三个月 / 最近1年 / 六个月前。绝对期间（2025年10月）
# 不属于相对表述，不做换算。
_RELATIVE_WINDOW = re.compile(
    r'(最近|近|过去)\s*([\d一二两三四五六七八九十]+)\s*个?\s*'
    r'(半月|半年|季度|季|星期|月|周|天|日|年)'
)
_RELATIVE_POINT = re.compile(
    r'([\d一二两三四五六七八九十]+)\s*个?\s*'
    r'(半月|半年|季度|季|星期|月|周|天|日|年)\s*(?:之前|以前|前)'
)
_CN_DIGITS = {
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _cn_number(text: str) -> int | None:
    text = str(text or '').strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text.count('十') == 1:
        left, right = text.split('十')
        if (not left or left in _CN_DIGITS) and (not right or right in _CN_DIGITS):
            return (_CN_DIGITS.get(left, 1) if left else 1) * 10 + (
                _CN_DIGITS.get(right, 0) if right else 0
            )
    return None



def metric_units(spec: str) -> dict[str, str]:
    result = {}
    columns = None
    for line in spec.splitlines():
        if not line.strip().startswith('|'):
            columns = None
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if set(''.join(cells)) <= {'-',':',' '}:
            continue
        if cells[0] in {'指标','指标名称'}:
            columns = cells
        elif columns and '单位' in columns and len(cells) > columns.index('单位'):
            result[cells[0]] = cells[columns.index('单位')]
    return result


def numeric_threshold(raw, unit: str = ''):
    """Units come from the scoped catalog, never a guessed month/day length."""
    if isinstance(raw, bool):
        raise ValueError('指标阈值必须为数值')
    if isinstance(raw, (int,float)):
        if not math.isfinite(raw):
            raise ValueError('指标阈值必须为有限数值')
        return raw
    match = re.fullmatch(r'\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(.*?)\s*', str(raw))
    if not match:
        raise ValueError('指标阈值未能绑定为数值，请在规划中明确数值和目录单位')
    number, suffix = Decimal(match[1]), match[2].replace('个','')
    unit = str(unit or '').strip()
    if not suffix or suffix == unit:
        factor = Decimal(1)
    elif unit in {'月','个月'} and suffix == '年':
        factor = Decimal(12)
    elif unit in {'年'} and suffix == '月':
        factor = Decimal(1)/Decimal(12)
    elif unit in _TIME_UNIT_DAYS and suffix in _TIME_UNIT_DAYS:
        # 时间型指标的阈值按目录单位折算：单位天时“三个月”记为90
        factor = Decimal(_TIME_UNIT_DAYS[suffix]) / Decimal(_TIME_UNIT_DAYS[unit])
    else:
        suffix = _AMOUNT_SUFFIX.get(suffix, suffix)
        if unit not in _AMOUNT_SCALES or suffix not in _AMOUNT_SCALES:
            raise ValueError(f'指标阈值单位与目录单位{unit or "未登记"}不一致，不能自动换算')
        factor = Decimal(_AMOUNT_SCALES[suffix])/Decimal(_AMOUNT_SCALES[unit])
    value = number * factor
    return int(value) if value == value.to_integral_value() else float(value)


def _convert_relative_time(text: str, target_unit: str) -> str:
    """把相对时间表述折算成指标单位；无法整除或同单位时保留原表述。"""
    if target_unit not in _TIME_UNIT_DAYS:
        return text
    target_days = _TIME_UNIT_DAYS[target_unit]

    def convert(amount_text: str, unit_word: str) -> int | None:
        if unit_word == target_unit or unit_word not in _TIME_UNIT_DAYS:
            return None
        amount = _cn_number(amount_text)
        if amount is None or amount <= 0:
            return None
        value = Decimal(amount * _TIME_UNIT_DAYS[unit_word]) / Decimal(target_days)
        if value != value.to_integral_value():
            return None
        return int(value)

    def window_repl(match):
        value = convert(match.group(2), match.group(3))
        if value is None:
            return match.group(0)
        return f'{match.group(1)}{value}{target_unit}'

    def point_repl(match):
        value = convert(match.group(1), match.group(2))
        if value is None:
            return match.group(0)
        return f'{value}{target_unit}之前'

    text = _RELATIVE_WINDOW.sub(window_repl, text)
    return _RELATIVE_POINT.sub(point_repl, text)


def _time_unit_for_metrics(structured: dict, units: dict[str, str]) -> str | None:
    """本任务指标通道里唯一的时间型单位；多个不同单位时不猜。"""
    found = set()
    for item in structured.get('指标') or []:
        name = str(item.get('name') if isinstance(item, dict) else item or '').strip()
        unit = units.get(name)
        if unit in _TIME_UNIT_DAYS:
            found.add(unit)
    return found.pop() if len(found) == 1 else None


def normalize_metric_units(structured: dict | None, units: dict[str, str] | None) -> None:
    """按语义目录登记的单位归一结构化提取里的阈值与相对时间表述。

    金额：目录单位为元时“100万”记为1000000；时间：目录单位为天时
    “近三个月”记为“近90天”、“六个月前”记为“180天之前”。只处理目录
    里有单位登记的指标，换算不了的原样保留。
    """
    if not isinstance(structured, dict) or not units:
        return
    for condition in structured.get('过滤条件') or []:
        if not isinstance(condition, dict):
            continue
        field = str(condition.get('field') or '').strip()
        unit = units.get(field)
        if not unit:
            continue
        values = condition.get('value')
        single = not isinstance(values, list)
        items = values if isinstance(values, list) else [values]
        converted = []
        for item in items:
            try:
                converted.append(numeric_threshold(item, unit))
            except (ValueError, InvalidOperation, TypeError):
                converted.append(item)
        condition['value'] = converted[0] if single else converted
    granularity = structured.get('时间粒度')
    if not isinstance(granularity, dict):
        return
    target_unit = _time_unit_for_metrics(structured, units)
    if target_unit is None:
        return
    for key in ('time_range', 'anchor'):
        text = granularity.get(key)
        if isinstance(text, str) and text.strip():
            granularity[key] = _convert_relative_time(text, target_unit)


def compile_metric_thresholds(decomposition):
    from app.domain.models import PrimaryIntent
    from app.planning.dependency_contract import projection
    from app.planning.task_dag import database_query_choice, extraction_to_parameters, TaskInputBindingError
    if not decomposition.metric_catalog:
        return decomposition
    registered, aliases = decomposition.metric_catalog
    units = decomposition.metric_units or {}
    compiled, terminals = [], {}
    for old_index, original in enumerate(decomposition.tasks):
        if any(type(dep) is not int or dep not in terminals for dep in original.depends_on):
            raise TaskInputBindingError('任务依赖必须引用本问题中已经规划的前序任务')
        task = replace(original, depends_on=tuple(terminals[dep] for dep in original.depends_on),
                       structured=copy.deepcopy(original.structured))
        structured = task.structured or {}
        existing = structured.get('结果操作')
        if isinstance(existing,dict) and existing.get('source_task_index') is not None:
            source = existing['source_task_index']
            if source in terminals:
                existing['source_task_index'] = terminals[source]
        conditions, others = [], []
        for condition in structured.get('过滤条件') or []:
            field = condition.get('field') if isinstance(condition,dict) else None
            name = aliases.get(field,field)
            if name in registered and str(condition.get('op') or '').upper() in OPERATORS:
                conditions.append((name,condition))
            else:
                others.append(condition)
        if not conditions or structured.get('计算待确认'):
            compiled.append(task); terminals[old_index]=len(compiled)-1
            continue
        if existing or structured.get('计算绑定'):
            raise TaskInputBindingError('指标阈值须与已有结果操作形成一个明确的运算顺序，不能重复或覆盖操作')
        steps = []
        try:
            for name,condition in conditions:
                values=condition.get('value')
                values=values if isinstance(values,list) else [values]
                if len(values)!=1:
                    raise ValueError('单个比较符必须对应一个指标阈值')
                steps.append({'type':'filter','field':name,'operator':OPERATORS[condition['op']],
                              'value':numeric_threshold(values[0],units.get(name,''))})
        except (ValueError,InvalidOperation) as exc:
            raise TaskInputBindingError(str(exc)) from exc
        operation = steps[0] if len(steps)==1 else {'type':'pipeline','operations':steps}
        limit=re.search(r'(?:前|top\s*|最多(?:返回|展示)?|只(?:返回|显示|要)(?:前)?)\s*([0-9]+|[一二两三四五六七八九十百]+)\s*(?:名|家|个|条|位)',task.question,re.I)
        if limit:
            from app.services.dataset_followup import _ranking_count
            count=_ranking_count(limit[1])
            if count>0:
                steps.append({'type':'limit','count':count})
                operation={'type':'pipeline','operations':steps}
        local = database_query_choice(structured) is False or '不再查询数据库' in task.question
        if local:
            if len(task.depends_on)!=1:
                raise TaskInputBindingError('本地指标阈值筛选必须绑定一个明确的前置结果')
            source=task.depends_on[0]
            available={name for _,name in projection(compiled[source].structured)}
            if any(name not in available for name,_ in conditions):
                raise TaskInputBindingError('前置结果未包含本地筛选需要的指标字段')
            structured.update({'是否查询数据库':'否','过滤条件':others,
                '结果操作':{'source_task_index':source,'operation':operation}})
            compiled.append(replace(task,structured=structured))
        else:
            owners=list(dict.fromkeys(item.get('entity') for item in structured.get('展示字段') or []
                if isinstance(item,dict) and item.get('entity')))
            groups=list(structured.get('维度') or []) or owners
            if not groups:
                raise TaskInputBindingError('指标阈值名单缺少明确的对象分组，不能按明细行筛选')
            structured['指标']=list(structured.get('指标') or [])
            present={m.get('name') if isinstance(m,dict) else m for m in structured['指标']}
            for name,_ in conditions:
                if name not in present:
                    structured['指标'].append({'name':name});present.add(name)
            structured.update({'意图':'统计查询','维度':groups,'过滤条件':others,
                '限制':None,'是否查询数据库':'是'})
            source=len(compiled)
            compiled.append(replace(task, intent=PrimaryIntent.METRIC_QUERY,structured=structured,
                question=task.question+'（先返回完整指标与分组数据，阈值由后续筛选）',
                parameters=tuple(extraction_to_parameters(structured))))
            output={'意图':'指标计算','是否查询数据库':'否',
                '结果操作':{'source_task_index':source,'operation':operation}}
            compiled.append(replace(task,question='根据上述任务的查询结果筛选'+task.question+'，不再查询数据库',
                name='筛选符合指标阈值的对象',depends_on=(source,),intent=PrimaryIntent.COMPARISON_ANALYSIS,
                structured=output,parameters=()))
        terminals[old_index]=len(compiled)-1
    return replace(decomposition,tasks=tuple(compiled))
