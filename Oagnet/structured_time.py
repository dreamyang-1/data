"""Compile planner time slots. The binding model never supplies dates or grain."""
import calendar
import json
import re
from datetime import date, datetime, time, timedelta
from binding_ownership import matching_fields, matching_dimensions, parameter_catalog, field_owners


UNITS = {
    'hour': 'hour', 'day': 'day', 'week': 'week', 'month': 'month',
    'quarter': 'quarter', 'year': 'year', '时': 'hour', '小时': 'hour',
    'second': 'second', 'seconds': 'second', '秒': 'second', '秒钟': 'second',
    'minute': 'minute', 'minutes': 'minute', '分': 'minute', '分钟': 'minute',
    'hours': 'hour', 'days': 'day', 'weeks': 'week', 'months': 'month',
    'quarters': 'quarter', 'years': 'year', '月份': 'month', '年度': 'year',
    '自然年': 'year', '自然月': 'month', '自然日': 'day',
    'half_year': 'half_year', '半年': 'half_year', '半年度': 'half_year',
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
        raise ValueError('时间分组支持秒、分钟、小时、日、周、月、季度、半年和年；财年、工作日或自定义周期需提供业务口径，不能按自然周期猜测')
    if normalize_unit(value) in {'hour', 'minute', 'second'} and str(meta.get('data_type') or '').upper() == 'DATE':
        raise ValueError('所选时间字段只有日期精度，不能凭空生成小时、分钟或秒，请选择具有时分秒的时间字段')
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


def _date_period(text, today):
    """Full-match a single declared period, never search the original question."""
    text = re.sub(r'\s+', '', text).replace('月份', '月').replace('年份', '年').replace('号', '日')
    for label, offset in [('今年', 0), ('去年', -1), ('明年', 1)]:
        if text.startswith(label) and text != label:
            text = str(today.year + offset) + '年' + text[len(label):]
            break
    week = re.fullmatch(r'(\d{4})(?:年?第?([\d一二三四五六七八九十]+)(?:周|星期)|-?[Ww](\d{1,2}))', text)
    if week:
        start = date.fromisocalendar(int(week[1]), _number(week[2] or week[3]), 1)
        return start, start + timedelta(days=6)
    quarter = re.fullmatch(r'(\d{4})(?:年?第?([一二三四1-4])季度?|[-年]?[Qq]([1-4]))', text)
    if quarter:
        return _month_bounds(int(quarter[1]), (_number(quarter[2] or quarter[3]) - 1) * 3 + 1, 3)
    half = re.fullmatch(r'(?:(\d{4})年?)?([上下])半年(?:度)?', text)
    if half:
        return _month_bounds(int(half[1]) if half[1] else today.year, 1 if half[2] == '上' else 7, 6)
    half = re.fullmatch(r'(\d{4})[-年]?[Hh]([12])', text)
    if half:
        return _month_bounds(int(half[1]), 1 if half[2] == '1' else 7, 6)
    full_year = re.fullmatch(r'(\d{4})年(?:全年|度|全年度)', text)
    if full_year:
        return _month_bounds(int(full_year[1]), 1, 12)
    absolute = re.fullmatch(r'(\d{4})(?:年|[-/])(?:(\d{1,2})(?:月|[-/])?(?:(\d{1,2})日?)?)?', text)
    if re.fullmatch(r'\d{4}', text):
        return _month_bounds(int(text), 1, 12)
    if absolute:
        year, month, day = int(absolute[1]), absolute[2], absolute[3]
        if day:
            point = date(year, int(month), int(day))
            return point, point
        return _month_bounds(year, int(month) if month else 1, 1 if month else 12)
    days = {'今天': 0, '今日': 0, 'today': 0, '昨天': -1, '昨日': -1,
            'yesterday': -1, '明天': 1, '明日': 1, 'tomorrow': 1, '前天': -2, '后天': 2}
    if text in days:
        point = today + timedelta(days=days[text])
        return point, point
    period = re.fullmatch(r'(本|这|上|下|今|去|明)(?:一个|一|个)?(半年度|半年|年度|年|月|周|星期|季度|季)', text)
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
        width = {'month': 1, 'quarter': 3, 'half_year': 6, 'year': 12}[unit]
        month = (today.month - 1) // width * width + 1
        year, m = divmod(today.year * 12 + month - 1 + width * offset, 12)
        return _month_bounds(year, m + 1, width)
    rolling = re.fullmatch(r'(?:最近|近|过去)([\d一二两三四五六七八九十]+)(?:个)?(半年|年|月|周|星期|季度|季|天|日)', text)
    if text in {'近半年', '最近半年', '过去半年'}:
        amount, unit = 6, 'month'
    elif rolling:
        amount, unit = _number(rolling[1]), normalize_unit(rolling[2])
    else:
        raise ValueError('时间范围尚未转换为明确日期，请在任务规划中明确起止日期；不会使用模型猜测的范围')
    if not 1 <= amount <= 3660:
        raise ValueError('时间数量超出支持范围')
    if unit in {'month', 'quarter', 'half_year', 'year'}:
        months = amount * {'month': 1, 'quarter': 3, 'half_year': 6, 'year': 12}[unit]
        year, m = divmod(today.year * 12 + today.month - 1 - months, 12)
        start = date(year, m + 1, min(today.day, calendar.monthrange(year, m + 1)[1]))
    else:
        start = today - timedelta(days=amount * (7 if unit == 'week' else 1))
    return start, today


def _clock(today):
    if not isinstance(today, datetime):
        raise ValueError('小时、分钟或秒的相对范围需要当前时刻，不能使用日期零点或模型猜测的时刻')
    if today.tzinfo is not None:
        raise ValueError('带时区的时刻需先按数据源已配置的业务时区转换，不能自动猜测时区')
    return today.replace(microsecond=0)


def _period(text, today):
    """Expand a declared period at its own precision, never from the question."""
    text = text.strip().replace('月份', '月').replace('年份', '年')
    compact = re.sub(r'\s+', '', text)
    if '财年' in compact or '工作日' in compact:
        raise ValueError('财年或工作日依赖业务日历，请明确起止时间或配置对应业务口径；不会按自然年或自然日替代')
    base_date = today.date() if isinstance(today, datetime) else today
    for label, offset in [('今天', 0), ('今日', 0), ('昨天', -1), ('明天', 1)]:
        if text.startswith(label) and text != label:
            text = (base_date + timedelta(days=offset)).isoformat() + ' ' + text[len(label):].strip()
            break
    fine_aliases = {f'{prefix}_{unit}': label + cn for prefix, label in
                    [('this', '本'), ('last', '上'), ('next', '下')] for unit, cn in
                    [('hour', '小时'), ('minute', '分钟'), ('second', '秒')]}
    compact = fine_aliases.get(compact, compact)
    fine_relative = re.fullmatch(r'(本|这|当前|上|下)(?:一个|一|个)?(小时|时|分钟|分|秒钟|秒)', compact)
    fine_rolling = re.fullmatch(r'(?:最近|近|过去)([\d一二两三四五六七八九十]+)(?:个)?(小时|时|分钟|分|秒钟|秒)', compact)
    if fine_relative or fine_rolling:
        clock = _clock(today)
        match = fine_relative or fine_rolling
        unit = normalize_unit(match[2])
        width = {'hour': 3600, 'minute': 60, 'second': 1}[unit]
        if fine_rolling:
            amount = _number(match[1])
            if not 1 <= amount <= 3660:
                raise ValueError('时间数量超出支持范围')
            # Existing range ends are inclusive. Represent a rolling [start,
            # now) window as closed second bounds; SQL restores the exact upper
            # exclusive boundary without including an extra second.
            return clock - timedelta(seconds=amount * width), clock - timedelta(seconds=1)
        floor = clock.replace(minute=0, second=0) if unit == 'hour' else clock.replace(second=0) if unit == 'minute' else clock
        offset = -1 if match[1] == '上' else 1 if match[1] == '下' else 0
        start = floor + timedelta(seconds=offset * width)
        return start, start + timedelta(seconds=width - 1)
    precise = re.fullmatch(
        r'(\d{4})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|号)?[T\s]*'
        r'(\d{1,2})(?:[:：时点](\d{1,2})(?:[:：分](\d{1,2})(?:秒)?)?(?:分)?|(?:时|点))', text)
    if precise:
        year, month, day, hour = map(int, precise.group(1, 2, 3, 4))
        minute, second = precise[5], precise[6]
        start = datetime(year, month, day, hour, int(minute or 0), int(second or 0))
        width = 1 if second is not None else 60 if minute is not None else 3600
        return start, start + timedelta(seconds=width - 1)
    # ISO hour (without a trailing colon/Chinese unit).
    hour_only = re.fullmatch(r'(\d{4}-\d{2}-\d{2})[T\s]+(\d{1,2})', text)
    if hour_only:
        start = datetime.combine(date.fromisoformat(hour_only[1]), time(int(hour_only[2])))
        return start, start + timedelta(hours=1, seconds=-1)
    return _date_period(text, base_date)


def _compatible_pair(start, end):
    if isinstance(start, datetime) or isinstance(end, datetime):
        start = start if isinstance(start, datetime) else datetime.combine(start, time())
        end = end if isinstance(end, datetime) else datetime.combine(end, time(23, 59, 59))
    return start, end


def validate_boundaries(start, end):
    """Validate canonical ASL endpoints without timezone/precision guessing."""
    def parse(text):
        if not isinstance(text, str):
            raise ValueError('时间边界必须是ISO日期或精确到秒的日期时间')
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            return date.fromisoformat(text)
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', text):
            return datetime.fromisoformat(text)
        raise ValueError('时间边界只支持YYYY-MM-DD或YYYY-MM-DD HH:MM:SS，不猜测时区或更细精度')
    start, end = parse(start), parse(end)
    if type(start) is not type(end):
        raise ValueError('时间边界精度不一致')
    if end < start:
        raise ValueError('时间范围结束日期早于开始日期')
    return start, end


_RANGE_YEAR = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')


def _inherit_range_year(parts):
    """区间尾段省略年份时沿用首段年份。

    “2025年10月到12月”是常见中文简写，尾段“12月”不带年份时按首段
    年份补全后再解析。尾段自带年份、或首段也没有年份（两端都无从
    判断所属年份）时原样返回，不猜测年份。
    """
    head, tail = parts
    if not tail or _RANGE_YEAR.search(tail):
        return parts
    year = _RANGE_YEAR.search(head)
    if year is None:
        return parts
    return head, f"{year.group(1)}年{tail.lstrip()}"


def bounds(value, today):
    if isinstance(value, dict):
        if not value.get('start') or not any(value.get(k) for k in ('end', 'end_inclusive', 'end_exclusive')):
            raise ValueError('结构化时间范围缺少明确的起止日期，请检查任务规划中的时间参数')
        start = _period(str(value['start']), today)[0]
        if value.get('end_exclusive'):
            upper = _period(str(value['end_exclusive']), today)[0]
            if isinstance(start, datetime) or isinstance(upper, datetime):
                upper = upper if isinstance(upper, datetime) else datetime.combine(upper, time())
                end = upper - timedelta(seconds=1)
            else:
                end = upper - timedelta(days=1)
        else:
            end = _period(str(value.get('end') or value['end_inclusive']), today)[1]
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = _period(str(value[0]), today)[0], _period(str(value[1]), today)[1]
    elif isinstance(value, str):
        parts = re.split(r'\s*(?:至|到|[~～]|\s+-\s+)\s*', value.strip())
        if len(parts) == 2:
            parts = _inherit_range_year(parts)
            start, end = _period(parts[0], today)[0], _period(parts[1], today)[1]
        elif len(parts) == 1:
            start, end = _period(parts[0], today)
        else:
            raise ValueError('多个不连续时间区间需要在任务规划中明确拆分')
    else:
        raise ValueError('结构化时间范围格式不完整')
    start, end = _compatible_pair(start, end)
    if end < start:
        raise ValueError('时间范围结束日期早于开始日期')
    return (start.isoformat(sep=' ', timespec='seconds'), end.isoformat(sep=' ', timespec='seconds')) if isinstance(start, datetime) else (start.isoformat(), end.isoformat())


def time_anchor(temporal, ast, catalog, knowledge, model_time):
    explicit = temporal.get('anchor') or temporal.get('field')
    original = {'field': explicit}
    if temporal.get('entity'):
        original['entity'] = temporal['entity']
    scoped = parameter_catalog(catalog, original, {}, 'time')
    if scoped.get('_parameter_owner_error') or scoped.get('_parameter_owner_candidates'):
        raise ValueError('时间字段的归属实体未在当前授权目录中唯一匹配')
    catalog = scoped
    fields = catalog['fields']
    time_dims = {k: dimension_fields(v) & fields.keys() for k, v in catalog['dimensions'].items() if is_temporal(v)}
    typed = {k for k, v in fields.items() if is_temporal(v)} | set().union(set(), *time_dims.values())

    def resolve(label):
        if not isinstance(label, str):
            return set()
        result = set(matching_fields(label, catalog, aliases=False)
                     or matching_fields(label, catalog)) & typed
        for key in matching_dimensions(label, catalog):
            result.update(time_dims.get(key, set()))
        return result

    if explicit:
        candidates = resolve(explicit)
        # Retain explicit business meaning; use already-bound evidence only
        # to distinguish homonyms, never to substitute another date concept.
        if len(candidates) > 1:
            selected_fields = set()
            for dim in ast.get('dimensions', []):
                definition = catalog['dimensions'].get(dim['name'], {})
                bindings = _object(definition.get('bind_entities')) or []
                if dim.get('attr') is not None:
                    bindings = [b for b in bindings if str(b.get('attr')) == str(dim['attr'])]
                    selected_fields.update(dimension_fields({'bind_entities': bindings}))
                else:
                    selected_fields.update(dimension_fields(definition))
                if dim['name'] in typed:
                    selected_fields.add(dim['name'])
            evidence = candidates & selected_fields
            if evidence:
                candidates = evidence
            metric_fields = set()
            for item in ast.get('metrics', []):
                caliber = _object(catalog['metrics'].get(item['name'], {}).get('time_caliber')) or {}
                if isinstance(caliber, dict) and caliber.get('time_anchor'):
                    metric_fields.add(_physical(caliber['time_anchor']))
            if len(candidates) > 1 and candidates & metric_fields:
                candidates &= metric_fields
            if len(candidates) > 1:
                subject = (ast.get('subject') or {}).get('entity')
                owned = {field for field in candidates if subject in field_owners(fields[field])}
                if owned:
                    candidates = owned
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
    if ' ' in start and str(catalog['fields'].get(anchor, {}).get('data_type') or '').upper() == 'DATE':
        raise ValueError(f'时间字段【{anchor}】的数据类型为DATE，只有日期精度；秒、分钟或小时范围需映射具有时分秒的时间字段')
    return {'type': 'range', 'start': start, 'end': end, 'unit': 'second' if ' ' in start else 'day', 'anchor': anchor}
