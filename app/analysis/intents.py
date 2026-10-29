"""Analysis goals are independent of the query's time-series/aggregate shape."""
from app.domain.models import PrimaryIntent as I
LABELS = {I.TREND_ANALYSIS: '趋势分析', I.COMPARISON_ANALYSIS: '对比分析',
          I.COMPOSITION_ANALYSIS: '占比分析', I.ANOMALY_ANALYSIS: '异常分析',
          I.ROOT_CAUSE_ANALYSIS: '归因分析', I.FORECAST_ANALYSIS: '预测分析'}
_INTENT_LABELS = {
    '统计查询': I.METRIC_QUERY, '指标查询': I.METRIC_QUERY,
    '明细查询': I.DETAIL_QUERY, '解释口径': I.METRIC_DEFINITION,
    '数据血缘': I.DATA_LINEAGE, '数据质量': I.DATA_QUALITY,
    '报告生成': I.REPORT_GENERATION, '闲聊': I.CHAT,
    '超出范围': I.OUT_OF_SCOPE, **{value: key for key, value in LABELS.items()},
}


def primary_intent_from_label(value) -> I | None:
    """Parse an upstream intent label without reclassifying the question."""
    if isinstance(value, I):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return I(text)
    except ValueError:
        return _INTENT_LABELS.get(text)


def analysis_goals(question=None, *, routes=None, intent=None) -> list[I]:
    """Read model-produced chain IDs/intent enums; never inspect free text."""
    from app.analysis.tools.registry import intent_for_chain
    if routes is not None:
        return list(dict.fromkeys(intent_for_chain(route) for route in routes))
    parsed = primary_intent_from_label(intent)
    return [parsed] if parsed in LABELS else []


def resolve_analysis_intent(intent: I, question=None) -> I:
    """The upstream model owns the intent, even if the wording suggests another."""
    return intent


def normalize_locked_root_intent(intent: I | None, question=None) -> I | None:
    """Do not reclassify an already locked model decision."""
    return intent
