"""Execute selected time-series capabilities without the full trend handler."""
from datetime import date
import statistics

from app.analysis.chain_tools.temporal_window import bucket_bounds, in_target_window, detect_edge_bucket_alignment
from app.analysis.operators import OperatorInputError


def trend(engine, request, columns, rows, selected):
    from app.analysis.engine import AnalysisError, AnalysisOutput
    metric, values = engine._series(request, columns, rows)
    time_col = next((col for col in columns if col != metric and any(word in col.lower() for word in
        ('日期', '时间', '月份', '季度', '年份', '周', 'date', 'time', 'month', 'year', 'period', 'quarter', 'week'))), None)
    if time_col is None:
        raise AnalysisError('所选时序工具需要明确的时间列')
    contract = request._analysis_contract or {}
    grain = contract.get('grain')
    totals = {}
    for row, value in zip(rows, values):
        label = str(row.get(time_col, '')).strip()
        if not bucket_bounds(label, grain):
            raise AnalysisError('时间标签无法按所选粒度解析')
        totals[label] = totals.get(label, 0.0) + float(value)
    ordered = sorted(totals, key=lambda label: bucket_bounds(label, grain)[0])
    target = contract.get('target_window')
    labels = ordered
    excluded = set()
    edges = {}
    if target:
        start, end = date.fromisoformat(target['start']), date.fromisoformat(target['end_exclusive'])
        labels = [label for label in ordered if in_target_window(label, start, end, grain)]
        edges = detect_edge_bucket_alignment(labels, start, end, grain)
        excluded = {edges[key] for key in ('first_partial_label', 'last_partial_label') if edges.get(key)}
    if not labels:
        raise AnalysisError('用户目标区间没有可用观测值')
    clean = [label for label in labels if label not in excluded]
    numbers = [totals[label] for label in clean]
    facts = {'metric_column': metric, 'point_count': len(labels),
             'temporal_continuity': engine._temporal_continuity(labels), 'edge_bucket_integrity': edges,
             'result_scope': {'time_column': time_col, 'labels': labels, 'target_window': target,
                              'excluded_baseline_count': len(ordered)-len(labels)}}
    model_catalog=(contract.get('tool_selection') or {}).get('source')=='MODEL_CATALOG'
    if model_catalog:
        from app.analysis.tools.registry import TOOLS
        facts['capability_readiness']={key:{'status':'NOT_COMPUTABLE',
            'reason':f'至少需要{TOOLS[key]["minimum_observations"]}个完整目标时间点，实际为{len(numbers)}个'}
            for key in selected if key not in {'period_over_period_change','year_over_year_change'}
            and TOOLS[key]['minimum_observations'] and len(numbers)<TOOLS[key]['minimum_observations']}
    warnings = []
    for method, key in [('period_over_period_change', 'period_over_period'), ('year_over_year_change', 'year_over_year')]:
        if method not in selected:
            continue
        try:
            result = engine.operator_registry.execute(method, [time_col, metric],
                [{time_col: label, metric: totals[label]} for label in ordered])
            detail = dict(result.facts)
            changes = [item for item in detail.get('changes', [])
                       if item.get('to', item.get('label')) in labels
                       and item.get('to', item.get('label')) not in excluded and item.get('from') not in excluded]
            detail['changes'] = changes
            if model_catalog:
                present={item.get('to',item.get('label')) for item in changes}
                changes.extend({'label':label,'to':label,'current':totals[label],'status':'NO_BASELINE'}
                    for label in labels if label not in present and label not in excluded)
                changes.sort(key=lambda item:labels.index(item.get('to',item.get('label'))))
            valid = sum(item.get('status') not in {'NO_BASELINE', 'NOT_COMPUTABLE'} for item in changes)
            detail.update(period_count=len(changes), computable_count=valid)
            if any(item.get('base') == 0 and item.get('change_rate') is None for item in changes):
                warnings.append('基期为0，仅计算增减量，变化率不可计算')
            if valid < len(changes):
                warnings.append('部分目标期间缺少完整基期，已逐项标记，不填0')
                if model_catalog and valid:detail['status']='PARTIAL'
            if not valid:
                detail.update(status='NOT_COMPUTABLE', reason='没有完整且可比较的基期；缺期或残缺时间桶不补0')
            facts[key] = detail
        except OperatorInputError as exc:
            facts[key] = {'status': 'NOT_COMPUTABLE', 'reason': str(exc), 'changes': []}
        if facts[key].get('status') == 'NOT_COMPUTABLE':
            warnings.append(facts[key]['reason'])
    if 'extrema' in selected:
        facts['selected_extrema'] = [{'最低值': min(numbers), '最高值': max(numbers)}] if numbers else []
    if 'volatility' in selected:
        avg = statistics.mean(numbers) if numbers else 0
        facts['selected_volatility'] = [{'标准差': statistics.pstdev(numbers),
            '变异系数': statistics.pstdev(numbers)/abs(avg) if avg else None}] if numbers else []
    if 'trend_summary' in selected:
        if len(numbers) >= 2:
            facts.update(start=numbers[0], end=numbers[-1], absolute_change=numbers[-1]-numbers[0],
                change_rate=engine._pct_change(numbers[0], numbers[-1]), trend_diagnostics=engine._trend_diagnostics(numbers))
        else:
            facts['trend_diagnostics'] = {'status': 'NOT_COMPUTABLE', 'reason': '至少需要两个完整时间点'}
    if 'continuous_runs' in selected:
        facts['continuous_runs'] = engine._continuous_runs(clean, numbers)
        if model_catalog:
            minimum=(contract['tool_selection'].get('parameters',{}).get('continuous_runs',{}).get('min_changes',2))
            facts['continuous_runs']=[run for run in facts['continuous_runs'] if run['period_count']>=minimum]
    if 'turning_points' in selected:
        facts['change_points'] = []
        for i in range(1, len(clean)-1):
            a, b, c = (totals[label] for label in clean[i-1:i+2])
            if (b-a)*(c-b) < 0 and all(bucket_bounds(clean[j], grain)[1] == bucket_bounds(clean[j+1], grain)[0] for j in (i-1, i)):
                facts['change_points'].append({'label': clean[i], 'before_change': b-a, 'after_change': c-b,
                                              'type': 'PEAK' if b>a else 'TROUGH'})
    return AnalysisOutput('已按用户要求执行所选时序工具。', 'selected_time_series_tools', facts, warnings)
