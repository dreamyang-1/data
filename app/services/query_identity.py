"""Identity of effective query inputs, independent of wording and request IDs."""
from __future__ import annotations

import json
import re
from typing import Any


def query_identity(request) -> dict[str, Any]:
    fields = {
        "semantic_model_id", "semantic_model_version", "database_id", "business_domain_ids",
        "resolved_business_domain_ids", "business_domain_selection_mode", "primary_intent",
        "entity", "metric_subject_entity", "fields", "dimensions", "filters", "time_range",
        "comparison_type", "resolved_periods", "resolved_comparison", "ranking_limit",
        "forecast_horizon_periods", "forecast_granularity", "source_dataset_id", "asl_template",
        "dependency_constraints", "execution_contract_transform", "report_template_id",
        "temporal_anchor", "semantic_filter_bindings", "trusted_dimension_bindings",
    }
    result = request.model_dump(mode="json", include=fields)
    # This internal ASL input is intentionally excluded from public model dumps.
    result["metric_subject_entity"] = request.metric_subject_entity
    # A conjunction is order independent. Do not sort projection, grouping or
    # ordering lists: their order can carry output/precedence semantics.
    result["filters"] = sorted(result["filters"], key=lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    result["metrics"] = [
        {"name": metric.metric_id or metric.canonical_name or metric.input,
         "version": metric.version, "unit": metric.unit}
        for metric in request.metrics
    ]
    result["operators"] = sorted({operator.value for operator in request.operators})
    result["query_assumptions"] = sorted(value for value in request.assumptions if value.startswith(
        ("SORT_DIRECTION=", "EXPLICIT_PROJECTION_", "DETAIL_PROJECTION_", "RESULT_SET_REFERENCE=")))
    extraction = request._planner_extraction
    structured = extraction.structured if extraction is not None else None
    # These inputs reach ASL and/or deterministic result shaping even when the
    # canonical model has no separate field for them (e.g. dedup and sort keys).
    result["planner_query_shape"] = {
        key: structured[key] for key in (
            "实体", "指标", "维度", "展示字段", "过滤条件", "时间粒度", "排序", "限制", "是否去重", "输出要求"
        ) if structured is not None and key in structured
    }
    result["identity_version"] = 2  # old incomplete hashes cannot become cache hits
    return result


def requires_fresh_query(chat, request=None, *, question: str = "") -> bool:
    if getattr(chat, "_regeneration_mode", None) == "REFRESH":
        return True
    text = "\n".join(filter(None, (
        chat.question, getattr(request, "original_question", ""), question,
    )))
    # Negated refresh instructions explicitly preserve an existing snapshot.
    text = re.sub(r"(?:不用|无需|不要|不必|不再)(?:重新|再次)?(?:查询|查库|统计|取数|刷新)", "", text)
    return bool(re.search(
        r"重新(?:从数据库)?(?:查询|查|统计|取数)|重查|刷新(?:数据|结果)|最新数据|实时数据|不要复用|不复用",
        text,
    ))


def source_quality(reference) -> str:
    """Keep recorded quality; lack of evidence is WARN, never a new PASS."""
    statuses = [str(item["source_quality_status"]).upper()
                for item in reference.transformation_log
                if isinstance(item, dict) and "source_quality_status" in item]
    if "FAIL" in statuses:
        return "FAIL"
    return "PASS" if statuses and all(value == "PASS" for value in statuses) else "WARN"
