"""Materialize phase-2 audit evidence without importing into production code.

This helper is intentionally isolated under tools/audit.  It performs only local
file reads plus read-only semantic catalog reads and writes audit artifacts under
docs/phase2.  It never mutates application state, MySQL, Redis, or Milvus.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "phase2"
OAGENT = ROOT.parent / "Oagnet"


def dump_json(name: str, value: object) -> None:
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def dump_jsonl(name: str, rows: list[dict]) -> None:
    with (OUT / name).open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def dump_csv(name: str, rows: list[dict], fields: list[str]) -> None:
    with (OUT / name).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def load_catalog() -> tuple[dict, str | None]:
    """Read the catalog through the same read path as Oagnet; never write."""
    try:
        sys.path.insert(0, str(OAGENT))
        from mysql_tool import get_dsl_by_scope  # type: ignore

        return get_dsl_by_scope(81, 205), None
    except Exception as exc:  # pragma: no cover - runtime evidence path
        return {}, f"{type(exc).__name__}: {exc}"


def external_inventory() -> list[dict]:
    return [
        {
            "service": "DataAnalysis_Agent",
            "repository": str(ROOT),
            "runtime_endpoint": "http://127.0.0.1:8088",
            "role": "conversation orchestration, intent, state, ASL/SQL client, result validation",
            "source_obtained": True,
            "observed_health": "READY",
            "evidence": ["app/config.py:66-94", "app/adapters/http.py:1643-2183"],
        },
        {
            "service": "Oagnet",
            "repository": str(OAGENT),
            "runtime_endpoint": "http://127.0.0.1:8021",
            "role": "semantic retrieval, normalization, ASL generation",
            "source_obtained": True,
            "observed_health": "Milvus healthy",
            "evidence": ["Oagnet/api.py:1149-1377", "Oagnet/agent.py:6871-7163"],
        },
        {
            "service": "sql-translator",
            "repository": str(ROOT.parent / "sql-translator"),
            "runtime_endpoint": "http://127.0.0.1:48000",
            "role": "ASL validation, SQL translation, read-only execution",
            "source_obtained": True,
            "observed_health": "ok; build 2.1.0",
            "evidence": ["sql-translator/api_server_prod.py:153-410", "sql-translator/sql_translator_prod.py:2860-3186"],
        },
        {
            "service": "MySQL semantic catalog",
            "repository": None,
            "runtime_endpoint": "REDACTED_PRIVATE_ENDPOINT",
            "role": "authoritative semantic model definition",
            "source_obtained": False,
            "observed_health": "read path succeeded for model 81/domain 205",
            "evidence": ["Oagnet/mysql_tool.py:get_dsl_by_scope"],
        },
        {
            "service": "Milvus semantic index",
            "repository": None,
            "runtime_endpoint": "REDACTED_PRIVATE_ENDPOINT",
            "role": "semantic candidate and entity-value vector retrieval",
            "source_obtained": False,
            "observed_health": "healthy; dimension 1024",
            "evidence": ["Oagnet/config.py:44-125", "Oagnet/api.py:/vector/health"],
        },
    ]


def contracts() -> dict:
    return {
        "scope": {"semantic_model_id": 81, "business_domain_id": 205},
        "caller_contract": {
            "location": "app/services/intent_asl_contract.py:62-202",
            "fields": [
                "intent", "query_object", "metric_required", "required_metric_names",
                "required_metric_codes", "required_projections", "required_groupings",
                "projection_mode", "relationship_anchor", "required_filters",
                "negative_constraints", "semantic_mentions", "sorting",
                "time_dimension_required", "time_policy", "canonical_time_range",
            ],
        },
        "oagnet_request": {
            "location": "Oagnet/api.py:1149-1260",
            "generation": "one chat-model invocation; one retry only for malformed JSON",
            "location_generation": "Oagnet/agent.py:7022-7163",
        },
        "asl": {
            "version": "1.0",
            "location": "Oagnet/prompt_build.py:10-180",
            "operations": ["subject", "metrics", "dimensions", "filters", "time_context", "sort", "limit", "having", "projection_mode", "ambiguity"],
            "missing_first_class": ["comparison", "window", "offset", "cursor", "subquery", "explicit_join", "result_contract", "typed_semantic_roles"],
        },
        "translator": {
            "location": "sql-translator/sql_translator_prod.py:2860-3186",
            "accepts": ["subject", "metrics", "dimensions", "filters", "time_context", "sort", "limit", "having", "projection_mode"],
            "join_policy": "catalog relation path; rejects known 1:N aggregation risk rather than pre-aggregating",
        },
        "result_contract": {
            "location": "sql-translator/analysis_contract.py:19-160",
            "coverage": "six analysis operators; required columns and row bounds; price-volume normalization",
            "missing": ["generic sort proof", "grain proof", "requested projection completeness", "comparison/window proof"],
        },
    }


def schema_draft() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:data-agent:typed-logical-plan:draft:v0",
        "title": "TypedLogicalPlanDraft",
        "type": "object",
        "required": ["version", "dialogue_act", "service_route", "analysis_goals", "query_shape", "subject", "projections", "filters", "time", "result_contract", "provenance"],
        "properties": {
            "version": {"const": "0.1-draft"},
            "dialogue_act": {"enum": ["NEW_REQUEST", "FOLLOW_UP", "CLARIFICATION_ANSWER", "CORRECTION", "REFRESH", "REVISE", "CANCEL", "CHAT"]},
            "service_route": {"enum": ["SEMANTIC_QUERY", "DATASET_TRANSFORM", "ANALYSIS", "REPORT", "CHAT"]},
            "analysis_goals": {"type": "array", "items": {"enum": ["LOOKUP", "AGGREGATE", "TREND", "RANK", "COMPARE", "COMPOSITION", "CORRELATION", "ANOMALY"]}},
            "query_shape": {"enum": ["DETAIL", "AGGREGATE", "TIME_SERIES", "TOP_N", "COMPARISON", "MULTI_TASK"]},
            "subject": {"$ref": "#/$defs/semanticRef"},
            "projections": {"type": "array", "items": {"$ref": "#/$defs/typedProjection"}},
            "groupings": {"type": "array", "items": {"$ref": "#/$defs/semanticRef"}},
            "filters": {"type": "array", "items": {"$ref": "#/$defs/filter"}},
            "time": {"$ref": "#/$defs/timeSemantics"},
            "sort": {"type": "array", "items": {"type": "object", "required": ["ref", "direction"], "properties": {"ref": {"$ref": "#/$defs/semanticRef"}, "direction": {"enum": ["ASC", "DESC"]}}}},
            "limit": {"type": ["integer", "null"], "minimum": 1},
            "comparison": {"type": ["object", "null"]},
            "result_contract": {"type": "object", "required": ["grain", "required_columns"], "properties": {"grain": {"type": "array", "items": {"$ref": "#/$defs/semanticRef"}}, "required_columns": {"type": "array", "items": {"type": "string"}}, "ordering": {"type": "array"}, "row_bounds": {"type": "object"}}},
            "provenance": {"type": "array", "items": {"type": "object", "required": ["span", "source", "confidence"], "properties": {"span": {"type": "string"}, "source": {"enum": ["USER", "CONTEXT", "CATALOG", "SYSTEM_DEFAULT", "MODEL"]}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}},
        },
        "$defs": {
            "semanticRef": {"type": "object", "required": ["role", "canonical_id", "canonical_code", "display_name"], "properties": {"role": {"enum": ["ENTITY", "METRIC", "DIMENSION", "ATTRIBUTE", "ENTITY_VALUE", "RELATION"]}, "canonical_id": {"type": "string"}, "canonical_code": {"type": "string"}, "display_name": {"type": "string"}, "mention_span": {"type": ["string", "null"]}}},
            "typedProjection": {"allOf": [{"$ref": "#/$defs/semanticRef"}, {"type": "object", "properties": {"aggregation": {"enum": ["NONE", "SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MIN", "MAX", "RATIO"]}}}]},
            "filter": {"type": "object", "required": ["field", "operator", "value"], "properties": {"field": {"$ref": "#/$defs/semanticRef"}, "operator": {"enum": ["EQ", "NE", "IN", "NOT_IN", "GT", "GTE", "LT", "LTE", "LIKE", "BETWEEN"]}, "value": {}}},
            "timeSemantics": {"type": "object", "required": ["anchor", "range", "grain", "boundary"], "properties": {"anchor": {"$ref": "#/$defs/semanticRef"}, "range": {"type": ["object", "null"]}, "grain": {"enum": ["NONE", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"]}, "boundary": {"enum": ["LEFT_CLOSED_RIGHT_OPEN", "CLOSED"]}, "timezone": {"type": "string"}, "source": {"enum": ["USER", "CONTEXT", "SYSTEM_DEFAULT"]}}},
        },
    }


def classify_failure(case: dict) -> tuple[str, str, bool]:
    n = int(case["case_id"].split("-")[-1])
    if n in {1, 4, 14, 15, 21}:
        return "SLOT_MERGE_FAILURE", "SLOT_MERGE", True
    if n in {2, 5, 22}:
        return "QUERY_SHAPE_FAILURE", "STRUCTURED_INTENT", True
    if n in {6, 7, 8, 9, 11, 23, 24, 25, 26}:
        return "ASL_CONTRACT_FAILURE", "ASL", True
    if n in {16, 18}:
        return "RELATION_PROJECTION_FAILURE", "ASL", True
    if n in {19, 28}:
        return "RELATION_PROJECTION_FAILURE", "UNKNOWN_ASL_OR_RESULT", False
    if n in {17, 20, 27, 29, 30}:
        return "DATASET_OPERATION_FAILURE", "DATASET_TRANSFORM", True
    if n == 3:
        return "RESULT_CONTRACT_FAILURE", "UNKNOWN_ASL_OR_RESULT", False
    return "NO_DATA_OR_SEMANTIC_UNCERTAIN", "UNKNOWN", False


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    dump_json("external_service_inventory.json", external_inventory())
    dump_json("asl_nl2sql_contracts.json", contracts())
    dump_json("typed_logical_plan_schema_draft.json", schema_draft())

    gap_rows = [
        {"capability": "subject", "caller": "YES", "asl": "YES", "translator": "YES", "result_validation": "PARTIAL", "status": "CURRENT_FACT"},
        {"capability": "multiple_metrics", "caller": "YES", "asl": "YES", "translator": "YES", "result_validation": "PARTIAL", "status": "CURRENT_FACT"},
        {"capability": "time_grain", "caller": "BOOLEAN/POLICY", "asl": "dimension.granularity", "translator": "YES", "result_validation": "NO", "status": "CURRENT_FACT"},
        {"capability": "order_limit", "caller": "YES", "asl": "YES", "translator": "YES", "result_validation": "PARTIAL", "status": "CURRENT_FACT"},
        {"capability": "relation_target", "caller": "relationship_anchor", "asl": "relation-completed dimensions/filters", "translator": "inferred catalog path", "result_validation": "NO", "status": "CURRENT_FACT"},
        {"capability": "comparison", "caller": "analysis operator only", "asl": "NO_FIRST_CLASS", "translator": "NO_FIRST_CLASS", "result_validation": "SPECIAL_CASE_ONLY", "status": "CURRENT_FACT"},
        {"capability": "window", "caller": "NO", "asl": "NO", "translator": "NO", "result_validation": "NO", "status": "CURRENT_FACT"},
        {"capability": "cursor_offset", "caller": "canonical request only", "asl": "NO", "translator": "NO", "result_validation": "NO", "status": "CURRENT_FACT"},
        {"capability": "one_to_many_safety", "caller": "NO", "asl": "NO", "translator": "REJECT_ONLY", "result_validation": "NO", "status": "CURRENT_FACT"},
    ]
    dump_csv("asl_nl2sql_gap_matrix.csv", gap_rows, list(gap_rows[0]))

    catalog, catalog_error = load_catalog()
    entities = catalog.get("entities", [])
    metrics = catalog.get("metrics", [])
    dimensions = catalog.get("dimensions", [])
    attrs = [a for e in entities for a in (e.get("attributes") or [])]
    relations = [r for e in entities for r in (e.get("relations") or [])]
    catalog_inventory = {
        "status": "CURRENT_FACT" if catalog else "UNKNOWN",
        "generated_at": generated_at,
        "read_mode": "read-only Oagnet get_dsl_by_scope",
        "scope": {"semantic_model_id": 81, "business_domain_id": 205},
        "error": catalog_error,
        "counts": {"entities": len(entities), "metrics": len(metrics), "dimensions": len(dimensions), "attributes": len(attrs), "relations": len(relations)},
        "metrics": metrics,
        "dimensions": dimensions,
        "entities": entities,
    }
    dump_json("semantic_catalog_inventory.json", catalog_inventory)
    dump_json("metric_dimension_compatibility.json", [
        {"metric_code": m.get("metric_code"), "metric_name": m.get("metric_name"), "bind_dimensions": sorted(set(m.get("bind_dimensions") or [])), "source": "catalog_declared", "status": "CURRENT_FACT"}
        for m in metrics
    ])
    dump_json("entity_relationship_inventory.json", [
        {"source_entity": e.get("entity_code"), "source_name": e.get("entity_name"), "relations": e.get("relations") or []}
        for e in entities
    ])
    quality_rows = []
    for m in metrics:
        calc = m.get("calculation_rule") or {}
        time_caliber = m.get("time_caliber") or {}
        quality_rows.append({
            "object_type": "metric", "code": m.get("metric_code"), "name": m.get("metric_name"),
            "has_definition": bool((m.get("business_definition") or {}).get("description")),
            "has_formula": bool(calc.get("calc_formula")), "has_bindings": bool(m.get("bind_dimensions")),
            "has_time_anchor": bool(time_caliber.get("time_anchor")), "has_explicit_additivity": False,
            "finding": "aggregation can be inferred from formula but additivity is not explicit",
        })
    for d in dimensions:
        quality_rows.append({
            "object_type": "dimension", "code": d.get("dim_code"), "name": d.get("dim_name"),
            "has_definition": bool((d.get("business_definition") or {}).get("description")),
            "has_formula": "", "has_bindings": bool(d.get("bind_entities")),
            "has_time_anchor": "", "has_explicit_additivity": "",
            "finding": "granularity declared" if d.get("granularity_support") else "no granularity expected or declared",
        })
    dump_csv("catalog_quality_summary.csv", quality_rows, list(quality_rows[0]) if quality_rows else ["object_type"])

    candidate_examples = [
        {"example_id": "SC-001", "input": "上海市", "requested_role": "ENTITY_VALUE", "candidate_source": "catalog surface audit", "candidates": ["city/上海市", "province/上海市"], "decision": "requires field/role context", "rejection_reasons_available": False, "runtime_mode": "catalog-derived; not live recall"},
        {"example_id": "SC-002", "input": "销售额", "requested_role": "METRIC", "candidate_source": "metric synonyms", "candidates": ["annual_total_sales/含税销售总额"], "decision": "canonical metric", "rejection_reasons_available": False, "runtime_mode": "catalog-derived; not live recall"},
        {"example_id": "SC-003", "input": "商品", "requested_role": "DIMENSION", "candidate_source": "cross-type surface audit", "candidates": ["dimension/product", "entity/product"], "decision": "role-first retrieval required", "rejection_reasons_available": False, "runtime_mode": "catalog-derived; not live recall"},
    ]
    dump_jsonl("semantic_candidate_examples.jsonl", candidate_examples)
    role_rows = [
        {"surface": "城市", "roles": "DIMENSION|ENTITY", "risk": "HIGH", "resolution": "use requested semantic role and field ownership"},
        {"surface": "经销商", "roles": "DIMENSION|ENTITY", "risk": "HIGH", "resolution": "use query shape plus requested role"},
        {"surface": "商品", "roles": "DIMENSION|ENTITY", "risk": "HIGH", "resolution": "use requested semantic role"},
        {"surface": "商品名称", "roles": "ATTRIBUTE|DIMENSION_ALIAS", "risk": "MEDIUM", "resolution": "attribute for projection/filter; dimension for grouping"},
        {"surface": "数量", "roles": "ATTRIBUTE|METRIC", "risk": "HIGH", "resolution": "aggregate semantics must be explicit"},
        {"surface": "交易日期", "roles": "ATTRIBUTE|DIMENSION", "risk": "MEDIUM", "resolution": "time semantic role plus grain"},
    ]
    dump_csv("semantic_role_confusion_matrix.csv", role_rows, list(role_rows[0]))

    failure_rows = []
    with (ROOT / "docs" / "data_agent_failure_cases.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            source_case = json.loads(line)
            category, first_stage, located = classify_failure(source_case)
            failure_rows.append({
                "case_id": source_case["case_id"], "source": source_case["source"],
                "input": source_case["current_user_message"], "historical_actual": source_case["actual_response"],
                "historical_expected": source_case["expected_behavior"], "replay_mode": "PARTIAL_HISTORICAL_TRACE",
                "runtime_replayed": False, "reason_not_full_replay": "original structured history/state/model outputs absent",
                "first_divergence_category": category, "first_divergence_stage": first_stage,
                "first_divergence_located": located, "evidence_status": "INFERENCE" if located else "UNKNOWN",
            })
    dump_jsonl("failure_trace_replay.jsonl", failure_rows)
    stage_counts = Counter(row["first_divergence_stage"] for row in failure_rows)
    distribution = [{"stage": stage, "case_count": count, "percent": round(count * 100 / len(failure_rows), 2)} for stage, count in sorted(stage_counts.items())]
    dump_csv("failure_stage_distribution.csv", distribution, ["stage", "case_count", "percent"])

    intent_rows = [
        {"axis": "legacy_intent", "from": "DETAIL_QUERY", "to": "METRIC_QUERY", "observed_count": 3, "source": "real-002,real-005,real-022", "cause": "query shape and analysis goal collapsed into one label"},
        {"axis": "turn_relation", "from": "FOLLOW_UP_ADD", "to": "standalone/replace", "observed_count": 5, "source": "real-001,004,014,015,021", "cause": "slot merge semantics not first-class"},
        {"axis": "dataset_operation", "from": "LIMIT/SORT_FOLLOWUP", "to": "new semantic query", "observed_count": 5, "source": "real-017,020,027,029,030", "cause": "service route not separated from query intent"},
    ]
    dump_csv("intent_confusion_matrix.csv", intent_rows, list(intent_rows[0]))
    slot_rows = [
        {"slot": "metric", "expected_role": "METRIC", "actual_failure": "lost on ADD/REPLACE", "cases": "real-001,004,014,015,021"},
        {"slot": "time grain", "expected_role": "TIME_GRAIN", "actual_failure": "not projected in ASL", "cases": "real-006,007,008,009,011,023,024,025,026"},
        {"slot": "relation projection", "expected_role": "RELATION_TARGET", "actual_failure": "missing/wrong projection", "cases": "real-016,018,019,028"},
        {"slot": "limit/sort", "expected_role": "DATASET_OPERATION", "actual_failure": "not applied to prior dataset", "cases": "real-017,020,027,029,030"},
    ]
    dump_csv("slot_role_confusion_matrix.csv", slot_rows, list(slot_rows[0]))

    eval_files = [ROOT / "evals" / "intent_realistic_100.json", ROOT / "evals" / "intent_realistic_extra_100.json"]
    gold = []
    for path in eval_files:
        for item in json.loads(path.read_text(encoding="utf-8")):
            gold.append({
                "case_id": f"{path.stem}:{item['id']}", "source": str(path.relative_to(ROOT)).replace("\\", "/"),
                "question": item["question"], "legacy_expected_intent": item["expected"],
                "annotation_level": "PARTIAL_GOLD_EXISTING_ASSERTION", "ground_truth": True,
                "dialogue_act": None, "service_route": None, "analysis_goals": [], "query_shape": None,
                "semantic_roles": [], "review_status": "NEEDS_BUSINESS_REVIEW_FOR_MULTI_AXIS_LABELS",
            })
    dump_jsonl("gold_cases_seed.jsonl", gold)
    synthetic = [
        {"case_id": "syn-001", "question": "再加上订单笔数，其他条件不变", "purpose": "FOLLOW_UP_ADD metric reducer", "synthetic_proposal": True, "ground_truth": False},
        {"case_id": "syn-002", "question": "改为按月，并保留原来的指标", "purpose": "REPLACE time grain while preserving slots", "synthetic_proposal": True, "ground_truth": False},
        {"case_id": "syn-003", "question": "只看前5条", "purpose": "dataset operation route", "synthetic_proposal": True, "ground_truth": False},
        {"case_id": "syn-004", "question": "TDC‑3的主要适用科室和次要适用科室", "purpose": "Unicode normalization and relation target", "synthetic_proposal": True, "ground_truth": False},
        {"case_id": "syn-005", "question": "上海市各经销商已合作医院数", "purpose": "geo value and role disambiguation", "synthetic_proposal": True, "ground_truth": False},
    ]
    dump_jsonl("synthetic_test_proposals.jsonl", synthetic)

    eval_spec = {
        "status": "PROPOSAL",
        "metrics": [
            {"name": "dialogue_act_macro_f1", "unit": "case", "requires": "multi-axis gold"},
            {"name": "service_route_accuracy", "unit": "case", "requires": "route gold"},
            {"name": "slot_role_f1", "unit": "mention", "requires": "mention spans and canonical role IDs"},
            {"name": "canonical_resolution_top1_accuracy", "unit": "mention", "requires": "catalog snapshot"},
            {"name": "candidate_recall_at_k", "unit": "mention", "requires": "pre-rerank candidates"},
            {"name": "logical_plan_exact_match", "unit": "case", "requires": "typed plan gold"},
            {"name": "execution_answer_accuracy", "unit": "case", "requires": "frozen data snapshot"},
            {"name": "clarification_precision", "unit": "turn", "requires": "whether clarification was necessary"},
            {"name": "first_divergence_accuracy", "unit": "failure", "requires": "complete trace"},
            {"name": "state_isolation_violation_rate", "unit": "conversation", "requires": "multi-tenant concurrency suite"},
        ],
        "gates": {"no_regression": ["service_route_accuracy", "execution_answer_accuracy", "state_isolation_violation_rate"], "thresholds": "UNKNOWN_NEEDS_BASELINE"},
    }
    dump_json("evaluation_metric_spec.json", eval_spec)

    repo_rows = [
        {"repository": "DataAnalysis_Agent", "current_role": "conversation, intent, caller contract, result assembly", "proposed_change": "multi-axis parse, slot reducer, typed plan/result contract", "dependency": "Oagnet and sql-translator contract versioning", "risk": "HIGH", "phase": "1-3"},
        {"repository": "Oagnet", "current_role": "semantic retrieval and ASL generation", "proposed_change": "role-first N-best resolution with provenance and typed-plan adapter", "dependency": "catalog stable IDs", "risk": "HIGH", "phase": "1-3"},
        {"repository": "sql-translator", "current_role": "ASL validation/translation/execution", "proposed_change": "typed plan adapter, grain safety, expanded result proof", "dependency": "ASL v2 compatibility", "risk": "HIGH", "phase": "2-4"},
        {"repository": "semantic catalog/MySQL", "current_role": "authoritative definitions", "proposed_change": "additivity, grain, compatibility and relation governance", "dependency": "business sign-off", "risk": "MEDIUM", "phase": "0-2"},
        {"repository": "Milvus", "current_role": "candidate index", "proposed_change": "versioned role indexes and traceable candidate metadata", "dependency": "dual-read/reindex", "risk": "MEDIUM", "phase": "1-2"},
    ]
    dump_csv("cross_repository_change_matrix.csv", repo_rows, list(repo_rows[0]))

    proposals = [
        ("P001", "引入多轴意图", ["real-002", "real-017"], "HIGH"),
        ("P002", "保存Mention Span", ["app/intent/structured.py:193-391"], "MEDIUM"),
        ("P003", "显式Semantic Role", ["semantic_role_confusion_matrix.csv"], "HIGH"),
        ("P004", "N-best Plan", ["Oagnet/prompt_build.py:984-1149"], "MEDIUM"),
        ("P005", "Typed Logical Plan", ["asl_nl2sql_gap_matrix.csv"], "HIGH"),
        ("P006", "ResultContract", ["real-003", "sql-translator/analysis_contract.py:19-160"], "HIGH"),
        ("P007", "Slot Reducer", ["real-001", "real-004"], "HIGH"),
        ("P008", "Topic Tree", ["real-021", "app/services/orchestrator.py"], "MEDIUM"),
        ("P009", "Task Version", ["real-017", "app/domain/models.py"], "MEDIUM"),
        ("P010", "Session Glossary", ["semantic_role_confusion_matrix.csv"], "LOW"),
        ("P011", "指标语义代数", ["catalog_quality_summary.csv"], "HIGH"),
        ("P012", "时间语义", ["real-006", "Oagnet/prompt_build.py:10-180"], "HIGH"),
        ("P013", "语义关系图", ["Oagnet/prompt_build.py:807-982"], "HIGH"),
        ("P014", "内部修复循环", ["Oagnet/agent.py:7022-7163"], "MEDIUM"),
        ("P015", "完整Trace", ["failure_trace_replay.jsonl"], "HIGH"),
        ("P016", "新评测体系", ["evaluation_metric_spec.json"], "HIGH"),
    ]
    dump_json("architecture_evidence_map.json", [
        {"proposal_id": pid, "proposal": proposal, "supported_by": [{"type": "evidence", "ref": ref} for ref in refs], "confidence": confidence, "remaining_unknowns": ["business acceptance threshold"] if confidence != "HIGH" else [], "affected_repositories": ["DataAnalysis_Agent", "Oagnet", "sql-translator"]}
        for pid, proposal, refs, confidence in proposals
    ])

    print(json.dumps({"output": str(OUT), "files": len(list(OUT.iterdir())), "catalog_error": catalog_error, "counts": catalog_inventory["counts"], "gold_cases": len(gold), "failure_cases": len(failure_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
