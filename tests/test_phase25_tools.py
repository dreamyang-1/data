"""Offline-only tests for Phase 2.5 audit tools."""

from __future__ import annotations

import json
from pathlib import Path

from tools.phase25.catalog_lint import lint_catalog
from tools.phase25.run_semantic_v2_shadow import compare_record, load_records, run


def test_catalog_lint_marks_unknown_business_rules_without_mutating_snapshot() -> None:
    snapshot = {
        "status": "CURRENT_FACT",
        "scope": {"semantic_model_id": 81},
        "entities": [{
            "entity_code": "sales_order",
            "primary_key": None,
            "update_frequency": "",
            "attributes": [],
            "relations": [],
        }],
        "metrics": [],
        "dimensions": [],
    }
    before = json.dumps(snapshot, sort_keys=True)
    report = lint_catalog(snapshot)
    assert report["write_operations_performed"] is False
    assert all(
        item["owner_review_status"] == "UNKNOWN_NEEDS_OWNER_REVIEW"
        for item in report["findings"]
    )
    assert json.dumps(snapshot, sort_keys=True) == before


def test_catalog_lint_reports_duplicate_relation_codes() -> None:
    relation = {
        "relation_code": "sales_order_product",
        "relation_constraint": None,
        "target_entity": "product",
    }
    report = lint_catalog({
        "entities": [{
            "entity_code": "sales_order", "primary_key": "id",
            "update_frequency": "daily", "attributes": [],
            "relations": [relation, relation],
        }],
        "metrics": [], "dimensions": [],
    })
    assert "DUPLICATE_RELATION_CODE" in report["summary"]["by_code"]


def test_shadow_record_without_fixture_is_unavailable() -> None:
    result = compare_record({"question": "查询销售额"})
    assert result["mode"] == "UNAVAILABLE"
    assert result["v2_plan"] is None


def test_shadow_invalid_fixture_is_partial_not_silently_accepted() -> None:
    result = compare_record({"plan": {"schema_version": "0.2"}})
    assert result["mode"] == "PARTIAL"
    assert result["plan_validation_errors"]


def test_shadow_loads_json_and_jsonl_locally(tmp_path: Path) -> None:
    single = tmp_path / "single.json"
    many = tmp_path / "many.jsonl"
    single.write_text('{"question":"a"}', encoding="utf-8")
    many.write_text('{"question":"a"}\n{"question":"b"}\n', encoding="utf-8")
    assert len(load_records(single)) == 1
    assert len(load_records(many)) == 2
    assert [item["mode"] for item in run(load_records(many))] == [
        "UNAVAILABLE", "UNAVAILABLE"
    ]
