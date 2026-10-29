"""Two-period comparisons at an explicitly planned grouping grain."""
import math

SORT_FIELDS = {'增减额': 'absolute_change', '增减量': 'absolute_change', '差额': 'absolute_change',
    '差值': 'absolute_change', '变化率': 'change_rate', '差异率': 'change_rate',
    '增长率': 'change_rate', '倍数': 'ratio', '基期100指数': 'base_100_index'}


def sort_field_key(field):
    return next((key for suffix, key in SORT_FIELDS.items() if str(field).endswith(suffix)), None)


def compare(engine, request, columns, rows):
    from app.analysis.engine import AnalysisError, AnalysisOutput
    contract = request._analysis_contract
    metric, _ = engine._series(request, columns, rows)
    identities = [col for col in columns if col.endswith('（编码）')]
    dimensions = contract['comparison_dimensions']
    if len(identities) != len(dimensions):
        raise AnalysisError('分组两期对比需要每个维度的稳定编码，不能按名称或行顺序猜测对齐')
    labels = [col[:-4] for col in identities]
    if set(labels) != set(dimensions) or any(col not in columns for col in labels):
        raise AnalysisError('分组编码缺少对应的展示名称')
    role_map = {'基期': 'base', 'BASE': 'base', '当前期': 'current', '本期': 'current', 'CURRENT': 'current'}
    periods, grouped = {}, {}
    for row in rows:
        role = role_map.get(str(row.get('period_role', '')))
        period = str(row.get('期间') or '')
        if not role or not period or periods.get(role, period) != period:
            raise AnalysisError('两期比较的期间与基期／本期角色不一致')
        periods[role] = period
        key = tuple(str(row.get(col)) for col in identities)
        if any(row.get(col) is None or str(row[col]) == '' for col in identities):
            raise AnalysisError('分组编码为空，无法对齐两期')
        item = grouped.setdefault(key, {'group_key': list(key)})
        if role in item:
            raise AnalysisError('同一期间存在重复分组编码，必须先按已声明粒度聚合')
        value = engine._number(row.get(metric))
        if value is None:
            raise AnalysisError('分组两期比较的指标必须是有限数值')
        value = float(value)
        if not math.isfinite(value):
            raise AnalysisError('分组两期比较的指标超出支持的有限数值范围')
        item[role] = value
        # Current label is preferred if a registered name changed between periods.
        if role == 'current' or 'label' not in item:
            item['label'] = '／'.join(str(row.get(col) or key[i]) for i, col in enumerate(labels))
    if set(periods) != {'base', 'current'}:
        raise AnalysisError('分组两期比较需要基期和本期两份数据')
    comparisons = []
    for item in grouped.values():
        base, current = item.get('base'), item.get('current')
        delta = None if base is None or current is None else current-base
        rate = None if delta is None or base == 0 else delta/abs(base)
        ratio = None if base in (None, 0) or current is None else current/base
        if any(value is not None and not math.isfinite(value)
               for value in (delta, rate, ratio, None if ratio is None else ratio*100)):
            raise AnalysisError('分组两期比较的计算结果超出有限数值范围')
        comparisons.append({**item, 'base': base, 'current': current,
            'absolute_change': delta, 'change_rate': rate, 'ratio': ratio,
            'base_100_index': None if ratio is None else ratio*100,
            'status': '缺少基期记录' if base is None else '缺少本期记录' if current is None else
                      '基期为0，比例不可计算' if base == 0 else '已完成'})
    # Disambiguate equal labels without collapsing distinct registered codes.
    for item in comparisons:
        if sum(other['label'] == item['label'] for other in grouped.values()) > 1:
            item['label'] += '（' + '／'.join(item['group_key']) + '）'
    sorts = contract.get('output_sort') or [{'field': '增减额', 'order': 'desc'}]
    for sort in reversed(sorts):
        field = str(sort.get('field') or '')
        key = sort_field_key(field)
        if key is None or sort.get('order', 'asc') not in {'asc', 'desc'}:
            raise AnalysisError('分组对比结果排序字段或方向不受支持')
        valid = sorted([item for item in comparisons if item[key] is not None],
                       key=lambda item: item[key], reverse=sort.get('order') == 'desc')
        comparisons = valid + [item for item in comparisons if item[key] is None]
    missing = sum(item['absolute_change'] is None for item in comparisons)
    return AnalysisOutput(f'已按稳定编码对齐{len(comparisons)}组的两期数据，按要求排序。',
        'grouped_period_comparison', {'grouped_period_comparison': True, 'metric_column': metric,
            'base_label': periods['base'], 'current_label': periods['current'],
            'label_column': labels[0] if len(labels) == 1 else '分组',
            'identity_column': identities[0] if len(identities) == 1 else '分组（编码）',
            'comparisons': comparisons},
        [f'{missing}组缺少一期记录，保留缺失状态，不擅自补0。'] if missing else [])
