"""Compile planner time slots. The binding model never supplies dates or grain."""
import calendar
import json
import re
from datetime import date, timedelta


UNITS = {
    'hour': 'hour', 'day': 'day', 'week': 'week', 'month': 'month',
    'quarter': 'quarter', 'year': 'year', '时': 'hour', '小时': 'hour',
    '日': 'day', '天': 'day', '周': 'week', '星期': 'week', '月': 'month',
    '季': 'quarter', '季度': 'quarter', '年': 'year',
}


def normalize_unit(value):
    return UNITS.get(str(value).strip().casefold()) if value is not None else None


def _object(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def is_temporal(metadata):
    metadata = metadata or {}
    if _object(metadata.get('granularity_support')):
        return True
    return bool(re.search(r'日期|时间|(?:^|[\W_])(?:date|time|datetime|timestamp)(?:$|[\W_])', ' '.join(str(metadata.get(k) or '') for k in
        ('data_type', 'dim_type', 'dim_code', 'dim_name', 'attr_code', 'attr_name')), re.I))


def _physical(value):
    value = _object(value)
    if isinstance(value, dict):
        table, column = value.get('mappingTable'), value.get('mappingColumn')
        return f'{table}.{column}' if table and column else None
    return value if isinstance(value, str) and '.' in value else None


def dimension_fields(meta):
    result = set()
    mapping = _physical(meta.get('field_mapping'))
    if mapping:
        result.add(mapping)
    bindings = _object(meta.get('bind_entities')) or []
    for item in bindings if isinstance(bindings, list) else []:
        if isinstance(item, dict) and item.get('mappingTable') and item.get('mappingColumn'):
            result.add(item['mappingTable'] + '.' + item['mappingColumn'])
    return result


def grouping_grain(temporal, original, key, target, ast, catalog):
    """A range unit alone cannot add a GROUP BY or truncate a detail date."""
    if target != 'dimensions' or not ast['metrics']:
        return None
    meta = catalog['dimensions'].get(key) or catalog['fields'].get(key) or {}
    if not is_temporal(meta):
        return None
    value = original.get('granularity') if isinstance(original, dict) else None
    if value is None:
        value = temporal.get('unit') if isinstance(temporal, dict) else None
    if value is not None and normalize_unit(value) is None:
        raise ValueError('结构化参数的时间分组单位不受支持，请明确小时、日、周、月、季度或年')
    return normalize_unit(value)


def _number(text):
    if text.isdigit():
        return int(text)
    digits = dict(zip('零一二三四五六七八九', range(10)))
    digits['两'] = 2
    if text in digits:
        return digits[text]
    if text.count('十') == 1:
        left, right = text.split('十')
        if (not left or left in digits) and (not right or right in digits):
            return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    raise ValueError('时间数量无法确定，请明确时间范围')


def _month_bounds(year, month, count=1):
    start = date(year, month, 1)
    y, m = divmod(year * 12 + month - 1 + count - 1, 12)
    return start, date(y, m + 1, calendar.monthrange(y, m + 1)[1])


def _period(text, today):
    """Full-match a single declared period, never search the original question."""
    text = re.sub(r'\s+', '', text)
    quarter = re.fullmatch(r'(\d{4})(?:年?第?([一二三四1-4])季度?|[-年]?[Qq]([1-4]))', text)
    if quarter:
        return _month_bounds(int(quarter[1]), (_number(quarter[2] or quarter[3]) - 1) * 3 + 1, 3)
    half = re.fullmatch(r'(\d{4})年?([上下])半年', text)
    if half:
        return _month_bounds(int(half[1]), 1 if half[2] == '上' else 7, 6)
    absolute = re.fullmatch(r'(\d{4})(?:年|[-/])(?:(\d{1,2})(?:月|[-/])?(?:(\d{1,2})日?)?)?', text)
    if re.fullmatch(r'\d{4}', text):
        return _month_bounds(int(text), 1, 12)
    if absolute:
        year, month, day = int(absolute[1]), absolute[2], absolute[3]
        if day:
            point = date(year, int(month), int(day))
            return point, point
        return _month_bounds(year, int(month) if month else 1, 1 if month else 12)
    if text in {'今天', '今日', 'today', '昨天', '昨日', 'yesterday'}:
        point = today - timedelta(days=int(text in {'昨天', '昨日', 'yesterday'}))
        return point, point
    period = re.fullmatch(r'(本|这|上|下|今|去|明)(?:个)?(年|月|周|星期|季度|季)', text)
    aliases = {'今年': (0, 'year'), '去年': (-1, 'year'), '明年': (1, 'year'),
               'this_year': (0, 'year'), 'last_year': (-1, 'year'),
               'this_month': (0, 'month'), 'last_month': (-1, 'month'),
               'this_quarter': (0, 'quarter'), 'last_quarter': (-1, 'quarter'),
               'this_week': (0, 'week'), 'last_week': (-1, 'week')}
    relative = aliases.get(text)
    if period:
        relative = (-1 if period[1] in {'上', '去'} else 1 if period[1] in {'下', '明'} else 0, normalize_unit(period[2]))
    if relative:
        offset, unit = relative
        if unit == 'week':
            start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            return start, start + timedelta(days=6)
        width = {'month': 1, 'quarter': 3, 'year': 12}[unit]
        month = (today.month - 1) // width * width + 1
        year, m = divmod(today.year * 12 + month - 1 + width * offset, 12)
        return _month_bounds(year, m + 1, width)
    rolling = re.fullmatch(r'(?:最近|近|过去)([\d一二两三四五六七八九十]+)(?:个)?(年|月|周|星期|季度|季|天|日)', text)
    if text in {'近半年', '最近半年', '过去半年'}:
        amount, unit = 6, 'month'
    elif rolling:
        amount, unit = _number(rolling[1]), normalize_unit(rolling[2])
    else:
        raise ValueError('时间范围尚未转换为明确日期，请在任务规划中明确起止日期；不会使用模型猜测的范围')
    if not 1 <= amount <= 3660:
        raise ValueError('时间数量超出支持范围')
    if unit in {'month', 'quarter', 'year'}:
        months = amount * {'month': 1, 'quarter': 3, 'year': 12}[unit]
        year, m = divmod(today.year * 12 + today.month - 1 - months, 12)
        start = date(year, m + 1, min(today.day, calendar.monthrange(year, m + 1)[1]))
    else:
        start = today - timedelta(days=amount * (7 if unit == 'week' else 1))
    return start, today


def bounds(value, today):
    if isinstance(value, dict):
        if not value.get('start') or not any(value.get(k) for k in ('end', 'end_inclusive', 'end_exclusive')):
            raise ValueError('结构化时间范围缺少明确的起止日期，请检查任务规划中的时间参数')
        start = date.fromisoformat(str(value['start']))
        if value.get('end_exclusive'):
            end = date.fromisoformat(str(value['end_exclusive'])) - timedelta(days=1)
        else:
            end = date.fromisoformat(str(value.get('end') or value['end_inclusive']))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = _period(str(value[0]), today)[0], _period(str(value[1]), today)[1]
    elif isinstance(value, str):
        parts = re.split(r'\s*(?:至|到|[~～]|\s+-\s+)\s*', value.strip())
        if len(parts) == 2:
            start, end = _period(parts[0], today)[0], _period(parts[1], today)[1]
        elif len(parts) == 1:
            start, end = _period(parts[0], today)
        else:
            raise ValueError('多个不连续时间区间需要在任务规划中明确拆分')
    else:
        raise ValueError('结构化时间范围格式不完整')
    if end < start:
        raise ValueError('时间范围结束日期早于开始日期')
    return start.isoformat(), end.isoformat()


def time_anchor(temporal, ast, catalog, knowledge, model_time):
    fields = catalog['fields']
    time_dims = {k: dimension_fields(v) & fields.keys() for k, v in catalog['dimensions'].items() if is_temporal(v)}
    typed = {k for k, v in fields.items() if is_temporal(v)} | set().union(set(), *time_dims.values())

    def resolve(label):
        if not isinstance(label, str):
            return set()
        result = {label} if label in typed else set()
        for key, meta in catalog['dimensions'].items():
            if label in {key, meta.get('dim_name')}:
                result.update(time_dims.get(key, set()))
        for key in typed:
            if label in {fields[key].get('attr_name'), fields[key].get('attr_code')}:
                result.add(key)
        return result

    explicit = temporal.get('anchor') or temporal.get('field')
    if explicit:
        candidates = resolve(explicit)
    else:
        metric_anchors = set()
        for item in ast['metrics']:
            caliber = _object(catalog['metrics'][item['name']].get('time_caliber')) or {}
            if isinstance(caliber, dict) and caliber.get('time_anchor'):
                metric_anchors.add(_physical(caliber['time_anchor']))
        if metric_anchors:
            # A declared but unavailable metric anchor must not be replaced by another date.
            candidates = metric_anchors & typed
            if candidates != metric_anchors:
                candidates = set()
        else:
            candidates = set()
            for dim in ast['dimensions']:
                candidates.update(resolve(dim['name']))
            if not candidates:
                tables = {d['name'].split('.')[0] for d in ast['dimensions'] if '.' in d['name']}
                tables.update(f['field'].split('.')[0] for f in ast['filters'])
                subject = catalog['entities'].get((ast.get('subject') or {}).get('entity'), {})
                for attr in _object(subject.get('attributes')) or []:
                    field = _physical(attr.get('field_mapping'))
                    if field:
                        tables.add(field.split('.')[0])
                relations = [getattr(r, 'metadata', {}) for r in knowledge.get('relations', [])]
                for meta in catalog['entities'].values():
                    relations.extend(_object(meta.get('relations')) or [])
                edges = []
                for rel in relations:
                    join = _object(rel.get('join_key')) or {}
                    if isinstance(join, dict):
                        pair = [_physical(join.get(k)) for k in ('source_field', 'target_field')]
                        if all(pair):
                            edges.append([f.split('.')[0] for f in pair])
                for _ in range(len(edges)):
                    for a, b in edges:
                        if a in tables or b in tables:
                            tables.update((a, b))
                reachable = {f for f in typed if f.split('.')[0] in tables}
                published = set().union(set(), *time_dims.values()) & reachable
                candidates = published or reachable
    if len(candidates) == 1:
        return next(iter(candidates))
    # Model can disambiguate a catalog field, not create or change a time value.
    selected = resolve(model_time.get('anchor')) & candidates
    if not explicit and len(selected) == 1 and not ast['metrics']:
        return next(iter(selected))
    if candidates:
        raise ValueError('存在多个可用时间字段，请明确按哪种业务日期筛选；起止日期已经明确，无需重复提供')
    raise ValueError('当前查询的授权语义目录缺少可关联的时间字段，请核对时间维度及关联配置；时间范围已经明确，无需重新提供')


def compile_time(temporal, ast, catalog, knowledge, model_time, today):
    if not isinstance(temporal, dict):
        raise ValueError('时间参数格式不完整')
    value = temporal.get('time_range')
    if value is None or value == '' or value in ('全部时间', '不限时间', '全部', 'all_time'):
        return None
    start, end = bounds(value, today)
    anchor = time_anchor(temporal, ast, catalog, knowledge, model_time if isinstance(model_time, dict) else {})
    return {'type': 'range', 'start': start, 'end': end, 'unit': 'day', 'anchor': anchor}
