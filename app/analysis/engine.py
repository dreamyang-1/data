from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import AnalysisOperator, AnalysisRequirement, CanonicalAnalysisRequest, KnowledgeContext, MetricRef, PrimaryIntent
from app.analysis.forecasting import DeterministicForecastSelector, ForecastModelError, assess_time_axis
from app.analysis.profiling import profile_dataset
from app.analysis.visualization import build_chart_specs
from app.analysis.operators import AnalysisOperatorRegistry, OperatorInputError
from app.analysis.insight_discovery import discover_insights
from app.analysis.contracts import (
    ExplicitObjectComparisonScope,
    explicit_object_comparison_scope,
    multidimensional_operator_for_question,
    ordered_entity_metric_ranking_request,
)


class AnalysisError(RuntimeError):
    """The dataset cannot support the requested conclusion safely."""

    def __init__(
        self, message: str, *, requirements: list[AnalysisRequirement] | None = None
    ) -> None:
        super().__init__(message)
        self.requirements = requirements or []


@dataclass(frozen=True)
class AnalysisOutput:
    answer: str
    method: str
    facts: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


class AnalysisEngine:
    """Deterministic analysis over validated query rows.

    It computes facts only. Knowledge documents may supply candidate business
    explanations, but are never promoted to measured causes.
    """

    def __init__(self, operator_registry: AnalysisOperatorRegistry | None = None) -> None:
        self.operator_registry = operator_registry or AnalysisOperatorRegistry()

    @staticmethod
    def _display_number(value: float) -> str:
        """Render business values without scientific notation."""
        if not math.isfinite(value):
            return str(value)
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.4f}".rstrip("0").rstrip(".")

    @classmethod
    def _table_cell(cls, value: Any, *, percentage: bool = False) -> str:
        if value is None:
            return "—"
        number = cls._number(value)
        if number is not None:
            rendered = (
                f"{float(number):.2%}"
                if percentage
                else cls._display_number(float(number))
            )
        else:
            rendered = str(value)
        return rendered.replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")

    @staticmethod
    def _display_column_name(column: str) -> str:
        raw = str(column)
        aliases = {
            "product_name": "产品名称",
            "dealer_name": "经销商名称",
            "hospital_name": "医院名称",
            "customer_name": "客户名称",
            "supplier_name": "供应商名称",
            "store_name": "门店名称",
            "hospital_level": "医院等级",
            "hospital_grade": "医院等级",
            "province": "省份",
            "city": "城市",
        }
        tail = raw.rsplit(".", 1)[-1]
        return aliases.get(tail, tail.replace("_", " "))

    def analyze(
        self,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        knowledge: KnowledgeContext,
    ) -> AnalysisOutput:
        handlers = {
            PrimaryIntent.TREND_ANALYSIS: self._trend,
            PrimaryIntent.COMPARISON_ANALYSIS: self._comparison,
            PrimaryIntent.COMPOSITION_ANALYSIS: self._composition,
            PrimaryIntent.ANOMALY_ANALYSIS: self._anomaly,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS: self._root_cause,
            PrimaryIntent.FORECAST_ANALYSIS: self._forecast,
            PrimaryIntent.REPORT_GENERATION: self._report,
            PrimaryIntent.DATA_QUALITY: self._data_quality,
        }
        handler = handlers.get(request.primary_intent)
        if handler is None:
            raise AnalysisError(f"unsupported analysis intent: {request.primary_intent}")
        if request.primary_intent == PrimaryIntent.DATA_QUALITY and not rows:
            output = AnalysisOutput(
                answer=(
                    "查询范围内没有返回记录，因此无法计算空值率或重复率；"
                    "这表示本次质量检查未完成，不代表数据质量通过。"
                ),
                method="empty_dataset_quality_assessment",
                facts={
                    "row_count": 0,
                    "null_counts": {},
                    "duplicate_row_count": 0,
                    "quality_conclusion": "NOT_ASSESSED",
                    "empty_scope": True,
                },
                warnings=["请确认时间范围、业务口径和数据刷新状态后重新检查"],
            )
        else:
            self._validate_shape(columns, rows)
            output = (
                self._ranking(request, columns, rows, knowledge)
                if ordered_entity_metric_ranking_request(request)
                else self._decline_ranking(request, columns, rows)
                if self._is_decline_ranking(request)
                else self._composable_analysis(request, columns, rows)
                or handler(request, columns, rows, knowledge)
            )
        governed_facts = dict(output.facts)
        governed_facts.update(
            {
                "decision_source": "DETERMINISTIC_ALGORITHM",
                "llm_role": "PRESENTATION_ONLY",
                "algorithm_contract_version": "analysis-contract-v2",
                "data_profile": profile_dataset(columns, rows),
            }
        )
        governed_facts["chart_specs"] = [
            item.model_dump(mode="json")
            for item in build_chart_specs(
                request.primary_intent, columns, rows, governed_facts
            )
        ]
        return AnalysisOutput(
            answer=output.answer,
            method=output.method,
            facts=governed_facts,
            warnings=output.warnings,
        )

    @staticmethod
    def _is_decline_ranking(request: CanonicalAnalysisRequest) -> bool:
        return bool(
            request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
            and request.ranking_limit
            and re.search(
                r"(?:下降|减少|下跌)(?:幅度)?(?:最大|最多)",
                request.original_question or "",
            )
        )

    def _decline_ranking(
        self,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
    ) -> AnalysisOutput:
        """Rank entities by first-to-last change within the requested window."""
        metric, _ = self._series(request, columns, rows)
        time_column = next(
            (
                column for column in columns if column != metric and any(
                    token in column.lower()
                    for token in (
                        "日期", "时间", "月份", "季度", "年份", "周期", "周",
                        "date", "time", "month", "year", "quarter", "period", "week",
                    )
                )
            ),
            None,
        )
        if time_column is None:
            raise AnalysisError("下降排行结果缺少时间列")
        label_candidates = [
            column for column in columns if column not in {metric, time_column}
            and not any(token in column.lower() for token in ("id", "code", "编号", "编码"))
        ]
        label_column = next(
            (
                column for column in label_candidates
                if any(token in column.lower() for token in ("产品", "商品", "product", "名称", "name"))
            ),
            label_candidates[0] if len(label_candidates) == 1 else None,
        )
        if label_column is None:
            raise AnalysisError("下降排行结果缺少唯一产品名称列")
        grouped: dict[str, list[tuple[tuple[int, int, int], str, float]]] = {}
        for row in rows:
            label = str(row.get(label_column, "")).strip()
            period = str(row.get(time_column, "")).strip()
            key = self._time_key(period)
            value = self._number(row.get(metric))
            if not label or key is None or value is None:
                raise AnalysisError("下降排行包含空产品、无效时间或非数值指标")
            grouped.setdefault(label, []).append((key, period, float(value)))
        changes: list[dict[str, Any]] = []
        for label, points in grouped.items():
            points.sort(key=lambda item: item[0])
            if len(points) < 2:
                continue
            start, end = points[0], points[-1]
            delta = end[2] - start[2]
            changes.append({
                "label": label,
                "start_period": start[1],
                "end_period": end[1],
                "start": start[2],
                "end": end[2],
                "change": delta,
                "change_rate": self._pct_change(start[2], end[2]),
            })
        declining = sorted(
            (item for item in changes if item["change"] < 0),
            key=lambda item: item["change"],
        )[: request.ranking_limit]
        if not declining:
            raise AnalysisError("请求时间范围内没有产品形成可验证的销售额下降")
        headers = [
            "排名", self._display_column_name(label_column), "开始周期",
            "结束周期", "期初值", "期末值", "下降金额", "变化率",
        ]
        table_lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
        ]
        for index, item in enumerate(declining, 1):
            table_lines.append("| " + " | ".join([
                self._table_cell(index), self._table_cell(item["label"]),
                self._table_cell(item["start_period"]), self._table_cell(item["end_period"]),
                self._table_cell(item["start"]), self._table_cell(item["end"]),
                self._table_cell(abs(item["change"])),
                self._table_cell(item["change_rate"], percentage=True),
            ]) + " |")
        answer = (
            f"{self._display_column_name(label_column)}按"
            f"{self._display_column_name(metric)}下降幅度排名，共 {len(declining)} 条。\n\n"
            + "\n".join(table_lines)
        )
        return AnalysisOutput(answer, "entity_period_decline_ranking", {
            "metric_column": metric,
            "label_column": label_column,
            "ranking_limit": request.ranking_limit,
            "rankings": declining,
            "selected_labels": [item["label"] for item in declining],
        })

    def _composable_analysis(
        self, request: CanonicalAnalysisRequest, columns: list[str], rows: list[dict[str, Any]]
    ) -> AnalysisOutput | None:
        question = request.original_question.replace(" ", "")
        operator_name = None
        if request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS and "量价" in question:
            operator_name = "price_volume_decomposition"
        elif request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS:
            operator_name = multidimensional_operator_for_question(question)
        elif request.primary_intent == PrimaryIntent.COMPOSITION_ANALYSIS and "漏斗" in question:
            operator_name = "funnel_conversion_analysis"
        elif request.primary_intent == PrimaryIntent.COMPOSITION_ANALYSIS and any(
            token in question for token in ("结构变化", "结构变动", "份额变化", "份额变动")
        ):
            operator_name = "structural_share_shift"
        if operator_name is None:
            return None
        try:
            result = self.operator_registry.execute(operator_name, columns, rows)
        except OperatorInputError as exc:
            raise AnalysisError(str(exc)) from exc
        facts = result.facts
        if operator_name == "price_volume_decomposition":
            answer = (
                f"指标总变化 {facts['total_change']:g}；数量效应 {facts['volume_effect']:+g}，"
                f"价格效应 {facts['price_effect']:+g}，交互效应 {facts['interaction_effect']:+g}。"
                "该结果是量价恒等式分解，不直接证明因果关系。"
            )
        elif operator_name == "funnel_conversion_analysis":
            rate = facts["overall_conversion_rate"]
            answer = "漏斗整体转化率因首阶段为0无法计算。" if rate is None else f"漏斗整体转化率为 {rate:.2%}。"
            valid = [item for item in facts["transitions"] if item["dropoff_rate"] is not None]
            if valid:
                largest = max(valid, key=lambda item: item["dropoff_rate"])
                answer += f"最大流失发生在{largest['from']}到{largest['to']}，流失率 {largest['dropoff_rate']:.2%}。"
        elif operator_name == "structural_share_shift":
            largest = facts["shifts"][0]
            answer = (
                f"结构变化最大的维度为{largest['label']}，份额变化 "
                f"{largest['share_change']:+.2%}；各维度份额变化已完成合计对账。"
            )
        else:
            root_causes = facts["root_causes"]
            if root_causes:
                summary = "；".join(
                    f"{dimension}：{','.join(elements)}"
                    for dimension, elements in root_causes.items()
                )
                answer = f"通过多维异常度与解释力筛选，主要贡献候选为{summary}。"
            elif facts["status"] == "no_anomaly_direction":
                answer = "基期与当前期的指标总量没有变化，无法确定上升或下降归因方向。"
            else:
                answer = "当前数据未形成满足解释力、覆盖度和跨维度一致性要求的归因候选。"
            answer += "这些结果是贡献归因，不代表已证明业务因果关系。"
        return AnalysisOutput(answer, result.method, facts, result.warnings)

    def analyze_ranking(
        self,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        knowledge: KnowledgeContext,
    ) -> AnalysisOutput:
        """Validate TOP/BOTTOM-N output, including coverage-rate reconciliation."""
        self._validate_shape(columns, rows)
        output = self._ranking(request, columns, rows, knowledge)
        governed_facts = dict(output.facts)
        governed_facts.update({
            "decision_source": "DETERMINISTIC_ALGORITHM",
            "llm_role": "PRESENTATION_ONLY",
            "algorithm_contract_version": "analysis-contract-v2",
            "data_profile": profile_dataset(columns, rows),
        })
        governed_facts["chart_specs"] = [
            item.model_dump(mode="json")
            for item in build_chart_specs(
                PrimaryIntent.COMPARISON_ANALYSIS, columns, rows, governed_facts
            )
        ]
        return AnalysisOutput(
            answer=output.answer,
            method=output.method,
            facts=governed_facts,
            warnings=output.warnings,
        )

    def _ranking(self, request, columns, rows, knowledge) -> AnalysisOutput:
        lowered = {column: column.lower() for column in columns}
        rate_column = next((
            column for column in columns
            if any(token in lowered[column] for token in (
                "匹配度", "覆盖率", "match_rate", "coverage_rate", "matching_rate"
            ))
        ), None)
        preferred = list(dict.fromkeys(
            value for metric in request.metrics
            for value in (metric.canonical_name, metric.input, metric.metric_id)
            if value
        ))
        preferred_matches = [name for name in preferred if name in columns]
        unbounded_sort = (
            AnalysisOperator.SORT in request.operators
            and AnalysisOperator.TOP_N not in request.operators
            and AnalysisOperator.BOTTOM_N not in request.operators
        )
        if unbounded_sort and len(preferred_matches) > 1:
            raise AnalysisError("排序结果存在多个请求指标列，无法安全确定排序依据")
        metric_column = next(iter(preferred_matches), None)
        metric_column = rate_column or metric_column
        if metric_column is None:
            numeric_columns = [
                column for column in columns
                if all(self._number(row.get(column)) is not None for row in rows)
                and not any(
                    token in lowered[column]
                    for token in ("id", "code", "编号", "编码", "序号")
                )
            ]
            if len(numeric_columns) != 1:
                raise AnalysisError("排名结果必须明确返回唯一的排名指标列")
            metric_column = numeric_columns[0]
        label_column = self._ranking_label_column(request, columns, metric_column)
        labels = [str(row.get(label_column, "")).strip() for row in rows]
        if any(not label for label in labels) or len(labels) != len(set(labels)):
            raise AnalysisError("排名对象为空或重复，SQL必须按唯一对象聚合")

        values = [self._number(row.get(metric_column)) for row in rows]
        if any(value is None for value in values):
            raise AnalysisError(f"排名指标列“{metric_column}”包含空值或非数值")
        normalized_values = [float(value) for value in values if value is not None]
        coverage_reconciled = False
        numerator_column = denominator_column = None
        is_coverage = bool(rate_column) or any(
            token in (request.original_question or "") for token in ("匹配度", "覆盖率")
        )
        if is_coverage:
            numerator_column = next((
                column for column in columns if any(token in lowered[column] for token in (
                    "匹配科室数", "购买科室数", "matched_department_count", "matched_dept_count"
                ))
            ), None)
            denominator_column = next((
                column for column in columns if any(token in lowered[column] for token in (
                    "有效科室总数", "总科室数", "eligible_department_count", "total_department_count"
                ))
            ), None)
            if not numerator_column or not denominator_column or not rate_column:
                raise AnalysisError(
                    "科室匹配度排名必须同时返回供应商、匹配科室数、有效科室总数和匹配度"
                )
            numerators = [self._number(row.get(numerator_column)) for row in rows]
            denominators = [self._number(row.get(denominator_column)) for row in rows]
            if any(value is None for value in numerators + denominators):
                raise AnalysisError("科室匹配度的分子或分母包含空值/非数值")
            denominator_values = [float(value) for value in denominators if value is not None]
            if any(value <= 0 for value in denominator_values):
                raise AnalysisError("有效科室总数必须大于0")
            if len({round(value, 12) for value in denominator_values}) != 1:
                raise AnalysisError("同一次供应商排名的有效科室总数必须使用统一统计范围")
            reconciled: list[float] = []
            for numerator, denominator, actual in zip(numerators, denominators, normalized_values):
                n, d = float(numerator), float(denominator)
                if n < 0 or n > d:
                    raise AnalysisError("匹配科室数必须处于0到有效科室总数之间")
                expected = n / d
                normalized_actual = actual / 100 if actual > 1 else actual
                if not 0 <= normalized_actual <= 1:
                    raise AnalysisError("科室匹配度必须处于0%到100%之间")
                if abs(normalized_actual - expected) > 0.005:
                    raise AnalysisError(
                        f"供应商匹配度与科室计数不一致：应为{expected:.2%}"
                    )
                reconciled.append(expected)
            normalized_values = reconciled
            coverage_reconciled = True

        descending = AnalysisOperator.BOTTOM_N not in request.operators
        paired = list(zip(labels, normalized_values, rows))
        expected_order = sorted(paired, key=lambda item: item[1], reverse=descending)
        if [item[0] for item in paired] != [item[0] for item in expected_order]:
            raise AnalysisError("排名结果顺序不正确，拒绝直接相信未排序的SQL结果")
        bounded = (
            AnalysisOperator.TOP_N in request.operators
            or AnalysisOperator.BOTTOM_N in request.operators
        )
        limit = (request.ranking_limit or 10) if bounded else None
        if limit is not None and len(rows) > limit:
            raise AnalysisError(f"排名结果超过用户要求的前{limit}名")
        profile_columns: list[str] = []
        profile_sources: dict[str, str] = {}
        # Preserve explicitly requested descriptive dimensions (for example
        # hospital level) in each ranked item. Previously only secondary
        # metrics were rendered, so the SQL returned the level column but the
        # final answer silently discarded it.
        for dimension in request.dimensions:
            source_column = dimension if dimension in columns else None
            if source_column is None and dimension == "医院等级":
                source_column = next((
                    column for column in columns
                    if re.sub(r"[^a-z0-9]", "", column.lower()).endswith(
                        ("hospitallevel", "hospitalgrade")
                    )
                ), None)
            if (
                source_column is not None
                and dimension not in {label_column, metric_column}
                and dimension not in profile_columns
            ):
                profile_columns.append(dimension)
                profile_sources[dimension] = source_column
        for metric in request.metrics:
            for candidate in (
                metric.canonical_name,
                metric.input,
                metric.metric_id,
                (
                    metric.metric_id.split(":", 1)[-1]
                    if metric.metric_id and ":" in metric.metric_id
                    else None
                ),
            ):
                if (
                    candidate
                    and candidate in columns
                    and candidate not in {label_column, metric_column}
                    and candidate not in profile_columns
                ):
                    profile_columns.append(candidate)
                    profile_sources[candidate] = candidate
        rankings = [
            {
                "rank": index,
                "label": label,
                "value": value,
                "profile": {
                    column: row.get(profile_sources[column])
                    for column in profile_columns
                },
            }
            for index, (label, value, row) in enumerate(paired, 1)
        ]
        ranking_shortfall = max(0, (limit or len(rankings)) - len(rankings))
        shortfall_text = (
            f"> 当前仅有 {len(rankings)} 个对象满足全部筛选条件，不足请求的前 {limit} 名。\n\n"
            if limit is not None and ranking_shortfall
            else ""
        )
        source_columns = ["排名", label_column, metric_column, *profile_columns]
        table_columns = [
            column if column == "排名" else self._display_column_name(column)
            for column in source_columns
        ]
        table_lines = [
            "| " + " | ".join(table_columns) + " |",
            "| " + " | ".join("---" for _ in table_columns) + " |",
        ]
        for item in rankings:
            table_lines.append("| " + " | ".join([
                self._table_cell(item["rank"]),
                self._table_cell(item["label"]),
                self._table_cell(item["value"], percentage=is_coverage),
                *(
                    self._table_cell(item["profile"].get(column))
                    for column in profile_columns
                ),
            ]) + " |")
        answer = (
            shortfall_text
            + f"{self._display_column_name(label_column)}按"
            f"{self._display_column_name(metric_column)}"
            f"{'降序' if descending else '升序'}排名，共 {len(rankings)} 条。\n\n"
            + "\n".join(table_lines)
        )
        return AnalysisOutput(answer, (
            "validated_top_n_ranking" if bounded else "validated_ordered_metric_ranking"
        ), {
            "metric_column": metric_column,
            "label_column": label_column,
            "ranking_limit": limit,
            "descending": descending,
            "rankings": rankings,
            "profile_columns": profile_columns,
            "coverage_reconciled": coverage_reconciled,
            "numerator_column": numerator_column,
            "denominator_column": denominator_column,
            "ranking_mode": "BOUNDED_TOP_N" if bounded else "FULL_ORDERED_SET",
            "returned_object_count": len(rankings),
            "requested_object_count": limit,
            "ranking_shortfall": ranking_shortfall,
        })

    @classmethod
    def _ranking_label_column(
        cls,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        metric_column: str,
    ) -> str:
        non_metrics = [column for column in columns if column != metric_column]
        scored: list[tuple[int, str]] = []
        for column in non_metrics:
            column_key = cls._object_key(column).replace("_", "")
            leaf_key = column_key.rsplit(".", 1)[-1]
            if any(token in column_key for token in ("id", "code", "编号", "编码", "序号")):
                continue
            if any(token in leaf_key for token in (
                "等级", "级别", "level", "grade", "省份", "城市", "province", "city",
            )):
                continue
            score = 0
            for dimension in request.dimensions:
                dimension_key = cls._object_key(dimension).replace("_", "")
                if column_key == dimension_key:
                    score = max(score, 10)
                elif column_key in {f"{dimension_key}名称", f"{dimension_key}name"}:
                    score = max(score, 9)
                elif dimension_key and dimension_key in column_key:
                    score = max(score, 6)
            if any(token in column_key for token in (
                "经销商", "供应商", "dealer", "supplier", "vendor",
                "门店", "区域", "渠道",
            )):
                score = max(score, 3)
            # Physical result columns commonly carry the entity prefix on all
            # attributes (for example ``dealer.dealer_name``,
            # ``dealer.province`` and ``dealer.city``).  The prefix proves
            # entity membership, but only the name-like leaf is a safe object
            # label.  Prefer it without guessing when two name columns remain.
            if any(token in leaf_key for token in ("name", "名称", "姓名")):
                score = max(score, 5)
            if score:
                scored.append((score, column))
        if not scored and len(non_metrics) == 1:
            return non_metrics[0]
        if not scored:
            raise AnalysisError("排名结果缺少与分组维度对应的唯一对象列")
        highest = max(score for score, _ in scored)
        best = [column for score, column in scored if score == highest]
        if len(best) != 1:
            raise AnalysisError("排名结果存在多个对象标签列，无法安全确定排名对象")
        return best[0]

    @staticmethod
    def _validate_shape(columns: list[str], rows: list[dict[str, Any]]) -> None:
        if not rows:
            raise AnalysisError("查询结果为空，无法执行分析")
        if len(columns) != len(set(columns)) or not all(isinstance(c, str) and c for c in columns):
            raise AnalysisError("数据列定义无效")
        declared = set(columns)
        if any(not isinstance(row, dict) or not set(row).issubset(declared) for row in rows):
            raise AnalysisError("数据行与列定义不一致")

    @classmethod
    def _series(
        cls, request: CanonicalAnalysisRequest, columns: list[str], rows: list[dict[str, Any]]
    ) -> tuple[str, list[float]]:
        preferred: list[str] = []
        for metric in request.metrics:
            preferred.extend(
                value for value in (metric.canonical_name, metric.input, metric.metric_id) if value
            )
        candidates = [
            column
            for column in columns
            if any(cls._number(row.get(column)) is not None for row in rows)
        ]
        metric_column = next((name for name in preferred if name in candidates), None)
        if metric_column is None:
            non_index = [
                name for name in candidates
                if not any(
                    token in name.lower()
                    for token in (
                        "id", "编号", "序号", "排名", "日期", "时间", "月份",
                        "季度", "年份", "year", "month", "date", "time", "period",
                    )
                )
            ]
            if len(non_index) > 1:
                raise AnalysisError(
                    "存在多个数值指标列，ASL/SQL结果必须明确指标列，不能自动猜测"
                )
            metric_column = non_index[0] if non_index else None
        if metric_column is None:
            raise AnalysisError("没有找到可计算的数值指标列")
        values = [cls._number(row.get(metric_column)) for row in rows]
        if any(value is None for value in values):
            raise AnalysisError(f"指标列“{metric_column}”包含空值或非数值，不能静默忽略")
        return metric_column, [float(value) for value in values if value is not None]

    @staticmethod
    def _number(value: Any) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, Decimal):
            return value if value.is_finite() else None
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(str(value)) if math.isfinite(value) else None
        if isinstance(value, str):
            normalized = value.strip().replace(",", "").replace("，", "")
            percent = normalized.endswith("%")
            if percent:
                normalized = normalized[:-1]
            try:
                result = Decimal(normalized)
            except InvalidOperation:
                return None
            return result / 100 if percent else result
        return None

    @staticmethod
    def _labels(columns: list[str], rows: list[dict[str, Any]], metric_column: str) -> list[str]:
        label_column = next((column for column in columns if column != metric_column), None)
        if label_column is None:
            raise AnalysisError("分析结果缺少分组或时间标签列，无法解释各行的业务含义")
        labels = [str(row.get(label_column, "")).strip() for row in rows]
        if any(not label for label in labels):
            raise AnalysisError(f"标签列“{label_column}”包含空值，无法可靠解释结果")
        return labels

    @classmethod
    def _temporal_labels(
        cls, columns: list[str], rows: list[dict[str, Any]], metric_column: str
    ) -> list[str]:
        time_column = next(
            (
                column for column in columns if column != metric_column and any(
                    token in column.lower()
                    for token in ("日期", "时间", "月份", "季度", "年份", "周期", "周", "date", "time", "month", "year", "quarter", "period", "week")
                )
            ),
            None,
        )
        if time_column is None:
            raise AnalysisError("趋势或预测结果缺少明确的时间列")
        labels = [str(row.get(time_column, "")).strip() for row in rows]
        keys = [cls._time_key(value) for value in labels]
        if any(key is None for key in keys):
            raise AnalysisError(f"时间列“{time_column}”包含无法验证顺序的值")
        sortable = [key for key in keys if key is not None]
        if len(sortable) != len(set(sortable)):
            raise AnalysisError("时间序列包含重复时间点")
        if sortable != sorted(sortable):
            raise AnalysisError("时间序列未按升序返回，拒绝在未知顺序上计算趋势")
        return labels

    @staticmethod
    def _time_key(value: str) -> tuple[int, int, int] | None:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.year, parsed.month, parsed.day
        except ValueError:
            pass
        if match := re.fullmatch(r"(20\d{2})-?[Ww](\d{2})", text):
            try:
                week_start = date.fromisocalendar(
                    int(match.group(1)), int(match.group(2)), 1
                )
            except ValueError:
                return None
            return week_start.year, week_start.month, week_start.day
        if match := re.fullmatch(r"(20\d{2})[-/.年](\d{1,2})(?:月)?", text):
            month = int(match.group(2))
            return (int(match.group(1)), month, 1) if 1 <= month <= 12 else None
        if match := re.fullmatch(r"(20\d{2})(?:年)?[Qq]([1-4])", text):
            return int(match.group(1)), (int(match.group(2)) - 1) * 3 + 1, 1
        if match := re.fullmatch(r"(20\d{2})年?第?([一二三四1234])季度", text):
            value = match.group(2)
            quarter = int(value) if value.isdigit() else {"一": 1, "二": 2, "三": 3, "四": 4}[value]
            return int(match.group(1)), (quarter - 1) * 3 + 1, 1
        if match := re.fullmatch(r"(\d{1,2})月", text):
            month = int(match.group(1))
            return (0, month, 1) if 1 <= month <= 12 else None
        if match := re.fullmatch(r"(20\d{2})年", text):
            return int(match.group(1)), 1, 1
        return None

    @staticmethod
    def _unique_labels(labels: list[str]) -> None:
        if len(labels) != len(set(labels)):
            raise AnalysisError("分组标签重复，需先由查询服务聚合为唯一分组")

    @staticmethod
    def _pct_change(start: float, end: float) -> float | None:
        return None if start == 0 else (end - start) / abs(start)

    @classmethod
    def _temporal_continuity(cls, labels: list[str]) -> dict[str, Any]:
        normalized = [value.strip() for value in labels]
        if all(re.fullmatch(r"20\d{2}-?[Ww]\d{2}", value) for value in normalized):
            grain, expected = "week", 7
            positions = [date(*cls._time_key(value)).toordinal() for value in normalized]
        elif all(re.search(r"[Qq]|季度", value) for value in normalized):
            grain, expected = "quarter", 3
            positions = [key[0] * 12 + key[1] for value in normalized if (key := cls._time_key(value))]
        elif all(
            re.fullmatch(r"(?:20\d{2}[-/.年])?\d{1,2}(?:月)?", value)
            for value in normalized
        ):
            grain, expected = "month", 1
            positions = [key[0] * 12 + key[1] for value in normalized if (key := cls._time_key(value))]
        elif all(re.fullmatch(r"20\d{2}年?", value) for value in normalized):
            grain, expected = "year", 1
            positions = [key[0] for value in normalized if (key := cls._time_key(value))]
        else:
            grain, expected = "day", 1
            positions = [date(*cls._time_key(value)).toordinal() for value in normalized]
        gaps = [
            {"from": labels[index - 1], "to": labels[index], "intervals": (positions[index] - positions[index - 1]) / expected}
            for index in range(1, len(positions))
            if positions[index] - positions[index - 1] != expected
        ]
        return {"grain": grain, "is_complete": not gaps, "gaps": gaps}

    @staticmethod
    def _requested_trend_granularity(question: str) -> str | None:
        """Return only an explicitly requested output grain.

        Relative ranges such as ``近一年`` describe the window, not the
        grouping grain, so they deliberately do not match this guard.
        """
        patterns = (
            (r"按(?:天|日)|逐日|每天|每日", "day"),
            (r"按周|逐周|每周|周度", "week"),
            (r"按月|逐月|每月|月度", "month"),
            (r"按季|逐季|每季|季度", "quarter"),
            (r"按年|逐年|每年|年度", "year"),
        )
        compact = "".join(str(question or "").split())
        return next(
            (grain for pattern, grain in patterns if re.search(pattern, compact)),
            None,
        )

    def _trend(self, request, columns, rows, knowledge) -> AnalysisOutput:
        metric, values = self._series(request, columns, rows)
        aggregated_multidimensional = False
        time_column = next(
            (
                column for column in columns if column != metric and any(
                    token in column.lower()
                    for token in (
                        "日期", "时间", "月份", "季度", "年份", "周期", "周",
                        "date", "time", "month", "year", "quarter", "period", "week",
                    )
                )
            ),
            None,
        )
        if time_column is not None:
            raw_labels = [str(row.get(time_column, "")).strip() for row in rows]
            if len(raw_labels) != len(set(raw_labels)):
                # Multiple requested business dimensions legitimately produce
                # several rows per month. Build the overall trend from the sum
                # at each time point; the untouched query rows remain available
                # to the response/table layer for dimensional inspection.
                totals: dict[str, float] = {}
                for row in rows:
                    label = str(row.get(time_column, "")).strip()
                    value = self._number(row.get(metric))
                    if not label or value is None:
                        raise AnalysisError("多维趋势结果包含空时间或非数值指标")
                    totals[label] = totals.get(label, 0.0) + float(value)
                ordered = sorted(
                    totals,
                    key=lambda label: self._time_key(label) or (9999, 12, 31),
                )
                rows = [
                    {time_column: label, metric: totals[label]} for label in ordered
                ]
                columns = [time_column, metric]
                values = [totals[label] for label in ordered]
                aggregated_multidimensional = True
        if len(values) < 2:
            raise AnalysisError("趋势分析至少需要两个有序时间点")
        labels = self._temporal_labels(columns, rows, metric)
        continuity = self._temporal_continuity(labels)
        requested_granularity = self._requested_trend_granularity(
            request.original_question
        )
        if (
            requested_granularity is not None
            and continuity["grain"] != requested_granularity
        ):
            raise AnalysisError(
                "趋势结果粒度与用户明确要求不一致："
                f"要求 {requested_granularity}，实际 {continuity['grain']}"
            )
        change = values[-1] - values[0]
        pct = self._pct_change(values[0], values[-1])
        trend_diagnostics = self._trend_diagnostics(values)
        deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
        change_points = [
            {
                "label": labels[index],
                "before_change": deltas[index - 1],
                "after_change": deltas[index],
                "type": "PEAK" if deltas[index - 1] > 0 > deltas[index] else "TROUGH",
            }
            for index in range(1, len(deltas))
            if deltas[index - 1] * deltas[index] < 0
        ]
        direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
        period_changes = []
        for index in range(1, len(values)):
            delta = values[index] - values[index - 1]
            period_changes.append({
                "from": labels[index - 1],
                "to": labels[index],
                "absolute_change": delta,
                "change_rate": self._pct_change(values[index - 1], values[index]),
            })
        largest_rise = max(period_changes, key=lambda item: item["absolute_change"])
        largest_drop = min(period_changes, key=lambda item: item["absolute_change"])
        if all(item["absolute_change"] >= 0 for item in period_changes):
            pattern = "持续上升"
        elif all(item["absolute_change"] <= 0 for item in period_changes):
            pattern = "持续下降"
        else:
            pattern = "存在波动"
        number = self._display_number
        answer = f"{metric}从 {labels[0]} 的 {number(values[0])} 到 {labels[-1]} 的 {number(values[-1])}，整体{direction} {number(abs(change))}"
        answer += "。" if pct is None else f"（{abs(pct):.2%}）。"
        answer += f"区间最低 {number(min(values))}，最高 {number(max(values))}，走势表现为{pattern}。"
        if largest_rise["absolute_change"] > 0:
            answer += (
                f"最大单期上升发生在 {largest_rise['from']} 至 "
                f"{largest_rise['to']}，增加 {number(largest_rise['absolute_change'])}。"
            )
        if largest_drop["absolute_change"] < 0:
            answer += (
                f"最大单期下降发生在 {largest_drop['from']} 至 "
                f"{largest_drop['to']}，减少 {number(abs(largest_drop['absolute_change']))}。"
            )
            later_recovery = sum(
                max(0.0, item["absolute_change"])
                for item in period_changes
                if labels.index(item["from"]) >= labels.index(largest_drop["to"])
            )
            if change < 0:
                answer += (
                    "从数值贡献看，整体下降主要集中在上述最大下滑区间，"
                    + (
                        f"后续回升合计 {number(later_recovery)}，仍不足以抵消该阶段降幅。"
                        if later_recovery > 0
                        else "之后未出现足以抵消该阶段降幅的正向变化。"
                    )
                )
        robust_direction = {
            "UP": "上升",
            "DOWN": "下降",
            "FLAT": "基本持平",
        }[trend_diagnostics["direction"]]
        answer += (
            f"稳健斜率判断为{robust_direction}，"
            f"方向一致性{trend_diagnostics['direction_consistency']:.2%}。"
        )
        if aggregated_multidimensional:
            answer = "按时间汇总各业务维度后，" + answer
        warnings = []
        if not continuity["is_complete"]:
            answer += f"时间序列存在 {len(continuity['gaps'])} 处缺口，趋势结论需谨慎解释。"
            warnings.append("时间序列不连续，变化率跨越的实际周期数可能不同")
        if change_points:
            answer += "方向转折点：" + "、".join(
                f"{item['label']}（{'峰值' if item['type'] == 'PEAK' else '谷值'}）"
                for item in change_points[:5]
            ) + "。"
        answer += (
            "以上是时间序列能够验证的变化解释；仅凭汇总趋势无法确定业务因果原因，"
            "如需进一步归因，应继续按产品、经销商、医院或地区拆分核验。"
        )
        return AnalysisOutput(answer, "robust_trend_diagnostics", {
            "metric_column": metric, "start": values[0], "end": values[-1],
            "absolute_change": change, "change_rate": pct, "minimum": min(values),
            "maximum": max(values), "point_count": len(values), "pattern": pattern,
            "period_changes": period_changes, "largest_rise": largest_rise,
            "largest_drop": largest_drop, "trend_diagnostics": trend_diagnostics,
            "temporal_continuity": continuity,
            "requested_granularity": requested_granularity,
            "actual_granularity": continuity["grain"],
            "change_points": change_points,
            "multidimensional_time_aggregation": aggregated_multidimensional,
        }, warnings)

    @staticmethod
    def _trend_diagnostics(values: list[float]) -> dict[str, Any]:
        """Robust trend decision using pairwise slopes and movement consistency."""
        slopes = [
            (values[right] - values[left]) / (right - left)
            for left in range(len(values) - 1)
            for right in range(left + 1, len(values))
        ]
        robust_slope = statistics.median(slopes)
        scale = max(statistics.median(abs(value) for value in values), 1e-12)
        normalized_slope = robust_slope / scale
        deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
        non_zero = [delta for delta in deltas if delta != 0]
        direction_consistency = (
            1.0
            if not non_zero
            else max(
                sum(delta > 0 for delta in non_zero),
                sum(delta < 0 for delta in non_zero),
            )
            / len(non_zero)
        )
        neutral_threshold = 0.005
        direction = (
            "UP"
            if normalized_slope > neutral_threshold
            else "DOWN"
            if normalized_slope < -neutral_threshold
            else "FLAT"
        )
        mean_value = statistics.mean(values)
        coefficient_of_variation = (
            None if mean_value == 0 else statistics.pstdev(values) / abs(mean_value)
        )
        return {
            "decision_method": "theil_sen_pairwise_slope",
            "direction": direction,
            "robust_slope_per_period": robust_slope,
            "normalized_slope": normalized_slope,
            "neutral_threshold": neutral_threshold,
            "direction_consistency": direction_consistency,
            "coefficient_of_variation": coefficient_of_variation,
        }

    def _comparison(self, request, columns, rows, knowledge) -> AnalysisOutput:
        object_scope = explicit_object_comparison_scope(request)
        requested_names = [
            value
            for metric in request.metrics
            for value in (metric.canonical_name, metric.input, metric.metric_id)
            if value
        ]
        metric_columns = list(dict.fromkeys(
            name for name in requested_names if name in columns
        ))
        if object_scope is not None:
            return self._explicit_object_comparison(
                request, columns, rows, metric_columns, object_scope
            )
        if len(metric_columns) > 1:
            if len(rows) != 2:
                raise AnalysisError(
                    "多指标对比结果必须明确返回且仅返回基期与对比期两行"
                )
            label_column = next(
                (column for column in columns if column not in metric_columns), None
            )
            if label_column is None:
                raise AnalysisError("多指标对比结果缺少期间或对象标签列")
            labels = [str(row.get(label_column, "")).strip() for row in rows]
            if any(not label for label in labels):
                raise AnalysisError(f"标签列“{label_column}”包含空值")
            self._unique_labels(labels)
            base_index, current_index, order_source = self._comparison_order(
                columns, rows, metric_columns[0], labels
            )
            comparisons: list[dict[str, Any]] = []
            fragments: list[str] = []
            for metric_column in metric_columns:
                values = [self._number(row.get(metric_column)) for row in rows]
                if any(value is None for value in values):
                    raise AnalysisError(f"指标列“{metric_column}”包含空值或非数值")
                base = float(values[base_index])
                current = float(values[current_index])
                change = current - base
                pct = self._pct_change(base, current)
                comparisons.append({
                    "metric_column": metric_column,
                    "base": base,
                    "current": current,
                    "absolute_change": change,
                    "change_rate": pct,
                })
                rate_text = "基期为0" if pct is None else f"{pct:+.2%}"
                fragments.append(
                    f"{metric_column} {base:g}→{current:g}，变化{change:+g}（{rate_text}）"
                )
            largest = max(
                comparisons,
                key=lambda item: abs(item["change_rate"])
                if item["change_rate"] is not None
                else float("-inf"),
            )
            answer = "；".join(fragments)
            if "变化最大" in request.original_question:
                answer += f"；按变化率绝对值，{largest['metric_column']}变化最大。"
            return AnalysisOutput(answer, "two_group_multi_metric_comparison", {
                "base_label": labels[base_index],
                "current_label": labels[current_index],
                "period_order_source": order_source,
                "comparisons": comparisons,
                "largest_change_metric": largest["metric_column"],
            })

        metric, values = self._series(request, columns, rows)
        if len(values) < 2:
            raise AnalysisError("对比分析至少需要两个可比对象或期间")
        if len(values) != 2:
            raise AnalysisError("对比结果必须明确返回且仅返回基期与对比期两行，避免按未知顺序取值")
        labels = self._labels(columns, rows, metric)
        self._unique_labels(labels)
        base_index, current_index, order_source = self._comparison_order(
            columns, rows, metric, labels
        )
        base, current = values[base_index], values[current_index]
        base_label, current_label = labels[base_index], labels[current_index]
        change, pct = current - base, self._pct_change(base, current)
        relation = "高" if change > 0 else "低" if change < 0 else "相同"
        answer = f"{current_label}的{metric}为 {current:g}，较{base_label}的 {base:g}{relation} {abs(change):g}"
        answer += "；基期为0，无法计算变化率。" if pct is None else f"（{abs(pct):.2%}）。"
        return AnalysisOutput(answer, "two_group_comparison", {
            "metric_column": metric, "base_label": base_label, "current_label": current_label,
            "base": base, "current": current, "absolute_change": change, "change_rate": pct,
            "period_order_source": order_source,
        })

    def _explicit_object_comparison(
        self,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        metric_columns: list[str],
        scope: ExplicitObjectComparisonScope,
    ) -> AnalysisOutput:
        """Compare a finite user-named object set without inventing a baseline."""

        label_column = self._explicit_object_label_column(
            request, columns, rows, metric_columns, scope
        )
        labels = [str(row.get(label_column, "")).strip() for row in rows]
        if any(not label for label in labels):
            raise AnalysisError(f"对象标签列“{label_column}”包含空值")
        self._unique_labels(labels)

        requested_by_key = {
            self._object_key(value): value for value in scope.requested_objects
        }
        returned_by_key = {self._object_key(value): value for value in labels}
        unexpected = [
            value for key, value in returned_by_key.items()
            if key not in requested_by_key
        ]
        if unexpected:
            raise AnalysisError(
                "查询结果包含未被用户指定的对象：" + "、".join(unexpected)
            )
        missing = [
            value for key, value in requested_by_key.items()
            if key not in returned_by_key
        ]
        if len(returned_by_key) < scope.minimum_returned_objects:
            detail = f"；未返回：{'、'.join(missing)}" if missing else ""
            raise AnalysisError(
                f"对象间比较至少需要 {scope.minimum_returned_objects} 个唯一对象，"
                f"当前仅返回 {len(returned_by_key)} 个{detail}"
            )

        resolved_metrics = self._explicit_object_metric_columns(
            request, columns, rows, label_column
        )

        row_by_key = {
            self._object_key(str(row.get(label_column, "")).strip()): row
            for row in rows
        }
        ordered_rows = [
            row_by_key[key] for key in requested_by_key if key in row_by_key
        ]
        objects: list[dict[str, Any]] = []
        fragments: list[str] = []
        for row in ordered_rows:
            label = str(row[label_column]).strip()
            metric_values: dict[str, float] = {}
            for metric in resolved_metrics:
                value = self._number(row.get(metric))
                if value is None:
                    raise AnalysisError(
                        f"指标列“{metric}”包含空值或非数值，不能静默忽略"
                    )
                metric_values[metric] = float(value)
            objects.append({"label": label, "metrics": metric_values})
            fragments.append(
                f"{label}：" + "，".join(
                    f"{metric}={self._display_number(value)}"
                    for metric, value in metric_values.items()
                )
            )

        summaries: list[dict[str, Any]] = []
        summary_fragments: list[str] = []
        for metric in resolved_metrics:
            observations = [
                (item["label"], item["metrics"][metric]) for item in objects
            ]
            maximum = max(value for _, value in observations)
            minimum = min(value for _, value in observations)
            highest = [label for label, value in observations if value == maximum]
            lowest = [label for label, value in observations if value == minimum]
            summaries.append({
                "metric_column": metric,
                "highest_labels": highest,
                "highest_value": maximum,
                "lowest_labels": lowest,
                "lowest_value": minimum,
                "range": maximum - minimum,
            })
            summary_fragments.append(
                f"{metric}最高为{'、'.join(highest)}（{self._display_number(maximum)}），"
                f"最低为{'、'.join(lowest)}（{self._display_number(minimum)}）"
            )

        answer = (
            f"已对返回的 {len(objects)} 个对象做描述性横向比较："
            + "；".join(fragments)
            + "。"
            + "；".join(summary_fragments)
            + "。"
        )
        warnings: list[str] = []
        if missing:
            warning = (
                f"用户指定了 {len(scope.requested_objects)} 个对象，但数据仅返回 "
                f"{len(objects)} 个；未返回对象：{'、'.join(missing)}；"
                "未返回不等同于指标为0"
            )
            warnings.append(warning)
        return AnalysisOutput(
            answer,
            "explicit_object_descriptive_comparison",
            {
                "label_column": label_column,
                "metric_columns": resolved_metrics,
                "requested_object_count": len(scope.requested_objects),
                "returned_object_count": len(objects),
                "requested_objects": list(scope.requested_objects),
                "missing_objects": missing,
                "objects": objects,
                "metric_summaries": summaries,
                "comparison_mode": "DESCRIPTIVE_NO_BASELINE",
            },
            warnings,
        )

    @classmethod
    def _explicit_object_label_column(
        cls,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        metric_columns: list[str],
        scope: ExplicitObjectComparisonScope,
    ) -> str:
        non_metrics = [column for column in columns if column not in metric_columns]
        preferred = [scope.filter_field]
        for dimension in request.dimensions:
            preferred.extend((dimension, f"{dimension}名称", f"{dimension}_name"))
        exact = next((name for name in preferred if name in non_metrics), None)
        if exact is not None:
            return exact

        requested = {cls._object_key(value) for value in scope.requested_objects}
        scored: list[tuple[int, str]] = []
        for column in non_metrics:
            labels = [str(row.get(column, "")).strip() for row in rows]
            if not labels or any(not value for value in labels):
                continue
            match_count = sum(cls._object_key(value) in requested for value in labels)
            if match_count:
                scored.append((match_count, column))
        if not scored:
            raise AnalysisError("对象间比较结果缺少可与用户指定对象核对的标签列")
        highest = max(score for score, _ in scored)
        best = [column for score, column in scored if score == highest]
        if len(best) != 1:
            raise AnalysisError("对象间比较结果存在多个候选标签列，无法安全确定对象字段")
        return best[0]

    @classmethod
    def _explicit_object_metric_columns(
        cls,
        request: CanonicalAnalysisRequest,
        columns: list[str],
        rows: list[dict[str, Any]],
        label_column: str,
    ) -> list[str]:
        missing: list[str] = []
        selected: list[str] = []
        for metric in request.metrics:
            names = [
                value for value in (
                    metric.canonical_name, metric.input, metric.metric_id
                ) if value
            ]
            column = next((name for name in names if name in columns), None)
            if column is None:
                missing.append(names[0] if names else "未命名指标")
            elif column not in selected:
                selected.append(column)
        if missing:
            raise AnalysisError(
                "对象间比较结果缺少请求指标列：" + "、".join(missing)
            )
        if not selected:
            numeric = [
                column for column in columns if column != label_column
                and all(cls._number(row.get(column)) is not None for row in rows)
            ]
            if len(numeric) != 1:
                raise AnalysisError("对象间比较必须明确返回需要比较的指标列")
            selected = numeric
        return selected

    @staticmethod
    def _object_key(value: str) -> str:
        return re.sub(r"\s+", "", value).casefold()

    @classmethod
    def _comparison_order(
        cls,
        columns: list[str],
        rows: list[dict[str, Any]],
        metric_column: str,
        labels: list[str],
    ) -> tuple[int, int, str]:
        role_column = next(
            (
                column for column in columns
                if column != metric_column
                and column.strip().casefold() in {
                    "period_role", "comparison_role", "期间角色", "对比角色"
                }
            ),
            None,
        )
        base_roles = {"base", "baseline", "基期", "上期", "对照期"}
        current_roles = {"current", "comparison", "当前期", "本期", "对比期"}
        if role_column is not None:
            roles = [str(row.get(role_column, "")).strip().casefold() for row in rows]
            base_indexes = [index for index, value in enumerate(roles) if value in base_roles]
            current_indexes = [index for index, value in enumerate(roles) if value in current_roles]
            if len(base_indexes) == len(current_indexes) == 1:
                return base_indexes[0], current_indexes[0], "period_role"
            raise AnalysisError("period_role 必须且只能包含一行 BASE/基期和一行 CURRENT/当前期")

        label_roles: list[str | None] = []
        for label in labels:
            normalized = label.strip().casefold()
            label_roles.append(
                "BASE" if normalized in base_roles
                else "CURRENT" if normalized in current_roles
                else None
            )
        if label_roles.count("BASE") == label_roles.count("CURRENT") == 1:
            return label_roles.index("BASE"), label_roles.index("CURRENT"), "label_role"

        time_keys = [cls._time_key(label) for label in labels]
        if all(key is not None for key in time_keys) and time_keys[0] != time_keys[1]:
            assert time_keys[0] is not None and time_keys[1] is not None
            return (0, 1, "time_order") if time_keys[0] < time_keys[1] else (1, 0, "time_order")
        raise AnalysisError(
            "对比结果无法确认基期与当前期；请查询服务返回 period_role，或返回可排序的两个时间标签"
        )

    def _composition(self, request, columns, rows, knowledge) -> AnalysisOutput:
        metric, values = self._series(request, columns, rows)
        if any(value < 0 for value in values):
            raise AnalysisError("占比计算不接受负值，需先确认指标口径")
        total = sum(values)
        if total <= 0:
            raise AnalysisError("合计为0，无法计算占比")
        labels = self._labels(columns, rows, metric)
        self._unique_labels(labels)
        shares = sorted(
            ({"label": label, "value": value, "share": value / total} for label, value in zip(labels, values)),
            key=lambda item: item["share"], reverse=True,
        )
        top = shares[:5]
        answer = f"{metric}合计 {self._display_number(total)}。" + "；".join(
            f"{item['label']}占 {item['share']:.2%}" for item in top
        ) + "。"
        return AnalysisOutput(answer, "share_of_total", {
            "metric_column": metric, "total": total, "shares": shares, "group_count": len(shares),
        })

    def _anomaly(self, request, columns, rows, knowledge) -> AnalysisOutput:
        metric, values = self._series(request, columns, rows)
        if len(values) < 5:
            raise AnalysisError("异常检测至少需要5个有效观测点")
        labels = self._labels(columns, rows, metric)
        median = statistics.median(values)
        deviations = [abs(value - median) for value in values]
        mad = statistics.median(deviations)
        if mad == 0:
            relative_floor = max(abs(median) * 0.5, 1e-9)
            candidates = [
                {"label": label, "value": value}
                for label, value in zip(labels, values)
                if abs(value - median) >= relative_floor
            ]
            method = "median_relative_deviation_fallback"
        else:
            candidates = [
                {"label": label, "value": value, "robust_z": 0.6745 * (value - median) / mad}
                for label, value in zip(labels, values)
                if abs(0.6745 * (value - median) / mad) >= 3.5
            ]
            method = "median_absolute_deviation"
        answer = "未发现达到稳健阈值的异常点。" if not candidates else (
            f"发现 {len(candidates)} 个异常候选：" + "；".join(
                f"{item['label']}={self._display_number(item['value'])}" for item in candidates[:10]
            ) + "。异常只代表统计偏离，仍需结合业务事件核实。"
        )
        knowledge_candidates = [doc.content[:300] for doc in knowledge.documents[:3]]
        if candidates and knowledge_candidates:
            answer += (
                "知识库中的待核实背景包括："
                + "；".join(knowledge_candidates)
                + "。这些背景不能单独证明异常原因。"
            )
        return AnalysisOutput(answer, method, {
            "metric_column": metric, "median": median, "mad": mad,
            "candidate_count": len(candidates), "candidates": candidates[:50],
            "point_count": len(values), "knowledge_candidates": knowledge_candidates,
        })

    def _root_cause(self, request, columns, rows, knowledge) -> AnalysisOutput:
        contribution_columns = [
            column for column in columns
            if any(signal in column.lower() for signal in ("贡献", "变化", "差额", "影响", "增量", "delta"))
            and not any(
                total_signal in column.lower()
                for total_signal in ("总变化", "整体变化", "总差额", "total_change")
            )
        ]
        if not contribution_columns:
            raise AnalysisError("归因查询必须返回按维度拆解的贡献值或变化量，不能用指标水平值冒充原因")
        if len(contribution_columns) > 1:
            requested_names = {
                name.strip().casefold()
                for metric_ref in request.metrics
                for name in (metric_ref.input, metric_ref.canonical_name or "")
                if name and name.strip()
            }
            matched = [
                column for column in contribution_columns
                if any(name in column.casefold() for name in requested_names)
            ]
            if len(matched) != 1:
                raise AnalysisError(
                    "归因结果包含多个贡献指标，无法安全判断应使用哪一列："
                    + "、".join(contribution_columns)
                )
            contribution_columns = matched
        narrowed_request = request.model_copy(deep=True)
        narrowed_request.metrics = [MetricRef(input=contribution_columns[0])]
        metric, values = self._series(narrowed_request, columns, rows)
        labels = self._labels(columns, rows, metric)
        self._unique_labels(labels)
        absolute_total = sum(abs(value) for value in values)
        if absolute_total == 0:
            raise AnalysisError("所有维度贡献均为0，无法形成归因排序")
        ranked = sorted(zip(labels, values), key=lambda item: abs(item[1]), reverse=True)
        drags = [(label, value) for label, value in ranked if value < 0]
        lifts = [(label, value) for label, value in ranked if value > 0]
        contribution_sum = sum(values)
        answer = f"维度贡献合计为 {contribution_sum:g}。"
        if drags:
            answer += "主要拖累项：" + "；".join(
                f"{label}={self._display_number(value)}（绝对贡献份额 {abs(value) / absolute_total:.2%}）"
                for label, value in drags[:5]
            ) + "。"
        if lifts:
            answer += "主要拉升或抵消项：" + "；".join(
                f"{label}=+{self._display_number(value)}（绝对贡献份额 {abs(value) / absolute_total:.2%}）"
                for label, value in lifts[:5]
            ) + "。"
        answer += "这些是数据验证的贡献驱动，但不足以证明因果关系。"

        total_change_column = next(
            (
                column for column in columns
                if any(
                    signal in column.lower()
                    for signal in ("总变化", "整体变化", "总差额", "total_change")
                )
            ),
            None,
        )
        observed_change = None
        residual = None
        coverage = None
        if total_change_column:
            total_values = [self._number(row.get(total_change_column)) for row in rows]
            if all(value is not None for value in total_values):
                unique_totals = {float(value) for value in total_values if value is not None}
                if len(unique_totals) == 1:
                    observed_change = unique_totals.pop()
                    residual = observed_change - contribution_sum
                    coverage = (
                        1.0
                        if observed_change == 0 and residual == 0
                        else max(
                            0.0,
                            1 - abs(residual) / max(abs(observed_change), 1e-12),
                        )
                    )
                    answer += (
                        f"与整体变化 {observed_change:g} 对账后，未解释残差为 "
                        f"{residual:g}，贡献覆盖度为 {coverage:.2%}。"
                    )

        matched_knowledge = []
        unmatched_knowledge = []
        for document in knowledge.documents[:5]:
            matched_labels = [
                label for label, _ in ranked
                if len(label.strip()) >= 2
                and re.search(re.escape(label), document.content, flags=re.IGNORECASE)
            ]
            item = {
                "content": document.content[:300],
                "source": document.source,
            }
            if matched_labels:
                item["matched_labels"] = matched_labels
                matched_knowledge.append(item)
            else:
                unmatched_knowledge.append(item)
        if matched_knowledge:
            answer += "知识库中与贡献项直接匹配的待核实解释：" + "；".join(
                item["content"] for item in matched_knowledge[:3]
            ) + "。"
        if unmatched_knowledge:
            answer += "另有知识候选尚未被当前数据验证，不能作为已确认原因。"

        warnings = ["归因结果是贡献驱动而非因果证明"]
        reconciliation = {
            "status": "unknown",
            "tolerance": 0.02,
            "residual": residual,
            "residual_pct": None,
        }
        if observed_change is None:
            warnings.append("上游未提供整体变化，无法校验贡献覆盖度和残差")
        else:
            scale = max(abs(observed_change), absolute_total, 1e-12)
            residual_pct = abs(residual or 0.0) / scale
            reconciliation.update({
                "status": "passed" if residual_pct <= 0.02 else "failed",
                "residual_pct": residual_pct,
            })
            if observed_change == 0 and any(value != 0 for value in values):
                warnings.append("整体变化接近0但分项存在相互抵消，只能解释绝对变化，不能解释贡献百分比")
            if reconciliation["status"] == "failed":
                warnings.append("分项贡献无法与整体变化对账，禁止将贡献率解释为完整可加分解")
            elif coverage is not None and coverage < 0.8:
                warnings.append("贡献覆盖度较低，仍有较大未解释残差")
        return AnalysisOutput(answer, "ranked_contribution_candidates", {
            "metric_column": metric,
            "ranked_candidates": [
                {
                    "label": label,
                    "contribution": value,
                    "absolute_contribution_share": abs(value) / absolute_total,
                    "contribution_pct_of_total_delta": (
                        value / observed_change if observed_change not in {None, 0} else None
                    ),
                }
                for label, value in ranked
            ],
            "contribution_sum": contribution_sum,
            "observed_change": observed_change,
            "residual": residual,
            "coverage": coverage,
            "drag_count": len(drags),
            "lift_count": len(lifts),
            "matched_knowledge": matched_knowledge,
            "unmatched_knowledge_candidates": unmatched_knowledge,
            "additivity_check": reconciliation,
            "causality_established": False,
        }, warnings)

    def _forecast(self, request, columns, rows, knowledge) -> AnalysisOutput:
        try:
            metric, values = self._series(request, columns, rows)
        except AnalysisError as exc:
            raise AnalysisError(
                "预测数据缺少唯一、完整的数值指标序列",
                requirements=[AnalysisRequirement(
                    code="forecast_metric_series",
                    category="DATA",
                    description="预测需要一列已绑定指标且每期都有有限数值。",
                    action="请数据接口按时间粒度返回唯一指标列，并补齐或明确处理空值。",
                )],
            ) from exc
        if len(values) < 6:
            raise AnalysisError(
                "历史数据不足，预测至少需要6个连续、有序的历史观测点",
                requirements=[AnalysisRequirement(
                    code="forecast_minimum_history",
                    category="DATA",
                    description=f"当前只有{len(values)}期有效数据，最低需要6期，建议至少12期。",
                    action=f"请至少补充{6 - len(values)}期连续历史数据；存在季节性时应提供不少于两个完整季节周期。",
                )],
            )
        if request.forecast_horizon_periods not in {None, 1}:
            raise AnalysisError(
                "当前内置模型只允许经过验证的一步预测",
                requirements=[AnalysisRequirement(
                    code="multi_step_forecast_model",
                    category="ALGORITHM_CAPABILITY",
                    description=f"用户要求预测{request.forecast_horizon_periods}期，但当前内置模型只完成了一步滚动回测。",
                    action="请接入支持多步回测的时序模型，或将目标改为下一期预测。",
                )],
            )
        try:
            labels = self._temporal_labels(columns, rows, metric)
        except AnalysisError as exc:
            raise AnalysisError(
                "预测数据缺少可验证的有序时间轴",
                requirements=[AnalysisRequirement(
                    code="forecast_time_axis",
                    category="DATA",
                    description="每条历史记录必须有唯一、可解析、升序的时间字段。",
                    action="请提供按天、周、月、季度或年聚合的时间列，去除重复时间点并按升序返回。",
                )],
            ) from exc
        inferred_granularity, regular = assess_time_axis(labels)
        if inferred_granularity is None or not regular:
            raise AnalysisError(
                "历史时间序列存在缺期或频率不一致",
                requirements=[AnalysisRequirement(
                    code="forecast_regular_frequency",
                    category="DATA",
                    description="预测要求相邻历史点使用相同时间粒度且连续无缺期。",
                    action="请补齐缺失周期，或由数据接口返回完整时间骨架并明确缺失值处理方式。",
                )],
            )
        if request.forecast_granularity and request.forecast_granularity != inferred_granularity:
            raise AnalysisError(
                "历史数据粒度与预测目标粒度不一致",
                requirements=[AnalysisRequirement(
                    code="forecast_granularity_mismatch",
                    category="DATA",
                    description=f"预测目标按{request.forecast_granularity}，历史数据却按{inferred_granularity}返回。",
                    action="请按预测目标粒度重新聚合历史数据，或明确修改预测目标粒度。",
                )],
            )
        try:
            result = DeterministicForecastSelector().fit_predict(
                values,
                non_negative=all(value >= 0 for value in values),
            )
        except ForecastModelError as exc:
            raise AnalysisError(str(exc)) from exc

        mape_text = (
            "不可计算"
            if result.validation_mape is None
            else f"{result.validation_mape:.2%}"
        )
        answer = (
            f"算法基于 {len(values)} 个有序历史点进行滚动回测，选择 "
            f"{result.model_name} 模型。下一期 {metric} 预测值为 "
            f"{result.forecast:g}，回测残差区间为 {result.lower:g} 至 "
            f"{result.upper:g}；验证MAE={result.validation_mae:g}，"
            f"MAPE={mape_text}。"
        )
        if result.diagnostics["linear_r_squared_reference"] is None:
            answer += "历史序列为常数，线性参考R²不适用。"
        answer += "正式预测应由算法模型复核；大模型只负责总结，不参与预测决策。"
        return AnalysisOutput(
            answer=answer,
            method="deterministic_backtest_model_selection",
            facts={
                "metric_column": metric,
                "history_points": len(values),
                "history_start": labels[0],
                "history_end": labels[-1],
                "forecast_horizon": 1,
                "selected_model": result.model_name,
                "forecast": result.forecast,
                "prediction_interval": [result.lower, result.upper],
                # Backward-compatible field retained for existing API consumers.
                "residual_reference_interval": [result.lower, result.upper],
                "validation_mae": result.validation_mae,
                "validation_mape": result.validation_mape,
                "validation_points": result.validation_points,
                "candidate_scores": result.candidate_scores,
                "diagnostics": result.diagnostics,
                "r_squared": result.diagnostics["linear_r_squared_reference"],
                "slope": result.diagnostics["linear_slope_reference"],
            },
            warnings=result.warnings,
        )

    def _forecast_legacy(self, request, columns, rows, knowledge) -> AnalysisOutput:
        metric, values = self._series(request, columns, rows)
        if len(values) < 6:
            raise AnalysisError("基线预测至少需要6个连续历史观测点")
        self._temporal_labels(columns, rows, metric)
        x_values = list(range(len(values)))
        x_mean, y_mean = statistics.mean(x_values), statistics.mean(values)
        denominator = sum((x - x_mean) ** 2 for x in x_values)
        slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values)) / denominator
        intercept = y_mean - slope * x_mean
        fitted = [intercept + slope * x for x in x_values]
        rmse = math.sqrt(sum((actual - estimate) ** 2 for actual, estimate in zip(values, fitted)) / len(values))
        forecast = intercept + slope * len(values)
        if all(value >= 0 for value in values) and forecast < 0:
            raise AnalysisError("线性外推产生负预测值，与历史非负指标约束冲突")
        total_variance = sum((value - y_mean) ** 2 for value in values)
        residual_variance = sum((actual - estimate) ** 2 for actual, estimate in zip(values, fitted))
        r_squared = (
            None
            if total_variance == 0
            else 1 - residual_variance / total_variance
        )
        lower, upper = forecast - 2 * rmse, forecast + 2 * rmse
        answer = (
            f"线性趋势基线预测下一期{metric}约为 {forecast:g}，残差参考区间 {lower:g}～{upper:g}。"
            + (
                "历史序列为常数，R²不适用。"
                if r_squared is None
                else f"模型依据是历史线性斜率 {slope:g}，拟合度 R²={r_squared:.3f}。"
            )
            + "这是基线外推，不包含节假日、促销和业务事件；正式预测应由算法模型复核。"
        )
        return AnalysisOutput(answer, "linear_trend_baseline", {
            "metric_column": metric, "history_points": len(values), "forecast_horizon": 1,
            "forecast": forecast, "residual_reference_interval": [lower, upper],
            "slope": slope, "rmse": rmse, "r_squared": r_squared,
        }, [
            "基线预测不是已校准的生产时序模型",
            *(
                ["历史序列没有变化，基线只能延续常数水平，不能证明未来稳定"]
                if r_squared is None
                else (["线性趋势拟合度较低，预测不稳定"] if r_squared < 0.3 else [])
            ),
        ])

    def _report(self, request, columns, rows, knowledge) -> AnalysisOutput:
        numeric_summary: dict[str, dict[str, float]] = {}
        for column in columns:
            if any(token in column.lower() for token in ("id", "编号", "序号", "排名")):
                continue
            values = [self._number(row.get(column)) for row in rows]
            if values and all(value is not None for value in values):
                floats = [float(value) for value in values if value is not None]
                numeric_summary[column] = {
                    "sum": sum(floats), "average": statistics.mean(floats),
                    "minimum": min(floats), "maximum": max(floats),
                }
        answer = f"报表共 {len(rows)} 行、{len(columns)} 列。"
        if numeric_summary:
            answer += "数值摘要：" + "；".join(
                f"{name}合计={facts['sum']:g}、均值={facts['average']:g}"
                for name, facts in list(numeric_summary.items())[:10]
            ) + "。"
        return AnalysisOutput(answer, "descriptive_report_summary", {
            "row_count": len(rows), "columns": columns, "numeric_summary": numeric_summary,
            "discovered_insights": discover_insights(columns, rows),
            "insight_ranking_method": "bounded_impact_times_confidence",
        }, ["自动洞察是待解释的统计候选，不自动构成因果结论"])

    def _data_quality(self, request, columns, rows, knowledge) -> AnalysisOutput:
        null_counts = {
            column: sum(row.get(column) is None or row.get(column) == "" for row in rows)
            for column in columns
        }
        duplicate_count = len(rows) - len(
            {tuple((column, repr(row.get(column))) for column in columns) for row in rows}
        )
        affected = {name: count for name, count in null_counts.items() if count}
        answer = f"检查 {len(rows)} 行数据：发现 {duplicate_count} 条完全重复记录"
        answer += "，未发现空值。" if not affected else "；空值情况：" + "、".join(
            f"{name}={count}" for name, count in affected.items()
        ) + "。"
        return AnalysisOutput(answer, "null_and_duplicate_scan", {
            "row_count": len(rows), "null_counts": null_counts,
            "duplicate_row_count": duplicate_count,
        }, ["当前质量检查仅覆盖返回结果中的空值和完全重复行"])
