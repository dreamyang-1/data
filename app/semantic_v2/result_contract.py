"""Deterministic ResultContract proof generation for shadow datasets."""

from __future__ import annotations

import math
from typing import Any

from .enums import ProofStatus
from .models import ContractProof, ResultContract


def prove_result_contract(
    contract: ResultContract,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    truncated: bool,
) -> ContractProof:
    """Prove structural result requirements without guessing business meaning."""

    checks: dict[str, ProofStatus] = {}
    errors: list[str] = []
    missing = [column for column in contract.required_columns if column not in columns]
    checks["required_columns"] = ProofStatus.FAIL if missing else ProofStatus.PASS
    if missing:
        errors.append("missing required columns: " + ", ".join(missing))
    row_count = len(rows)
    bounds_ok = row_count >= contract.row_bounds.minimum and (
        contract.row_bounds.maximum is None or row_count <= contract.row_bounds.maximum
    )
    checks["row_bounds"] = ProofStatus.PASS if bounds_ok else ProofStatus.FAIL
    if not bounds_ok:
        errors.append("row count violates contract bounds")
    empty_ok = contract.allow_empty or bool(rows)
    checks["allow_empty"] = ProofStatus.PASS if empty_ok else ProofStatus.FAIL
    if not empty_ok:
        errors.append("empty result is not allowed")
    truncation_ok = contract.allow_truncated or not truncated
    checks["allow_truncated"] = ProofStatus.PASS if truncation_ok else ProofStatus.FAIL
    if not truncation_ok:
        errors.append("truncated result is not allowed")
    numeric_ok = True
    for rule in contract.numeric_constraints:
        for row in rows:
            value = row.get(rule.column)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                numeric_ok = False
                break
            if rule.finite_only and isinstance(value, float) and not math.isfinite(value):
                numeric_ok = False
                break
            if rule.minimum is not None and value < rule.minimum:
                numeric_ok = False
                break
            if rule.maximum is not None and value > rule.maximum:
                numeric_ok = False
                break
    checks["numeric_constraints"] = ProofStatus.PASS if numeric_ok else ProofStatus.FAIL
    if not numeric_ok:
        errors.append("numeric constraint failed")
    status = ProofStatus.FAIL if errors else ProofStatus.PASS
    return ContractProof(status=status, checks=checks, errors=errors)


def completed_allowed(*proofs: ContractProof) -> bool:
    """Return true only when every blocking proof explicitly passes."""

    return bool(proofs) and all(proof.status == ProofStatus.PASS for proof in proofs)
