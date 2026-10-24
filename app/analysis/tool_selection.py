"""Internal capability selection: a broad chain versus explicit independent tools.

No API fields or authorization are created here. Required input validation and
mathematical prerequisites always run; unrelated analysis tools do not.
"""
import re
from app.domain.models import PrimaryIntent as I

BROAD = {
    I.TREND_ANALYSIS: r'趋势分析|分析.{0,35}趋势|变化趋势|走势分析',
    I.COMPARISON_ANALYSIS: r'对比分析|比较分析|对比|比较',
    I.COMPOSITION_ANALYSIS: r'占比分析|构成分析|结构分析',
    I.ANOMALY_ANALYSIS: r'异常分析|异常检测|检测.{0,60}异常|是否存在异常',
    I.ROOT_CAUSE_ANALYSIS: r'归因分析|原因分析|分析.{0,20}原因',
    I.FORECAST_ANALYSIS: r'预测分析|预测|预估',
}
TOOLS = {
    I.TREND_ANALYSIS: {
        'period_over_period_change': r'环比|比上月|较上月',
        'year_over_year_change': r'同比|比去年同期|较上年同期',
        'turning_points': r'拐点|转折点',
        'continuous_runs': r'连续(?:上涨|下跌|涨跌)|持续(?:上涨|下跌)',
        'extrema': r'最高|最低|极值',
        'volatility': r'变异系数|波动系数',
        'trend_summary': r'稳健斜率|方向一致性|首尾变化',
    },
    I.COMPARISON_ANALYSIS: {
        'comparison_difference': r'差额|差值|变化率|增长率',
        'ratio_and_index': r'倍数|基期.{0,3}指数|基期100',
        'group_comparison': r'逐组差异|分组差异',
    },
    I.COMPOSITION_ANALYSIS: {
        'composition_share': r'占比|比重|份额',
        'cumulative_contribution': r'累计贡献|累计占比|80%|80％',
        'concentration': r'集中度|HHI|分布熵|等效分组',
        'share_shift': r'结构变化|份额变化|份额变动',
    },
    I.ANOMALY_ANALYSIS: {
        'MAD': r'(?<![A-Za-z])MAD(?![A-Za-z])|中位数绝对偏差',
        'IQR': r'(?<![A-Za-z])IQR(?![A-Za-z])|四分位(?:距|围栏)',
        'Z_SCORE': r'Z[-_ ]?(?:SCORE|分数)|标准Z',
        'ADJACENT_JUMP': r'相邻跳变|相邻突变',
    },
    I.ROOT_CAUSE_ANALYSIS: {
        'dimension_contribution': r'增减贡献|分项贡献|变化贡献',
        'positive_negative_offset': r'拉升|拖累|抵消',
        'effect_reconciliation': r'贡献对账|归因对账|未解释残差',
        'price_volume_decomposition': r'量价分解|量价效应',
    },
    I.FORECAST_ANALYSIS: {
        'forecast_backtest': r'回测|模型比较',
        'forecast_next': r'下一期预测|下一月预测',
        'model:naive_last': r'上一期延续|朴素预测',
        'model:recent_mean_3': r'最近3期均值|最近三期均值',
        'model:linear_drift': r'线性趋势外推|线性外推',
    },
}


def _mentioned(pattern, question):
    return any(not re.search(r'(?:不要|无需|不用|不做|不计算|不需要|不看)(?:进行|计算|分析|查看|做)?$',
                             question[max(0, match.start()-12):match.start()])
               for match in re.finditer(pattern, question, re.I))


def select_tools(intent, question):
    text = question or ''
    chosen = [key for key, pattern in TOOLS.get(intent, {}).items() if _mentioned(pattern, text)]
    # A named anomaly algorithm or forecast model is an explicit method choice,
    # even when the sentence also says "异常检测" or "预测".
    algorithm = intent == I.ANOMALY_ANALYSIS and chosen or any(key.startswith('model:') for key in chosen)
    limited = bool(re.search(r'仅|只(?:需|要|做|计算|分析|检测|用|执行)', text))
    full = _mentioned(BROAD.get(intent, r'(?!)'), text) and not limited and not algorithm
    return {'mode': 'SELECTED' if chosen and not full else 'FULL', 'capabilities': chosen if not full else []}


def independent_intent(question):
    # Strong analytical phrases only. Ordinary retrieval/ranking must keep its
    # existing route; a free-standing "金额" or "排序" is not a tool request.
    for intent in (I.ANOMALY_ANALYSIS, I.ROOT_CAUSE_ANALYSIS, I.FORECAST_ANALYSIS,
                   I.TREND_ANALYSIS, I.COMPOSITION_ANALYSIS, I.COMPARISON_ANALYSIS):
        if select_tools(intent, question)['mode'] == 'SELECTED':
            return intent
    return None


def selected_for(request):
    contract = getattr(request, '_analysis_contract', None) or {}
    selection = contract.get('tool_selection') or select_tools(request.primary_intent, request.original_question)
    return set(selection['capabilities']) if selection.get('mode') == 'SELECTED' else None


def report_allowed(request, method):
    selected = selected_for(request)
    if selected is None:
        return True
    mapping = {
        'theil_sen_pairwise_slope': 'trend_summary', 'period_over_period': 'period_over_period_change',
        'year_over_year': 'year_over_year_change', 'neighbor_direction_reversal': 'turning_points',
        'adjacent_sign_runs': 'continuous_runs', 'part_divided_by_total': 'composition_share',
        'group_share_threshold_filter': 'composition_share',
        'descending_cumulative_share': 'cumulative_contribution', 'hhi_and_entropy': 'concentration',
        'share_percentage_point_shift': 'share_shift', 'structural_share_shift': 'share_shift',
        'current_divided_by_base': 'ratio_and_index', 'comparable_entity_difference': 'group_comparison',
        'current_minus_baseline_by_member': 'dimension_contribution', 'signed_contribution_sum': 'positive_negative_offset',
        'contribution_reconciliation': 'effect_reconciliation', 'effect_reconciliation': 'effect_reconciliation',
        'expanding_window_one_step_backtest': 'forecast_backtest', 'train_prefix_predict_next': 'forecast_backtest',
        'selected_model_refit': 'forecast_next',
    }
    if method == 'MEDIAN_RELATIVE_DEVIATION':
        return 'MAD' in selected
    if method in {'series_readiness_check', 'temporal_continuity', 'forecast_readiness_check', 'detector_union'}:
        return True  # input checks and combined results of selected detectors
    if request.primary_intent == I.COMPARISON_ANALYSIS and method not in mapping:
        return 'comparison_difference' in selected
    if request.primary_intent == I.FORECAST_ANALYSIS and any(key.startswith('model:') for key in selected):
        return method in {'expanding_window_one_step_backtest', 'train_prefix_predict_next', 'selected_model_refit'}
    return mapping.get(method, method) in selected


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
