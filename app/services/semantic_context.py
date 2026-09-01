from __future__ import annotations

import hashlib
import json
import re
from ast import literal_eval
from typing import Any

from app.domain.models import (
    BusinessRuleRef, CanonicalAnalysisRequest, SemanticAssetRef,
    SemanticContextSnapshot, RelationshipPathRef, SemanticRetrievalItem,
    SemanticRetrievalSummary, FilterResolutionEvidence, SemanticPlanEvidence,
)


def build_semantic_context_snapshot(
    request: CanonicalAnalysisRequest,
    semantic_model_id: int | None,
) -> SemanticContextSnapshot:
    """Project selected semantics without copying or persisting a second model."""

    scope = str(semantic_model_id or "unscoped")
    assets: list[SemanticAssetRef] = []
    seen: set[tuple[str, str]] = set()

    def add(asset: SemanticAssetRef) -> None:
        key = (asset.asset_type, asset.asset_id)
        if key not in seen:
            seen.add(key)
            assets.append(asset)

    for metric in request.metrics:
        resolved = bool(metric.metric_id)
        add(SemanticAssetRef(
            asset_id=metric.metric_id or f"{scope}:metric-input:{metric.input}",
            asset_type="METRIC",
            canonical_name=metric.canonical_name or metric.input,
            version=metric.version or "current",
            source="SEMANTIC_LAYER" if resolved else "REQUEST_UNDERSTANDING",
            confidence=1.0 if resolved else request.intent_confidence,
            selection_reason=(
                f"指标解析服务已将用户指标“{metric.input}”绑定到权威指标"
                if resolved else f"查询理解阶段提取了用户指标“{metric.input}”"
            ),
            metadata={"unit": metric.unit} if metric.unit else {},
        ))
    if request.entity:
        add(SemanticAssetRef(
            asset_id=f"{scope}:entity:{request.entity}", asset_type="ENTITY",
            canonical_name=request.entity, source="REQUEST_UNDERSTANDING",
            confidence=request.intent_confidence,
            selection_reason="查询理解阶段将其选为本次查询对象",
        ))
    for dimension in request.dimensions:
        add(SemanticAssetRef(
            asset_id=f"{scope}:dimension:{dimension}", asset_type="DIMENSION",
            canonical_name=dimension, source="REQUEST_UNDERSTANDING",
            confidence=request.intent_confidence,
            selection_reason="查询理解阶段将其选为分组或分析维度",
        ))
    for field in request.fields:
        add(SemanticAssetRef(
            asset_id=f"{scope}:field:{field}", asset_type="FIELD",
            canonical_name=field, source="REQUEST_UNDERSTANDING",
            confidence=request.intent_confidence,
            selection_reason="查询理解阶段将其选为明细输出字段",
        ))

    canonical = [asset.model_dump(mode="json") for asset in assets]
    fingerprint = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return SemanticContextSnapshot(
        snapshot_id=f"scx_{fingerprint[:24]}", semantic_model_id=semantic_model_id,
        assets=assets, context_fingerprint=fingerprint,
    )


def enrich_semantic_context_snapshot(
    snapshot: SemanticContextSnapshot,
    definitions: list[dict[str, Any]],
) -> SemanticContextSnapshot:
    """Add declared metric semantics and business rules to a base snapshot."""

    by_id = {str(item.get("metric_id")): item for item in definitions if item.get("metric_id")}
    enriched: list[SemanticAssetRef] = []
    rules: list[BusinessRuleRef] = list(snapshot.business_rules)
    for asset in snapshot.assets:
        definition = by_id.get(asset.asset_id)
        if asset.asset_type != "METRIC" or definition is None:
            enriched.append(asset)
            continue
        formula = str(definition.get("calculation_formula") or definition.get("formula") or "")
        aggregation_match = re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\s*\(", formula, re.I)
        aggregation = aggregation_match.group(1).upper() if aggregation_match else None
        is_distinct = bool(re.search(r"\bDISTINCT\b", formula, re.I))
        additivity = (
            "ADDITIVE" if aggregation == "SUM"
            else "NON_ADDITIVE" if aggregation in {"COUNT", "AVG", "MIN", "MAX"} or is_distinct
            else "UNKNOWN"
        )
        bound_entities = [
            str(item.get("resolved_entity_code") or item.get("entity_code"))
            for item in definition.get("bound_entities", []) if isinstance(item, dict)
            and (item.get("resolved_entity_code") or item.get("entity_code"))
        ]
        metadata = dict(asset.metadata)
        metadata.update({
            "formula": formula or None,
            "description": definition.get("description"),
            "applicable_scenarios": definition.get("applicable_scenarios") or [],
            "semantic_model_id": definition.get("semantic_model_id"),
            "business_domain_id": definition.get("business_domain_id"),
            "metadata_source": "SEMANTIC_LAYER_DEFINITION",
            "inferred_fields": ["aggregation", "additivity"],
        })
        enriched.append(asset.model_copy(update={
            "grain": bound_entities,
            "aggregation": aggregation,
            "additivity": additivity,
            "metadata": metadata,
        }))
        for raw_rule in definition.get("global_filters", []) or []:
            parsed: Any = raw_rule
            if isinstance(raw_rule, str):
                try:
                    parsed = literal_eval(raw_rule)
                except (ValueError, SyntaxError):
                    parsed = {"filterCondition": raw_rule}
            if not isinstance(parsed, dict):
                continue
            rule_text = str(parsed.get("filterCondition") or "").strip()
            if not rule_text:
                continue
            digest = hashlib.sha256(
                f"{asset.asset_id}\x1f{rule_text}".encode("utf-8")
            ).hexdigest()
            rules.append(BusinessRuleRef(
                rule_id=f"rule_{digest[:24]}", version=asset.version,
                source="SEMANTIC_LAYER", confidence=1.0,
                selection_reason="该规则是所选权威指标声明的全局过滤口径",
                rule_text=rule_text, applies_to=[asset.asset_id],
            ))

    canonical = {
        "assets": [item.model_dump(mode="json") for item in enriched],
        "rules": [item.model_dump(mode="json") for item in rules],
        "paths": [item.model_dump(mode="json") for item in snapshot.relationship_paths],
    }
    fingerprint = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return snapshot.model_copy(update={
        "snapshot_id": f"scx_{fingerprint[:24]}",
        "assets": enriched,
        "business_rules": rules,
        "context_fingerprint": fingerprint,
    })


def semantic_relationship_targets(request: CanonicalAnalysisRequest) -> list[str]:
    """Return explicit entity-like targets only; never infer from field names."""
    values = [*request.dimensions, *request.fields]
    values.extend(
        str(item.get("field") or "") for item in request.filters
        if isinstance(item, dict)
    )
    targets = []
    for value in values:
        text = str(value).strip()
        candidate = text.split('.', 1)[0] if '.' in text else text
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', candidate):
            targets.append(candidate)
    return list(dict.fromkeys(targets))


def attach_relationship_paths(
    snapshot: SemanticContextSnapshot, payload: dict[str, Any]
) -> SemanticContextSnapshot:
    paths: list[RelationshipPathRef] = []
    for item in payload.get("paths", []) or []:
        relationships = item.get("relationships") or []
        canonical_text = str(item.get("canonical_path") or "")
        canonical_path = [part.strip() for part in canonical_text.split("->") if part.strip()]
        if len(canonical_path) < 2 or not relationships:
            continue
        paths.append(RelationshipPathRef(
            path_id=str(item["path_id"]),
            source_asset_id=f"{snapshot.semantic_model_id}:entity:{item['source_entity']}",
            target_asset_id=f"{snapshot.semantic_model_id}:entity:{item['target_entity']}",
            relationship_ids=[str(edge["relationship_id"]) for edge in relationships],
            canonical_path=canonical_path,
            join_conditions=[str(edge["join_condition"]) for edge in relationships],
            cardinalities=[str(edge.get("cardinality") or "UNKNOWN") for edge in relationships],
            risk=str(item.get("risk") or "HIGH"),
            version="current", source="SEMANTIC_LAYER", confidence=1.0,
            selection_reason="该路径由语义关系表在当前查询实体之间解析得到",
        ))
    canonical = {
        "assets": [item.model_dump(mode="json") for item in snapshot.assets],
        "rules": [item.model_dump(mode="json") for item in snapshot.business_rules],
        "paths": [item.model_dump(mode="json") for item in paths],
    }
    fingerprint = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return snapshot.model_copy(update={
        "snapshot_id": f"scx_{fingerprint[:24]}",
        "relationship_paths": paths,
        "context_fingerprint": fingerprint,
    })


def apply_minimum_sufficient_context(
    snapshot: SemanticContextSnapshot,
    question: str,
    *,
    char_budget: int = 12_000,
) -> SemanticContextSnapshot:
    """Rank typed semantic context and retain only a bounded sufficient set."""

    compact_question = re.sub(r'\s+', '', question or '').casefold()
    candidates: list[tuple[SemanticRetrievalItem, Any]] = []

    def candidate(
        ref_id: str, ref_type: str, value: Any, text: str,
        base_score: float, required: bool, reason: str,
    ) -> None:
        normalized_text = re.sub(r'\s+', '', text).casefold()
        exact_boost = 0.08 if normalized_text and normalized_text in compact_question else 0.0
        encoded = json.dumps(
            value.model_dump(mode="json"), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        )
        candidates.append((SemanticRetrievalItem(
            ref_id=ref_id, ref_type=ref_type, score=min(1.0, base_score + exact_boost),
            required=required, char_cost=len(encoded), selection_reason=reason,
        ), value))

    for asset in snapshot.assets:
        required = asset.asset_type in {"METRIC", "ENTITY"}
        base = {"METRIC": .92, "ENTITY": .88, "DIMENSION": .82, "FIELD": .78, "MODEL": .75}[asset.asset_type]
        candidate(
            asset.asset_id, asset.asset_type, asset, asset.canonical_name,
            base, required,
            "权威指标/查询主体必须保留" if required else "与当前查询结构直接相关",
        )
    for rule in snapshot.business_rules:
        candidate(
            rule.rule_id, "BUSINESS_RULE", rule, rule.rule_text, .9, True,
            "所选指标声明的业务口径必须随查询保留",
        )
    for path in snapshot.relationship_paths:
        candidate(
            path.path_id, "RELATIONSHIP_PATH", path,
            ' '.join(path.canonical_path), .95, True,
            "执行所需的权威 Join 路径必须保留",
        )
    for query in snapshot.validated_queries:
        candidate(
            query.query_id, "VALIDATED_QUERY", query, query.question_pattern,
            .7 * query.confidence, False,
            "历史查询仅作为低优先级语义范例，不直接复用 SQL",
        )

    candidates.sort(key=lambda item: (
        not item[0].required, -item[0].score, item[0].ref_type, item[0].ref_id
    ))
    selected: list[SemanticRetrievalItem] = []
    dropped: list[SemanticRetrievalItem] = []
    selected_ids: set[str] = set()
    used = 0
    for item, _value in candidates:
        if item.required or used + item.char_cost <= char_budget:
            selected.append(item)
            selected_ids.add(item.ref_id)
            used += item.char_cost
        else:
            dropped.append(item)

    assets = [item for item in snapshot.assets if item.asset_id in selected_ids]
    rules = [item for item in snapshot.business_rules if item.rule_id in selected_ids]
    paths = [item for item in snapshot.relationship_paths if item.path_id in selected_ids]
    queries = [item for item in snapshot.validated_queries if item.query_id in selected_ids]
    summary = SemanticRetrievalSummary(
        char_budget=char_budget, chars_used=used, selected=selected, dropped=dropped,
        budget_exceeded_by_required_context=used > char_budget,
    )
    canonical = {
        "assets": [item.model_dump(mode="json") for item in assets],
        "rules": [item.model_dump(mode="json") for item in rules],
        "paths": [item.model_dump(mode="json") for item in paths],
        "queries": [item.model_dump(mode="json") for item in queries],
        "retrieval": summary.model_dump(mode="json"),
    }
    fingerprint = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return snapshot.model_copy(update={
        "snapshot_id": f"scx_{fingerprint[:24]}", "assets": assets,
        "business_rules": rules, "relationship_paths": paths,
        "validated_queries": queries, "retrieval_summary": summary,
        "context_fingerprint": fingerprint,
    })


def build_semantic_plan_evidence(
    request: CanonicalAnalysisRequest,
    snapshot: SemanticContextSnapshot,
) -> SemanticPlanEvidence:
    """Build auditable query-plan evidence without exposing model reasoning."""

    metrics = [item.asset_id for item in snapshot.assets if item.asset_type == "METRIC"]
    entities = [item.asset_id for item in snapshot.assets if item.asset_type == "ENTITY"]
    dimensions = [
        item.canonical_name for item in snapshot.assets
        if item.asset_type in {"DIMENSION", "FIELD"}
    ]
    filters: list[FilterResolutionEvidence] = []
    warnings: list[str] = []
    for item in request.filters:
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "=").strip().upper()
        raw_value = item.get("value")
        if isinstance(raw_value, list):
            value_summary = f"列表值（{len(raw_value)}项）"
        else:
            value_summary = str(raw_value)[:500] or "空值"
        if re.fullmatch(r'[A-Za-z_]\w*\.[A-Za-z_]\w*', field):
            resolution, confidence = "SEMANTIC_FIELD", 1.0
            reason = "过滤字段已绑定到带实体作用域的语义字段"
        elif re.fullmatch(r'[A-Za-z_]\w*', field):
            resolution, confidence = "LOGICAL_DIMENSION", request.intent_confidence
            reason = "过滤字段使用逻辑维度，由语义查询规划器继续解析"
        else:
            resolution, confidence = "UNRESOLVED", 0.0
            reason = "过滤字段尚未形成合法语义引用"
            warnings.append(f"未解析过滤字段: {field or '<empty>'}")
        filters.append(FilterResolutionEvidence(
            field=field or "<empty>", operator=operator or "=",
            value_summary=value_summary, resolution=resolution,
            confidence=confidence, selection_reason=reason,
        ))

    time_grain = request.forecast_granularity
    if time_grain is None:
        for assumption in request.assumptions:
            if str(assumption).startswith("DEFAULT_TIME_GRANULARITY="):
                time_grain = str(assumption).split("=", 1)[1].strip() or None
                break
    if snapshot.retrieval_summary and snapshot.retrieval_summary.budget_exceeded_by_required_context:
        warnings.append("必需语义上下文超过字符预算，已完整保留并标记超限")
    if snapshot.relationship_paths and any(path.risk != "LOW" for path in snapshot.relationship_paths):
        warnings.extend(
            f"关系路径 {path.path_id} 存在 {path.risk} 聚合风险"
            for path in snapshot.relationship_paths if path.risk != "LOW"
        )
    payload = {
        "snapshot": snapshot.snapshot_id,
        "metrics": metrics,
        "entities": entities,
        "dimensions": dimensions,
        "paths": [item.path_id for item in snapshot.relationship_paths],
        "rules": [item.rule_id for item in snapshot.business_rules],
        "filters": [item.model_dump(mode="json") for item in filters],
        "time_grain": time_grain,
        "warnings": warnings,
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return SemanticPlanEvidence(
        plan_id=f"spl_{fingerprint[:24]}", semantic_snapshot_id=snapshot.snapshot_id,
        semantic_model_id=snapshot.semantic_model_id, metric_ids=metrics,
        entity_ids=entities, grouping_dimensions=dimensions,
        relationship_path_ids=[item.path_id for item in snapshot.relationship_paths],
        business_rule_ids=[item.rule_id for item in snapshot.business_rules],
        filter_resolutions=filters, time_grain=time_grain,
        validation_status="PARTIAL" if warnings else "COMPLETE",
        warnings=warnings, plan_fingerprint=fingerprint,
    )
