"""Unified, deterministic validation of query and analysis results."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any

from app.analysis.engine import AnalysisOutput
from app.domain.models import (
    CanonicalAnalysisRequest,
    Dataset,
    EvidenceItem,
    ExtensionExecution,
    ResultValidationReport,
    ValidationCheck,
)


class ResultValidator:
    """Compose existing safety facts into one auditable validation report."""

    _CAUSAL_WORDS = ("导致", "造成", "根本原因", "因为", "源于")

    def validate(
        self,
        *,
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        evidence: list[EvidenceItem],
        analysis: AnalysisOutput | None = None,
        tool_executions: list[ExtensionExecution] | None = None,
        required_tool_names: set[str] | None = None,
        final_answer: str | None = None,
    ) -> ResultValidationReport:
        checks: list[ValidationCheck] = []
        checks.extend(self._tool_checks(tool_executions or [], required_tool_names or set()))
        checks.append(self._dataset_identity_check(dataset, evidence))
        checks.append(self._empty_data_check(dataset, analysis is not None))
        checks.append(self._time_scope_check(request, dataset))
        checks.append(self._metric_scope_check(request, dataset, analysis))
        checks.append(self._finite_values_check(dataset))
        if analysis is not None:
            checks.append(self._change_rate_check(analysis.facts))
            checks.append(self._reconciliation_check(analysis.facts))
            checks.append(self._evidence_grounding_check(analysis, evidence))
            checks.append(self._causality_check(analysis, final_answer))

        errors = [item.message for item in checks if item.status == "FAIL"]
        warnings = [item.message for item in checks if item.status == "WARN"]
        status = "FAIL" if errors else "WARN" if warnings else "PASS"
        confirmed = []
        if analysis is not None and not errors:
            confirmed.append(f"确定性分析方法 {analysis.method} 已执行并通过强制校验")
        hypotheses = []
        if analysis is not None:
            hypotheses = [
                str(item.get("statement") or item.get("title") or "").strip()
                for item in analysis.facts.get("matched_knowledge", [])
                if isinstance(item, dict)
                and str(item.get("statement") or item.get("title") or "").strip()
            ][:20]
        return ResultValidationReport(
            status=status,
            checks=checks,
            confirmed_findings=confirmed,
            possible_hypotheses=hypotheses,
            warnings=warnings,
            errors=errors,
        )

    @staticmethod
    def _tool_checks(
        executions: list[ExtensionExecution], required: set[str]
    ) -> list[ValidationCheck]:
        if not executions:
            return [ValidationCheck(
                code="TOOL_EXECUTION",
                status="NOT_APPLICABLE",
                message="本次分析没有额外扩展工具执行结果需要校验",
            )]
        failed_required = [
            item.name for item in executions
            if item.name in required and item.status != "COMPLETED"
        ]
        failed_optional = [
            item.name for item in executions
            if item.name not in required and item.status != "COMPLETED"
        ]
        if failed_required:
            status, message = "FAIL", "必需工具执行失败：" + ", ".join(failed_required)
        elif failed_optional:
            status, message = "WARN", "补充工具执行失败，不影响核心数据结论：" + ", ".join(failed_optional)
        else:
            status, message = "PASS", "所有已调用工具均成功完成"
        return [ValidationCheck(
            code="TOOL_EXECUTION", status=status, message=message,
            details={"required_tools": sorted(required)},
        )]

    @staticmethod
    def _dataset_identity_check(
        dataset: Dataset, evidence: list[EvidenceItem]
    ) -> ValidationCheck:
        query_id = f"query:{dataset.snapshot_id}"
        evidence_ids = {item.evidence_id for item in evidence}
        trusted = (
            bool(dataset.snapshot_id)
            and dataset.data_as_of.tzinfo is not None
            and dataset.data_as_of.utcoffset() is not None
            and query_id in evidence_ids
        )
        quality_pass = dataset.quality_status.upper() == "PASS"
        if not trusted:
            status, message = "FAIL", "查询结果缺少可回溯的快照、时区时间或查询证据"
        elif not quality_pass:
            status, message = "WARN", f"上游数据质量状态为 {dataset.quality_status}，不能升级为高可靠结论"
        else:
            status, message = "PASS", "查询快照、读取时间、质量状态和证据引用完整"
        return ValidationCheck(
            code="DATASET_IDENTITY", status=status, message=message,
            evidence_ids=[query_id] if query_id in evidence_ids else [],
            details={
                "snapshot_id": dataset.snapshot_id,
                "data_as_of": dataset.data_as_of.isoformat(),
                "quality_status": dataset.quality_status,
            },
        )

    @staticmethod
    def _empty_data_check(dataset: Dataset, analysis_required: bool) -> ValidationCheck:
        effective = [
            value for row in dataset.rows for value in row.values() if value is not None
        ]
        if dataset.rows and effective:
            return ValidationCheck(
                code="DATA_AVAILABLE", status="PASS",
                message=f"结果包含 {dataset.row_count} 行可用数据",
            )
        return ValidationCheck(
            code="DATA_AVAILABLE",
            status="FAIL" if analysis_required else "WARN",
            message=(
                "结果为空，不能执行分析或生成数值结论"
                if analysis_required else "查询成功但结果为空"
            ),
        )

    @staticmethod
    def _time_scope_check(
        request: CanonicalAnalysisRequest, dataset: Dataset
    ) -> ValidationCheck:
        if request.time_range is None:
            return ValidationCheck(
                code="TIME_SCOPE", status="NOT_APPLICABLE",
                message="本次请求没有显式时间范围",
            )
        watermark = dataset.source_data_as_of
        if watermark is None:
            return ValidationCheck(
                code="TIME_SCOPE", status="WARN",
                message="结果未提供业务数据水位，无法证明请求时间范围已被完整覆盖",
            )
        watermark_day = watermark.date() if isinstance(watermark, datetime) else watermark
        if request.time_range.start > watermark_day:
            return ValidationCheck(
                code="TIME_SCOPE", status="FAIL",
                message="请求时间范围完全位于当前业务数据水位之后",
                details={"source_data_as_of": watermark_day.isoformat()},
            )
        if request.time_range.end_exclusive > watermark_day + timedelta(days=1):
            return ValidationCheck(
                code="TIME_SCOPE", status="WARN",
                message="请求时间范围有一部分超过当前业务数据水位",
                details={"source_data_as_of": watermark_day.isoformat()},
            )
        return ValidationCheck(
            code="TIME_SCOPE", status="PASS",
            message="请求结束日期未超过当前业务数据水位",
            details={"source_data_as_of": watermark_day.isoformat()},
        )

    @staticmethod
    def _metric_scope_check(
        request: CanonicalAnalysisRequest,
        dataset: Dataset,
        analysis: AnalysisOutput | None,
    ) -> ValidationCheck:
        metrics = [
            item.canonical_name or item.input or item.metric_id or ""
            for item in request.metrics
        ]
        if not metrics:
            return ValidationCheck(
                code="METRIC_SCOPE", status="NOT_APPLICABLE",
                message="本次请求没有结构化指标",
            )
        searchable = json.dumps(
            {
                "columns": dataset.columns,
                "method": analysis.method if analysis else "",
                "facts": analysis.facts if analysis else {},
            },
            ensure_ascii=False,
            default=str,
        ).casefold()
        missing = [metric for metric in metrics if metric.casefold() not in searchable]
        if missing:
            return ValidationCheck(
                code="METRIC_SCOPE", status="WARN",
                message="部分指标名称未直接出现在结果列或分析事实中，需要依赖语义指标证据确认口径",
                details={"unmatched_metrics": missing},
            )
        return ValidationCheck(
            code="METRIC_SCOPE", status="PASS",
            message="请求指标可在结果列或分析事实中追踪",
        )

    @staticmethod
    def _finite_values_check(dataset: Dataset) -> ValidationCheck:
        invalid: list[str] = []
        for index, row in enumerate(dataset.rows):
            for column, value in row.items():
                if isinstance(value, float) and not math.isfinite(value):
                    invalid.append(f"row={index},column={column}")
                    if len(invalid) >= 10:
                        break
            if len(invalid) >= 10:
                break
        return ValidationCheck(
            code="FINITE_VALUES",
            status="FAIL" if invalid else "PASS",
            message=("结果包含 NaN 或无穷值" if invalid else "结果未发现 NaN 或无穷值"),
            details={"locations": invalid},
        )

    @classmethod
    def _change_rate_check(cls, facts: dict[str, Any]) -> ValidationCheck:
        candidates = cls._fact_dicts(facts)
        verified = 0
        mismatches: list[dict[str, Any]] = []
        for item in candidates:
            if not {"base", "current", "change_rate"}.issubset(item):
                continue
            base, current, rate = (
                cls._number(item.get("base")), cls._number(item.get("current")),
                cls._number(item.get("change_rate")),
            )
            if base is None or current is None or rate is None or base == 0:
                continue
            verified += 1
            expected = (current - base) / abs(base)
            if not math.isclose(rate, expected, rel_tol=1e-7, abs_tol=1e-9):
                mismatches.append({"base": base, "current": current, "actual": rate, "expected": expected})
        if mismatches:
            return ValidationCheck(
                code="CHANGE_RATE", status="FAIL",
                message="发现环比/同比变化率与基期、当前期数值不一致",
                details={"mismatches": mismatches[:10]},
            )
        return ValidationCheck(
            code="CHANGE_RATE",
            status="PASS" if verified else "NOT_APPLICABLE",
            message=(f"已复核 {verified} 组变化率计算" if verified else "没有可独立复核的变化率三元组"),
        )

    @classmethod
    def _reconciliation_check(cls, facts: dict[str, Any]) -> ValidationCheck:
        failures: list[str] = []
        verified = 0
        for key, value in cls._walk(facts):
            normalized = key.casefold()
            if normalized.endswith("residual") or "残差" in normalized:
                number = cls._number(value)
                if number is not None:
                    verified += 1
                    if not math.isclose(number, 0, rel_tol=1e-7, abs_tol=1e-7):
                        failures.append(f"{key}={number}")
            if key == "status" and str(value).casefold() == "failed":
                failures.append("additivity_check.status=failed")
        if failures:
            return ValidationCheck(
                code="RECONCILIATION", status="FAIL",
                message="分组合计、贡献度或分解结果未能与总体变化对账",
                details={"failures": failures[:10]},
            )
        return ValidationCheck(
            code="RECONCILIATION",
            status="PASS" if verified else "NOT_APPLICABLE",
            message=(f"已复核 {verified} 个对账残差" if verified else "本分析没有需要执行的总分对账残差"),
        )

    @staticmethod
    def _evidence_grounding_check(
        analysis: AnalysisOutput, evidence: list[EvidenceItem]
    ) -> ValidationCheck:
        kinds = {item.kind for item in evidence}
        missing = [kind for kind in ("QUERY_RESULT",) if kind not in kinds]
        locked = (
            analysis.facts.get("decision_source") == "DETERMINISTIC_ALGORITHM"
            and analysis.facts.get("llm_role") == "PRESENTATION_ONLY"
        )
        if missing:
            return ValidationCheck(
                code="EVIDENCE_GROUNDING", status="FAIL",
                message="分析结论缺少查询证据",
                details={"missing_evidence_kinds": missing, "decision_locked": locked},
            )
        if not locked:
            return ValidationCheck(
                code="EVIDENCE_GROUNDING", status="WARN",
                message="分析结果未声明确定性决策锁，模型合成不得将其升级为确定结论",
                evidence_ids=[
                    item.evidence_id for item in evidence
                    if item.kind == "QUERY_RESULT"
                ][:20],
                details={"decision_locked": False},
            )
        return ValidationCheck(
            code="EVIDENCE_GROUNDING", status="PASS",
            message="分析结论已绑定查询证据，模型角色限制为表达整理",
            evidence_ids=[item.evidence_id for item in evidence if item.kind == "QUERY_RESULT"][:20],
        )

    @classmethod
    def _causality_check(
        cls, analysis: AnalysisOutput, final_answer: str | None
    ) -> ValidationCheck:
        if analysis.facts.get("causality_established") is not False:
            return ValidationCheck(
                code="CAUSALITY", status="NOT_APPLICABLE",
                message="分析事实未声明非因果约束",
            )
        if final_answer and any(word in final_answer for word in cls._CAUSAL_WORDS):
            return ValidationCheck(
                code="CAUSALITY", status="FAIL",
                message="最终表述将描述性贡献错误表达为已证明的因果关系",
            )
        return ValidationCheck(
            code="CAUSALITY", status="PASS",
            message="贡献、相关和候选解释未被提升为确定性因果结论",
        )

    @classmethod
    def _fact_dicts(cls, value: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, dict):
            result.append(value)
            for child in value.values():
                result.extend(cls._fact_dicts(child))
        elif isinstance(value, list):
            for child in value:
                result.extend(cls._fact_dicts(child))
        return result

    @classmethod
    def _walk(cls, value: Any, prefix: str = ""):
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if isinstance(child, (dict, list)):
                    yield from cls._walk(child, path)
                else:
                    yield str(key), child
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk(child, prefix)

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
