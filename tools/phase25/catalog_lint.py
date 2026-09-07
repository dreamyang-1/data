"""Offline semantic catalog linter for a previously captured local snapshot.

The tool has no database, Milvus, Redis, MinIO, or network imports.  It only
reads a JSON snapshot and writes/prints deterministic findings.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


UNKNOWN = "UNKNOWN_NEEDS_OWNER_REVIEW"


def lint_catalog(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return evidence-backed findings without inferring business semantics."""

    findings: list[dict[str, Any]] = []
    entities = snapshot.get("entities", [])
    metrics = snapshot.get("metrics", [])
    dimensions = snapshot.get("dimensions", [])

    def add(code: str, severity: str, subject: str, evidence: Any) -> None:
        findings.append({
            "code": code,
            "severity": severity,
            "subject": subject,
            "evidence": evidence,
            "owner_review_status": UNKNOWN,
        })

    relation_codes: list[str] = []
    for entity in entities:
        entity_code = str(entity.get("entity_code") or "<unknown>")
        if not entity.get("primary_key"):
            add("ENTITY_PRIMARY_KEY_MISSING", "HIGH", entity_code, "primary_key is empty")
        if not str(entity.get("update_frequency") or "").strip():
            add("UPDATE_FREQUENCY_MISSING", "MEDIUM", entity_code, "update_frequency is empty")
        for attribute in entity.get("attributes", []) or []:
            attribute_code = f"{entity_code}.{attribute.get('attr_code') or '<unknown>'}"
            if not str(attribute.get("data_type") or "").strip():
                add("ATTRIBUTE_DATA_TYPE_MISSING", "HIGH", attribute_code, "data_type is empty")
            if attribute.get("is_primary_key") and not attribute.get("is_unique"):
                add("KEY_UNIQUENESS_UNDECLARED", "HIGH", attribute_code, {
                    "is_primary_key": True, "is_unique": attribute.get("is_unique")
                })
        for relation in entity.get("relations", []) or []:
            code = str(relation.get("relation_code") or "<unknown>")
            relation_codes.append(code)
            if not relation.get("relation_constraint"):
                add("RELATION_CONSTRAINT_MISSING", "HIGH", code, "relation_constraint is empty")
    for code, count in Counter(relation_codes).items():
        if count > 1:
            add("DUPLICATE_RELATION_CODE", "HIGH", code, {"count": count})

    for metric in metrics:
        code = str(metric.get("metric_code") or "<unknown>")
        formula = str((metric.get("calculation_rule") or {}).get("calc_formula") or "")
        left = formula.split("=", 1)[0].strip() if "=" in formula else ""
        if left and left != code:
            add("METRIC_FORMULA_LHS_MISMATCH", "HIGH", code, {"formula_lhs": left})
        time_anchor = (metric.get("time_caliber") or {}).get("time_anchor")
        if not time_anchor:
            add("METRIC_TIME_ANCHOR_MISSING", "HIGH", code, "time_anchor is empty")
        permission = metric.get("permission_config") or {}
        if not permission.get("view_roles") and permission.get("data_limit") is None:
            add("METRIC_PERMISSION_EMPTY", "HIGH", code, "view_roles and data_limit are empty")
        if metric.get("bind_dimensions"):
            add(
                "METRIC_DIMENSION_JOIN_SAFETY_UNPROVEN",
                "HIGH",
                code,
                {"declared_dimensions": metric.get("bind_dimensions")},
            )

    return {
        "schema_version": "1.0",
        "source_status": snapshot.get("status", "UNKNOWN"),
        "scope": snapshot.get("scope", {}),
        "summary": {
            "finding_count": len(findings),
            "by_code": dict(sorted(Counter(item["code"] for item in findings).items())),
            "highest_severity": "HIGH" if any(item["severity"] == "HIGH" for item in findings) else "MEDIUM",
        },
        "findings": findings,
        "write_operations_performed": False,
    }


def main() -> int:
    """Read a snapshot and emit a JSON lint report."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = lint_catalog(json.loads(args.snapshot.read_text(encoding="utf-8-sig")))
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
