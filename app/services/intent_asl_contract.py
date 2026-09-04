"""Machine-readable contract between canonical intent parsing and ASL planning.

The contract deliberately contains semantic labels rather than physical table or
column names.  Oagnet resolves those labels against the metadata recalled for the
current semantic model.  This keeps query-shape authority with the caller without
teaching either service database-specific mappings.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.domain.models import AnalysisOperator, CanonicalAnalysisRequest, PrimaryIntent
from app.services.relationship_projection import (
    requires_distinct_relationship_projection,
)


CONTRACT_VERSION = "1.0"


_RELATIONSHIP_COUNT_PROJECTIONS = {
    "已合作医院数": ("医院", "医院名称"),
    "已合作经销商数": ("经销商", "经销商名称"),
    "已合作供应商数": ("供应商", "供应商名称"),
    "已合作客户数": ("客户", "客户名称"),
    "已合作门店数": ("门店", "门店名称"),
}


def _is_negative_filter(item: dict[str, Any]) -> bool:
    return str(item.get("operator") or "").upper() in {
        "NE", "!=", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE",
    }


def _query_object(
    request: CanonicalAnalysisRequest, *, prefer_grouping_object: bool = False
) -> str | None:
    if (
        "DEPARTMENT_GRAIN=PRODUCT_MAIN_DEPARTMENT_COMBINATION"
        in request.assumptions
    ):
        # The visible grouping label is an attribute grain; its semantic owner
        # remains the product master.  Returning the grouping label itself as
        # query_object makes an overlapping standard-department dimension look
        # more authoritative than the product attribute.
        return request.entity or "商品"
    # Ranked/grouped requests commonly express the return object as the last
    # non-time dimension.  This is a semantic label only; Oagnet still resolves
    # the actual entity and display field from recalled metadata.
    candidates = [
        value for value in request.dimensions
        if value not in {"时间", "日期", "年", "季度", "月", "周", "日"}
    ]
    if prefer_grouping_object and candidates:
        return candidates[-1]
    return request.entity or (candidates[-1] if candidates else None)


def build_intent_asl_contract(request: CanonicalAnalysisRequest) -> dict[str, Any]:
    """Build the authoritative query-shape contract for one canonical request."""

    metric_codes = [
        metric.metric_id.split(":", 1)[1]
        for metric in request.metrics
        if metric.metric_id and ":" in metric.metric_id
    ]
    ranking = any(
        operator in request.operators
        for operator in (AnalysisOperator.TOP_N, AnalysisOperator.BOTTOM_N, AnalysisOperator.SORT)
    ) or request.ranking_limit is not None
    descending = AnalysisOperator.BOTTOM_N not in request.operators
    detail_like = request.primary_intent == PrimaryIntent.DETAIL_QUERY
    metric_required = request.primary_intent in {
        PrimaryIntent.METRIC_QUERY,
        PrimaryIntent.TREND_ANALYSIS,
        PrimaryIntent.COMPARISON_ANALYSIS,
        PrimaryIntent.COMPOSITION_ANALYSIS,
        PrimaryIntent.ANOMALY_ANALYSIS,
        PrimaryIntent.ROOT_CAUSE_ANALYSIS,
        PrimaryIntent.FORECAST_ANALYSIS,
    }
    # A comparison can be an entity-to-entity detail recommendation, but if the
    # canonical parser supplied metrics they remain mandatory.
    if request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS:
        metric_required = bool(request.metrics)

    positive_filters = [item for item in request.filters if not _is_negative_filter(item)]
    negative_filters = [item for item in request.filters if _is_negative_filter(item)]
    filter_roles = {
        str(item.get("field") or "").strip()
        for item in request.filters
        if isinstance(item, dict) and str(item.get("field") or "").strip()
    }
    role_aliases = {
        "产品": "商品", "产品名称": "商品名称",
        "品牌": "商品品牌", "品牌名称": "商品品牌", "母品牌": "商品品牌",
        "母厂牌": "商品品牌", "厂牌": "商品品牌",
        "品类": "商品品类", "商品分类": "商品品类", "产品分类": "商品品类",
        "区域": "地区", "省份": "地区", "城市": "地区",
        "业务省份": "地区", "业务城市": "地区",
    }

    def normalized_role(value: str) -> str:
        return role_aliases.get(value, value)

    normalized_filter_roles = {normalized_role(value) for value in filter_roles}
    required_groupings = list(dict.fromkeys(
        value for value in request.dimensions
        if value not in {"时间", "日期", "年", "季度", "月", "周", "日"}
        and normalized_role(value) not in normalized_filter_roles
    ))
    default_time_only = "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
    profile_snapshot_scope = any(
        value in {
            "TIME_SCOPE=PROFILE_SNAPSHOT",
        }
        for value in request.assumptions
    )
    all_history_scope = any(
        value in {"TIME_SCOPE=ALL_TIME", "TIME_SCOPE=ALL_AVAILABLE_HISTORY"}
        for value in request.assumptions
    )
    time_grouping_requested = (
        AnalysisOperator.TIME_BUCKET in request.operators
        or any(
            value.startswith("DEFAULT_TIME_GRANULARITY=")
            for value in request.assumptions
        )
    )
    # ALL_TIME controls the date predicate, not whether a time dimension may
    # be projected. An all-history monthly/quarterly statistic therefore has
    # an OPTIONAL range and a required bucket, while snapshot/profile queries
    # still prohibit transaction-time grouping.
    period_independent_scope = (
        profile_snapshot_scope
        or all_history_scope and not time_grouping_requested
    )
    time_policy = (
        "FORBIDDEN"
        if period_independent_scope or (detail_like and default_time_only)
        else "REQUIRED"
        if request.time_range is not None
        else "OPTIONAL"
    )
    return {
        "version": CONTRACT_VERSION,
        "intent": request.primary_intent.value,
        "query_object": _query_object(request, prefer_grouping_object=ranking),
        "metric_required": metric_required,
        "required_metrics": [metric.canonical_name or metric.input for metric in request.metrics],
        "required_metric_codes": metric_codes,
        "required_projections": list(dict.fromkeys(request.fields if detail_like else [])),
        "required_groupings": required_groupings if not detail_like else [],
        "projection_mode": (
            "DISTINCT"
            if detail_like and requires_distinct_relationship_projection(request)
            else "ROWS"
            if detail_like
            else None
        ),
        # Relationship lists in this sales-analysis agent describe observed
        # transaction relationships, not incidental shared attributes such as
        # two parties being in the same city.  This remains a semantic entity
        # label; Oagnet resolves its current code and joins only through the
        # published relationship graph.
        "relationship_anchor": (
            "销售订单"
            if "SET_RELATIONSHIP_PROJECTION" in request.assumptions
            else None
        ),
        "filters": positive_filters,
        "negative_filters": negative_filters,
        # The caller owns the literal mention, while Oagnet owns its semantic
        # field and canonical source value.  This prevents the intent parser
        # from guessing 商品名称 for an ambiguous ``X产品`` phrase.
        "semantic_entity_mentions": list(dict.fromkeys(
            value.strip() for value in request.semantic_entity_mentions if value.strip()
        )),
        "sorting": (
            {
                "required": True,
                "direction": "DESC" if descending else "ASC",
                "limit": request.ranking_limit,
            }
            if ranking else None
        ),
        "time_dimension_required": request.primary_intent == PrimaryIntent.TREND_ANALYSIS,
        "time_policy": time_policy,
        "canonical_time_range": (
            {
                "start": request.time_range.start.isoformat(),
                "end": (
                    request.time_range.end_exclusive - timedelta(days=1)
                ).isoformat(),
            }
            if time_policy == "REQUIRED" and request.time_range is not None
            else None
        ),
    }


def validate_intent_asl_contract_definition(contract: dict[str, Any]) -> list[str]:
    """Return stable definition errors before the contract crosses services."""

    errors: list[str] = []
    if contract.get("version") != CONTRACT_VERSION:
        errors.append("CONTRACT_VERSION_INVALID")
    if contract.get("intent") == PrimaryIntent.DETAIL_QUERY.value:
        if not contract.get("query_object"):
            errors.append("QUERY_OBJECT_REQUIRED")
        if not contract.get("required_projections"):
            errors.append("DETAIL_PROJECTION_REQUIRED")
        if contract.get("projection_mode") not in {"DISTINCT", "ROWS"}:
            errors.append("DETAIL_PROJECTION_MODE_REQUIRED")
    groupings = contract.get("required_groupings")
    if not isinstance(groupings, list) or any(
        not isinstance(value, str) or not value.strip() for value in groupings
    ):
        errors.append("GROUPING_CONTRACT_INVALID")
    if contract.get("metric_required") and not (
        contract.get("required_metric_codes") or contract.get("required_metrics")
    ):
        errors.append("METRIC_REQUIRED")
    if contract.get("time_dimension_required") and contract.get("intent") != PrimaryIntent.TREND_ANALYSIS.value:
        errors.append("TIME_DIMENSION_CONTRACT_INVALID")
    if contract.get("time_policy") not in {"REQUIRED", "OPTIONAL", "FORBIDDEN"}:
        errors.append("TIME_POLICY_INVALID")
    if contract.get("time_dimension_required") and contract.get("time_policy") != "REQUIRED":
        errors.append("TIME_POLICY_CONFLICT")
    canonical_time_range = contract.get("canonical_time_range")
    if contract.get("time_policy") == "REQUIRED" and not (
        isinstance(canonical_time_range, dict)
        and canonical_time_range.get("start")
        and canonical_time_range.get("end")
    ):
        errors.append("CANONICAL_TIME_RANGE_REQUIRED")
    if contract.get("time_policy") != "REQUIRED" and canonical_time_range is not None:
        errors.append("CANONICAL_TIME_RANGE_FORBIDDEN")
    mentions = contract.get("semantic_entity_mentions")
    if not isinstance(mentions, list) or any(
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > 100
        for value in mentions
    ):
        errors.append("SEMANTIC_ENTITY_MENTIONS_INVALID")
    sorting = contract.get("sorting")
    if sorting is not None and (
        sorting.get("required") is not True
        or str(sorting.get("direction") or "").upper() not in {"ASC", "DESC"}
    ):
        errors.append("SORTING_CONTRACT_INVALID")
    return errors


def validate_intent_asl_contract_completeness(
    contract: dict[str, Any],
    request: CanonicalAnalysisRequest,
) -> list[dict[str, Any]]:
    """Compare current-turn explicit requirements with the outbound contract.

    Shape validation can only prove that a contract is internally coherent.
    This second gate proves that explicit constraints captured before context
    merging survived into the contract.  It deliberately uses the existing
    ``CurrentTurnFacts`` provenance boundary instead of reparsing history or
    introducing a second conversation state model.
    """

    admission = request.turn_admission
    if admission is None:
        return []
    explicit = admission.current_turn_facts.explicit_slots
    errors: list[dict[str, Any]] = []

    def authorized_relationship_count_projection() -> bool:
        """Recognize the one audited metric-to-detail execution rewrite.

        Relationship counts are calculated from the corresponding DISTINCT
        visible-name set so count and list questions describe the same rows.
        The rewrite intentionally changes only intent, metric and projection;
        all explicit filters and sorting constraints are still checked below.
        """

        if (
            request.execution_contract_transform
            != "RELATIONSHIP_COUNT_TO_DISTINCT_PROJECTION"
            or "RELATIONSHIP_COUNT_FROM_VISIBLE_NAME_SET"
            not in request.assumptions
        ):
            return False
        expected_intent_slot = explicit.get("analysis_type")
        expected_metric_slot = explicit.get("metrics")
        if (
            expected_intent_slot is None
            or expected_intent_slot.value != PrimaryIntent.METRIC_QUERY.value
            or expected_metric_slot is None
            or not isinstance(expected_metric_slot.value, list)
            or len(expected_metric_slot.value) != 1
        ):
            return False
        shape = _RELATIONSHIP_COUNT_PROJECTIONS.get(
            str(expected_metric_slot.value[0]).strip()
        )
        if shape is None:
            return False
        entity, projection = shape
        return bool(
            contract.get("intent") == PrimaryIntent.DETAIL_QUERY.value
            and contract.get("metric_required") is False
            and not contract.get("required_metrics")
            and not contract.get("required_metric_codes")
            and projection in (contract.get("required_projections") or [])
            and contract.get("query_object") in {entity, "sales_order"}
        )

    relationship_count_projection = authorized_relationship_count_projection()

    def add(code: str, slot: str, expected: Any, actual: Any) -> None:
        errors.append({
            "code": code,
            "slot": slot,
            "expected": expected,
            "actual": actual,
        })

    expected_intent = explicit.get("analysis_type")
    if (
        expected_intent is not None
        and contract.get("intent") != expected_intent.value
        and not relationship_count_projection
    ):
        add(
            "EXPLICIT_INTENT_MISSING",
            "intent",
            expected_intent.value,
            contract.get("intent"),
        )

    expected_metrics = explicit.get("metrics")
    if expected_metrics is not None:
        required_metrics = [
            str(value).strip()
            for value in (contract.get("required_metrics") or [])
            if str(value).strip()
        ]
        required_metric_codes = [
            str(value).strip()
            for value in (contract.get("required_metric_codes") or [])
            if str(value).strip()
        ]
        # Semantic binding is allowed to replace a user-facing alias such as
        # ``整体业务规模`` with its canonical metric name/ID.  Completeness is
        # therefore about preserving the requested metric cardinality, not
        # literal string equality across that authorized normalization step.
        actual_metric_count = max(
            len(required_metrics),
            len(required_metric_codes),
        )
        expected_metric_count = len([
            value for value in expected_metrics.value if str(value).strip()
        ])
        if (
            actual_metric_count < expected_metric_count
            and not relationship_count_projection
        ):
            add(
                "EXPLICIT_METRIC_MISSING",
                "metrics",
                expected_metrics.value,
                {
                    "required_metrics": required_metrics,
                    "required_metric_codes": required_metric_codes,
                },
            )

    entity_aliases = {
        "商品": "产品",
        "制造商": "厂家",
        "供应商": "经销商",
    }

    def entity_key(value: Any) -> str:
        text = str(value or "").strip()
        return entity_aliases.get(text, text)

    expected_object = explicit.get("query_object")
    if (
        expected_object is not None
        and entity_key(contract.get("query_object"))
        != entity_key(expected_object.value)
        and not relationship_count_projection
    ):
        add(
            "EXPLICIT_QUERY_OBJECT_MISSING",
            "query_object",
            expected_object.value,
            contract.get("query_object"),
        )

    expected_projection = explicit.get("projection")
    if expected_projection is not None:
        actual_projection = list(contract.get("required_projections") or [])
        missing_projection = [
            value for value in expected_projection.value
            if value not in actual_projection
        ]
        if missing_projection:
            add(
                "EXPLICIT_PROJECTION_MISSING",
                "projection",
                expected_projection.value,
                actual_projection,
            )

    negative_operators = {
        "NE", "!=", "NOT_EQ", "NOT IN", "NOT_IN", "EXCLUDE",
    }

    def filter_signature(item: dict[str, Any]) -> tuple[str, bool, tuple[str, ...]]:
        raw_value = item.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        normalized_values = tuple(sorted(
            str(value).strip().strip("%")
            for value in values
            if value not in (None, "")
        ))
        return (
            str(item.get("field") or "").strip(),
            str(item.get("operator") or "").upper() in negative_operators,
            normalized_values,
        )

    expected_filters = explicit.get("filters")
    if expected_filters is not None and isinstance(expected_filters.value, list):
        contract_filters = [
            item for item in [
                *(contract.get("filters") or []),
                *(contract.get("negative_filters") or []),
            ]
            if isinstance(item, dict)
        ]
        actual_signatures = {filter_signature(item) for item in contract_filters}

        # Entity-value grounding is an authorized semantic normalization step:
        # the current-turn parser may provisionally capture ``商品名称=费森尤斯``
        # while the current vector catalog proves that the literal belongs to
        # ``母厂牌/parent_brand``.  Completeness must still prove that the same
        # operator and value survived, but it must compare against the grounded
        # semantic attribute rather than requiring the parser's provisional
        # field label forever.  Without this bridge the correct grounded ASL is
        # rejected as EXPLICIT_FILTER_MISSING before it can execute.
        # Keep this proof local to DataAnalysis_Agent.  The Oagnet request
        # contract is intentionally versioned and strict, so internal grounding
        # provenance must not be added to the wire schema.  The outbound
        # ``filters`` are already canonicalized before this contract is built.
        semantic_bindings = [
            binding.model_dump(mode="json")
            for binding in request.semantic_filter_bindings
        ]

        def has_grounded_equivalent(item: dict[str, Any]) -> bool:
            raw_value = item.get("value")
            expected_values = (
                raw_value if isinstance(raw_value, list) else [raw_value]
            )
            expected_value_set = {
                str(value).strip().strip("%")
                for value in expected_values
                if value not in (None, "")
            }
            expected_negative = (
                str(item.get("operator") or "").upper() in negative_operators
            )
            for binding in semantic_bindings:
                input_value = str(binding.get("input_value") or "").strip().strip("%")
                canonical_value = (
                    str(binding.get("canonical_value") or "").strip().strip("%")
                )
                if not expected_value_set.intersection({input_value, canonical_value}):
                    continue
                canonical_fields = {
                    str(binding.get("canonical_name") or "").strip(),
                    str(binding.get("attribute_code") or "").strip(),
                }
                canonical_fields.discard("")
                if not canonical_fields:
                    continue
                for actual in contract_filters:
                    actual_field, actual_negative, actual_values = filter_signature(actual)
                    if (
                        actual_field in canonical_fields
                        and actual_negative == expected_negative
                        and canonical_value in set(actual_values)
                    ):
                        return True
            return False

        missing_filters = [
            item for item in expected_filters.value
            if isinstance(item, dict)
            and filter_signature(item) not in actual_signatures
            and not has_grounded_equivalent(item)
        ]
        if missing_filters:
            add(
                "EXPLICIT_FILTER_MISSING",
                "filters",
                missing_filters,
                contract_filters,
            )

    expected_sorting = explicit.get("sorting")
    if expected_sorting is not None:
        actual_sorting = contract.get("sorting")
        expected_value = expected_sorting.value
        if not isinstance(actual_sorting, dict):
            add("EXPLICIT_SORTING_MISSING", "sorting", expected_value, actual_sorting)
        elif (
            str(actual_sorting.get("direction") or "").upper()
            != str(expected_value.get("direction") or "").upper()
            or (
                expected_value.get("limit") is not None
                and actual_sorting.get("limit") != expected_value.get("limit")
            )
        ):
            add("EXPLICIT_SORTING_MISMATCH", "sorting", expected_value, actual_sorting)

    expected_top_n = explicit.get("top_n")
    if expected_top_n is not None:
        actual_sorting = contract.get("sorting") or {}
        if actual_sorting.get("limit") != expected_top_n.value:
            add(
                "EXPLICIT_TOP_N_MISSING",
                "top_n",
                expected_top_n.value,
                actual_sorting.get("limit"),
            )

    return errors
