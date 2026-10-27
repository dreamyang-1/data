"""Analysis goals are independent of the query's time-series/aggregate shape."""
import re
from app.domain.models import PrimaryIntent as I

LABELS = {I.TREND_ANALYSIS: '趋势分析', I.COMPARISON_ANALYSIS: '对比分析',
          I.COMPOSITION_ANALYSIS: '占比分析', I.ANOMALY_ANALYSIS: '异常分析',
          I.ROOT_CAUSE_ANALYSIS: '归因分析', I.FORECAST_ANALYSIS: '预测分析'}
PATTERNS = (
    (I.ROOT_CAUSE_ANALYSIS, r'归因|原因分析|下降原因|增长原因|波动原因|分析.{0,12}原因|变化贡献|增减贡献|为什么.{0,20}(?:下降|增长|波动)'),
    (I.FORECAST_ANALYSIS, r'预测|预估|预计|未来走势|回测|候选模型比较|模型比较'),
    (I.ANOMALY_ANALYSIS, r'异常|离群|突增|突降'),
    (I.COMPOSITION_ANALYSIS, r'占比|比重|构成|份额'),
    (I.COMPARISON_ANALYSIS, r'对比|比较|差额|差异率'),
    (I.TREND_ANALYSIS, r'趋势|走势|月度变化|连续涨跌|拐点'),
)


def analysis_goals(question: str) -> list[I]:
    goals = []
    # Negation is local to a clause; it must not erase another explicit goal.
    for clause in re.split(r'[，,。；;]|同时|并且', question or ''):
        for intent, pattern in PATTERNS:
            if intent == I.ROOT_CAUSE_ANALYSIS and re.search(r'不分析原因|不是(?:要)?分析(?:下降|增长|波动)?原因', clause):
                continue
            if re.search(r'(?:不要|不做|无需|不用|不需要|不是(?:要)?|不分析原因|不用分析原因).{0,6}(?:' + pattern + ')', clause):
                continue
            if re.search(pattern, clause) and intent not in goals:
                goals.append(intent)
    # "预测未来趋势" and "归因变化" are single deliverables. Only a
    # separate explicit trend goal should add a second chain.
    if I.FORECAST_ANALYSIS in goals and I.TREND_ANALYSIS in goals:
        if not re.search(r'趋势分析|分析.{0,30}历史.{0,10}趋势|先.{0,20}趋势', question):
            goals.remove(I.TREND_ANALYSIS)
    if not goals:
        from app.analysis.tool_selection import independent_intent
        independent = independent_intent(question)
        if independent:
            goals.append(independent)
    return goals


def resolve_analysis_intent(intent: I, question: str) -> I:
    if intent not in LABELS:
        return intent
    goals = analysis_goals(question)
    return goals[0] if goals else intent
