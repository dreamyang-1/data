"""Compile declared catalog metric thresholds to bounded dataset filters."""
from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import math
import re


OPERATORS = {'>':'gt', '>=':'gte', '<':'lt', '<=':'lte', '=':'eq', '!=':'ne'}


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
    else:
        scales = {'元':1,'万元':10_000,'亿元':100_000_000}
        suffix={'万':'万元','亿':'亿元'}.get(suffix,suffix)
        if unit not in scales or suffix not in scales:
            raise ValueError(f'指标阈值单位与目录单位{unit or "未登记"}不一致，不能自动换算')
        factor = Decimal(scales[suffix])/Decimal(scales[unit])
    value = number * factor
    return int(value) if value == value.to_integral_value() else float(value)


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
