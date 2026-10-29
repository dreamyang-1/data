"""Compile model-selected capabilities into deterministic data requirements."""
from datetime import timedelta
import copy

from app.analysis.chain_tools.temporal_window import normalize_grain, resolve_target_window, shift_calendar_months
from .registry import CHAINS, TOOLS, registered_operator


def build_model_requirements(plan, structured, *, today):
    selection = plan.selection()
    methods = selection['capabilities']
    intent = CHAINS[plan.chain][1]
    granularity = structured.get('时间粒度') or {}
    grain = plan.grain or normalize_grain(granularity.get('unit'))
    if plan.chain in {'trend', 'anomaly', 'forecast'} and grain is None:
        raise ValueError('ANALYSIS_GRAIN_REQUIRED_BY_TOOL_CONTRACT')
    target = ((plan.target_window.start, plan.target_window.end_exclusive-timedelta(days=1))
              if plan.target_window else resolve_target_window(str(granularity.get('time_range') or ''), grain, today=today))
    if target is None and plan.comparison_periods:
        target=(min(p.start for p in plan.comparison_periods),max(p.end_exclusive for p in plan.comparison_periods)-timedelta(days=1))
    result = {
        'version': 2, 'intent': intent.value, 'methods': methods, 'tool_selection': selection,
        'grain': grain, 'reference_date': today.isoformat(), 'timezone': 'Asia/Shanghai',
        'time_anchor': granularity.get('anchor'), 'missing_policy': 'REPORT_NOT_COMPUTABLE',
        'minimum_points': max((TOOLS[key]['minimum_observations'] or 1 for key in methods), default=1),
        'input_shape': {'trend':'ordered_time_series','comparison':'comparable_groups_or_periods',
            'composition':'non_overlapping_parts_and_total','anomaly':'comparable_observations',
            'attribution':'baseline_current_by_dimension','forecast':'ordered_history'}[plan.chain],
        'metric_names': [item.get('name') if isinstance(item,dict) else item for item in structured.get('指标') or []],
        'filters': copy.deepcopy(structured.get('过滤条件') or []), 'windows': [],
        'operator': registered_operator(selection),
        'auxiliary_fetch_allowed': plan.auxiliary_fetch_allowed,
        'comparison_periods': [item.model_dump(mode='json') for item in plan.comparison_periods],
    }
    if target:
        start,end=target
        result['target_window']={'start':start.isoformat(),'end_exclusive':(end+timedelta(days=1)).isoformat()}
        retrieval_start=start
        if 'period_over_period_change' in methods and plan.auxiliary_fetch_allowed:
            size={'month':1,'quarter':3,'half_year':6,'year':12}.get(grain)
            aligned=(start.day==1 and (grain=='month' or grain=='quarter' and start.month in {1,4,7,10}
                or grain=='half_year' and start.month in {1,7} or grain=='year' and start.month==1))
            if size and aligned:retrieval_start=shift_calendar_months(start,-size)
            elif grain=='day':retrieval_start=start-timedelta(days=1)
            elif grain=='week' and start.weekday()==0:retrieval_start=start-timedelta(days=7)
        result['windows'].append({'role':'target','start':retrieval_start.isoformat(),'end':end.isoformat()})
        if 'year_over_year_change' in methods and plan.auxiliary_fetch_allowed:
            if grain=='month':
                result['windows'].append({'role':'yoy_baseline','start':shift_calendar_months(start,-12).isoformat(),
                    'end':shift_calendar_months(end,-12).isoformat()})
            else:
                selection['unavailable']=[*selection['unavailable'],'同比算子当前仅支持月粒度，本次不伪造其他日历同比。']
    return result
