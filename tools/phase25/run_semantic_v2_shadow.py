"""Offline semantic V2 shadow runner.

The runner only consumes local JSON/JSONL fixtures and catalog snapshots.  It
does not import the production orchestrator, model clients, databases, Redis,
Milvus, MinIO, or SQL execution adapters.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from app.semantic_v2.legacy_adapter import assess_legacy_adapter
from app.semantic_v2.models import PlanEnvelope
from app.semantic_v2.validators import validate_plan


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load one JSON object/list or a JSONL file from disk."""

    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.casefold() == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    value = json.loads(text)
    if isinstance(value, list):
        return [dict(item) for item in value]
    if isinstance(value, dict):
        return [value]
    raise ValueError("shadow input must contain a JSON object or object list")


def compare_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate a recorded V2 plan or report why a plan is unavailable."""

    raw_plan = record.get("v2_plan") or record.get("plan")
    legacy = record.get("legacy_interpretation") or {
        "legacy_expected_intent": record.get("legacy_expected_intent"),
        "question": record.get("question") or record.get("current_user_message"),
    }
    if not isinstance(raw_plan, dict):
        return {
            "mode": "UNAVAILABLE",
            "legacy_interpretation": legacy,
            "v2_plan": None,
            "legacy_adapter_report": None,
            "plan_validation_errors": ["record does not contain a recorded or fixture V2 plan"],
            "semantic_candidate_summary": {},
            "clarification_decision": None,
            "result_contract": None,
            "equivalence_comparison": {
                "status": "UNAVAILABLE",
                "reason": "no deterministic V2 fixture was supplied",
            },
        }
    try:
        plan = PlanEnvelope.model_validate(raw_plan)
    except Exception as exc:  # Pydantic validation errors are data, not runner failure.
        return {
            "mode": "PARTIAL",
            "legacy_interpretation": legacy,
            "v2_plan": raw_plan,
            "legacy_adapter_report": None,
            "plan_validation_errors": [str(exc)],
            "semantic_candidate_summary": {},
            "clarification_decision": None,
            "result_contract": None,
            "equivalence_comparison": {"status": "PARTIAL", "reason": "invalid V2 fixture"},
        }
    adapter = assess_legacy_adapter(plan)
    errors = validate_plan(plan)
    return {
        "mode": "MOCK" if plan.provenance.source == "MOCK" else "PARTIAL",
        "legacy_interpretation": legacy,
        "v2_plan": plan.model_dump(mode="json"),
        "legacy_adapter_report": adapter.model_dump(mode="json"),
        "plan_validation_errors": errors,
        "semantic_candidate_summary": {
            "mention_count": len(plan.mentions),
            "candidate_set_count": len(plan.semantic_candidates),
            "binding_count": len(plan.semantic_bindings),
        },
        "clarification_decision": (
            plan.clarification_decision.model_dump(mode="json")
            if plan.clarification_decision else None
        ),
        "result_contract": (
            plan.result_contract.model_dump(mode="json") if plan.result_contract else None
        ),
        "equivalence_comparison": {
            "status": "PARTIAL",
            "reason": "structural comparison only; no model or SQL execution is permitted",
            "safe_for_legacy_execution": adapter.can_execute_safely and not errors,
        },
    }


def run(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Process records deterministically and preserve input ordering."""

    return [compare_record(record) for record in records]


def main() -> int:
    """CLI entry point for local shadow fixtures."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--catalog-snapshot", type=Path)
    args = parser.parse_args()
    if args.catalog_snapshot:
        # Parsing proves the local snapshot is readable; its contents are never mutated.
        json.loads(args.catalog_snapshot.read_text(encoding="utf-8-sig"))
    results = run(load_records(args.input))
    output = "\n".join(json.dumps(item, ensure_ascii=False) for item in results) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
