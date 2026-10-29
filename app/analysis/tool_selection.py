"""Internal capability selection: a broad chain versus explicit independent tools.

No API fields or authorization are created here. Required input validation and
mathematical prerequisites always run; unrelated analysis tools do not.
"""
from app.domain.models import PrimaryIntent as I
from app.analysis.tools.plans import AnalysisToolPlan, AnalysisToolPlanRequired
from app.analysis.tools.registry import CHAINS, DEFAULTS, TOOLS as CATALOG, chain_for_intent


def select_tools(intent, question=None, *, plan=None, selection=None):
    """Validate explicit model fields. Question text is never inspected.

    The question argument is retained only for old callers to receive a clear
    contract error; it can never select, exclude or expand a capability.
    """
    chain = chain_for_intent(intent)
    if chain is None:
        raise ValueError('UNREGISTERED_ANALYSIS_CHAIN')
    if plan is not None:
        proposal = plan if isinstance(plan, AnalysisToolPlan) else AnalysisToolPlan.model_validate(plan)
        if proposal.chain != chain:
            raise ValueError('ANALYSIS_PLAN_INTENT_MISMATCH')
        return proposal.selection()
    if not isinstance(selection, dict):
        raise AnalysisToolPlanRequired('ANALYSIS_TOOL_PLAN_REQUIRED: 请由规划模型输出工具ID、参数和数据需求')
    if selection.get('mode') != 'SELECTED':
        raise ValueError('EXPLICIT_TOOL_IDS_REQUIRED')
    keys = selection.get('capabilities')
    if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
        raise ValueError('EMPTY_OR_INVALID_TOOL_IDS')
    allowed = {tool['capability_id'] for tool in CHAINS[chain][2]}
    if len(keys) != len(set(keys)) or not set(keys).issubset(allowed):
        raise ValueError('DUPLICATE_OR_UNREGISTERED_TOOL_IDS')
    from jsonschema import Draft202012Validator
    parameters = selection.get('parameters') or {}
    if not isinstance(parameters, dict) or not set(parameters).issubset(keys):
        raise ValueError('UNBOUND_TOOL_PARAMETERS')
    import json
    try:
        json.dumps(parameters, allow_nan=False)
        for key, args in parameters.items():
            Draft202012Validator(CATALOG[key]['parameter_schema']).validate(args)
    except Exception as exc:
        raise ValueError('INVALID_TOOL_PARAMETERS') from exc
    return selection


def selected_for(request):
    if chain_for_intent(request.primary_intent) is None:
        return None
    contract = getattr(request, '_analysis_contract', None) or {}
    selection = select_tools(request.primary_intent, selection=contract.get('tool_selection'))
    return set(selection['capabilities'])


_REPORT_CAPABILITIES = {
    'theil_sen_pairwise_slope': 'trend_summary', 'period_over_period': 'period_over_period_change',
    'year_over_year': 'year_over_year_change', 'neighbor_direction_reversal': 'turning_points',
    'adjacent_sign_runs': 'continuous_runs', 'extrema': 'extrema', 'volatility': 'volatility',
    'part_divided_by_total': 'composition_share', 'group_share_threshold_filter': 'composition_share',
    'descending_cumulative_share': 'cumulative_contribution', 'hhi_and_entropy': 'concentration',
    'share_percentage_point_shift': 'share_shift', 'structural_share_shift': 'share_shift',
    'current_divided_by_base': 'ratio_and_index', 'comparable_entity_difference': 'group_comparison',
    'current_minus_baseline_by_member': 'dimension_contribution',
    'multidimensional_root_cause': 'dimension_contribution',
    'signed_contribution_sum': 'positive_negative_offset',
    'contribution_reconciliation': 'effect_reconciliation', 'effect_reconciliation': 'effect_reconciliation',
    'price_volume_decomposition': 'price_volume_decomposition',
    'expanding_window_one_step_backtest': 'forecast_backtest', 'train_prefix_predict_next': 'forecast_backtest',
    'selected_model_refit': 'forecast_next', 'MAD': 'MAD', 'MEDIAN_RELATIVE_DEVIATION': 'MAD',
    'IQR': 'IQR', 'Z_SCORE': 'Z_SCORE', 'ADJACENT_JUMP': 'ADJACENT_JUMP',
}


def report_capability(intent, method):
    if intent == I.COMPARISON_ANALYSIS and method not in _REPORT_CAPABILITIES:
        return 'comparison_difference'
    return _REPORT_CAPABILITIES.get(method)


def report_allowed(request, method):
    selected = selected_for(request)
    if selected is None:
        return True
    if method == 'multidimensional_root_cause' and selected.intersection({
        'multidimensional_attribution','multidimensional_ratio_attribution'}):
        return True
    if method in {'series_readiness_check', 'temporal_continuity', 'forecast_readiness_check', 'detector_union'}:
        return True  # input checks and combined results of selected detectors
    if request.primary_intent == I.FORECAST_ANALYSIS and any(key.startswith('model:') for key in selected):
        return method in {'expanding_window_one_step_backtest', 'train_prefix_predict_next', 'selected_model_refit'}
    return report_capability(request.primary_intent, method) in selected


def selected_capability_errors(intent, selection, facts):
    """Validate explicitly requested capabilities and their deterministic facts."""
    if not isinstance(selection, dict) or selection.get('mode') != 'SELECTED':
        return []
    selected = set(selection.get('capabilities') or [])
    if selection.get('source') == 'MODEL_CATALOG':
        from app.analysis.tools.execution import capability_errors
        return capability_errors(intent, selection, facts)
    reports = facts.get('capability_results') or []
    completed = {
        capability
        for report in reports if isinstance(report, dict) and report.get('status') == 'COMPLETED'
        for capability in [report_capability(intent, report.get('method'))]
        if capability
    }
    errors = []
    for capability in sorted(selected):
        if capability.startswith('model:'):
            if facts.get('selected_model') != capability.split(':', 1)[1]:
                errors.append('指定预测模型=' + capability)
            if 'forecast_next' not in completed:
                errors.append('指定模型的下一期预测事实')
        elif capability not in completed:
            errors.append('指定能力=' + capability)
    declared = facts.get('tool_selection')
    if declared is not None and set(declared) != selected:
        errors.append('实际能力集合与用户指定不一致')
    if 'forecast_next' not in selected and not any(
        capability.startswith('model:') for capability in selected
    ) and facts.get('forecast') is not None:
        errors.append('用户未要求预测但返回了未来预测')
    return errors


TOOL_NAMES = {
    'period_over_period_change': '环比分析工具', 'year_over_year_change': '同比分析工具',
    'turning_points': '拐点识别工具', 'continuous_runs': '连续涨跌识别工具',
    'extrema': '极值统计工具', 'volatility': '波动系数工具', 'trend_summary': '趋势概要工具',
    'comparison_difference': '差额与变化率工具', 'ratio_and_index': '倍数与基期指数工具',
    'group_comparison': '分组差异工具', 'composition_share': '占比计算工具',
    'cumulative_contribution': '累计贡献工具', 'concentration': '集中度工具', 'share_shift': '结构变化工具',
    'MAD': 'MAD异常检测工具', 'IQR': 'IQR异常检测工具', 'Z_SCORE': 'Z分数异常检测工具',
    'ADJACENT_JUMP': '相邻跳变工具', 'dimension_contribution': '分项贡献工具',
    'positive_negative_offset': '拉升拖累与抵消工具', 'effect_reconciliation': '贡献对账工具',
    'price_volume_decomposition': '量价分解工具', 'forecast_backtest': '历史回测工具',
    'forecast_next': '下一期预测工具', 'model:naive_last': '上一期延续预测工具',
    'model:recent_mean_3': '最近3期均值预测工具', 'model:linear_drift': '线性趋势外推预测工具',
}

from app.analysis.tools.registry import TOOLS as _CATALOG_TOOLS
TOOL_NAMES.update({key: tool['title'] for key, tool in _CATALOG_TOOLS.items() if key not in TOOL_NAMES})
_REPORT_CAPABILITIES.update({
    'funnel_conversion_analysis':'funnel_conversion_analysis',
    'multidimensional_attribution':'multidimensional_attribution',
    'multidimensional_ratio_attribution':'multidimensional_ratio_attribution',
})
