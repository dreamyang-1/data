"""Deterministic construction of user-visible analysis plans."""
from __future__ import annotations

from app.domain.models import (
    AnalysisHypothesis,
    AnalysisOperator,
    AnalysisPlan,
    CanonicalAnalysisRequest,
    DataSufficiencyRule,
    ExplorationQueryRequirements,
    PrimaryIntent,
)
from app.analysis.contracts import (
    contract_for_request,
    explicit_object_comparison_scope,
    ordered_entity_metric_ranking_request,
)


class AnalysisPlanner:
    _METHODS = {
        PrimaryIntent.TREND_ANALYSIS: ["time_order_validation", "trend_change_calculation"],
        PrimaryIntent.COMPARISON_ANALYSIS: ["baseline_alignment", "absolute_and_rate_difference"],
        PrimaryIntent.COMPOSITION_ANALYSIS: ["part_whole_reconciliation", "share_calculation"],
        PrimaryIntent.ANOMALY_ANALYSIS: ["series_readiness_check", "robust_outlier_detection"],
        PrimaryIntent.ROOT_CAUSE_ANALYSIS: ["target_change_confirmation", "dimension_contribution_decomposition"],
        PrimaryIntent.FORECAST_ANALYSIS: ["forecast_readiness_check", "deterministic_model_selection", "backtest"],
        PrimaryIntent.REPORT_GENERATION: ["descriptive_profile", "evidence_grounded_report"],
        PrimaryIntent.DATA_QUALITY: ["completeness_check", "duplicate_and_validity_check"],
    }
    _MINIMUM_ROWS = {
        PrimaryIntent.TREND_ANALYSIS: 2,
        PrimaryIntent.COMPARISON_ANALYSIS: 2,
        PrimaryIntent.COMPOSITION_ANALYSIS: 2,
        PrimaryIntent.ANOMALY_ANALYSIS: 5,
        PrimaryIntent.ROOT_CAUSE_ANALYSIS: 2,
        PrimaryIntent.FORECAST_ANALYSIS: 8,
        PrimaryIntent.REPORT_GENERATION: 1,
        PrimaryIntent.DATA_QUALITY: 1,
    }

    def build(self, request: CanonicalAnalysisRequest) -> AnalysisPlan | None:
        methods = list(self._METHODS.get(request.primary_intent, []))
        ordered_entity_ranking = ordered_entity_metric_ranking_request(request)
        if (
            AnalysisOperator.TOP_N in request.operators
            or AnalysisOperator.BOTTOM_N in request.operators
            or ordered_entity_ranking
        ):
            methods = ["ranking_metric_validation", "stable_ranking", "coverage_reconciliation"]
        if not methods:
            return None
        from app.analysis.tools.registry import chain_for_intent
        if chain_for_intent(request.primary_intent) is not None:
            if not request._analysis_contract:
                # Planning has not finished yet; do not infer tools for a UI summary.
                return None
            from app.analysis.tool_selection import selected_for
            selected_for(request)
            methods = list(request._analysis_contract.get('methods') or request._analysis_contract['tool_selection']['capabilities'])
        object_scope = explicit_object_comparison_scope(request)
        minimum_rows = (
            1
            if AnalysisOperator.TOP_N in request.operators
            or AnalysisOperator.BOTTOM_N in request.operators
            or ordered_entity_ranking
            else object_scope.minimum_returned_objects
            if object_scope is not None
            else self._MINIMUM_ROWS[request.primary_intent]
        )
        sufficiency_code = (
            "MIN_ROWS_EXPLICIT_OBJECT_COMPARISON"
            if object_scope is not None
            else f"MIN_ROWS_{request.primary_intent.value}"
        )
        sufficiency_description = (
            f"用户显式指定了 {len(object_scope.requested_objects)} 个对象；"
            "至少返回两个唯一对象即可执行描述性横向比较，未返回对象必须披露"
            if object_scope is not None
            else "数据行数必须足以支持所选确定性分析方法"
        )
        metric_names = [
            metric.canonical_name or metric.input or metric.metric_id
            for metric in request.metrics
            if metric.canonical_name or metric.input or metric.metric_id
        ]
        objective = f"对“{request.original_question}”执行可验证的{request.primary_intent.value}"
        hypotheses: list[AnalysisHypothesis] = []
        if request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
            hypotheses.append(AnalysisHypothesis(
                statement="目标指标变化可能由候选维度的结构或数值变化贡献",
                verification="逐维度计算变化贡献并与总体变化对账",
            ))
        baseline = self._baseline(request)
        return AnalysisPlan(
            objective=objective,
            methods=methods,
            metrics=list(dict.fromkeys(str(item) for item in metric_names)),
            dimensions=list(dict.fromkeys(request.dimensions)),
            baseline=baseline,
            hypotheses=hypotheses,
            sufficiency_rules=[DataSufficiencyRule(
                code=sufficiency_code,
                description=sufficiency_description,
                minimum_rows=minimum_rows,
            )],
            conclusion_policy=[
                "所有数值结论必须能回溯到查询结果或确定性计算事实",
                "数据不足时停止分析并返回缺失条件",
                "贡献度或相关性不得表述为已证明的因果关系",
            ],
            data_contract=contract_for_request(request),
            exploration_requirements=(
                ExplorationQueryRequirements()
                if request.primary_intent == PrimaryIntent.REPORT_GENERATION
                else None
            ),
        )

    @staticmethod
    def _baseline(request: CanonicalAnalysisRequest) -> str | None:
        if request.comparison_type:
            return request.comparison_type
        if request.primary_intent == PrimaryIntent.TREND_ANALYSIS:
            return "按时间顺序比较相邻有效观测值"
        if request.primary_intent == PrimaryIntent.COMPOSITION_ANALYSIS:
            return "各组成部分与总体值对账"
        if request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
            return "目标指标总体变化"
        return None
