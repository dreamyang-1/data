"""Deterministic ResultContract proof generation for shadow datasets."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from .enums import ProofStatus, SemanticRole, Severity
from .models import (ContractProof, ResultContract, OutputFieldRequirement, OrderingRequirement,
                     CardinalityExpectation, ProofRequirement, ProofCheck, RowBounds,
                     AliasedOutputFieldRequirement, RelationshipPathSpec, TemporalComparisonSpec)


class ResultContractCompiler:
    @staticmethod
    def compile(plan) -> ResultContract:
        payload = plan.payload
        outputs = []
        seen = set()
        temporal = payload.payload_type == 'COMPARISON' and isinstance(payload.comparison, TemporalComparisonSpec)

        def output(ref, role, identifier=None, entity_alias=None):
            if identifier is None:
                identifier = f'{role.value.lower()}:{getattr(ref, "canonical_id", getattr(ref, "local_id", ""))}'
                if entity_alias:
                    identifier += ':' + entity_alias
            if identifier not in seen:
                kind = AliasedOutputFieldRequirement if entity_alias else OutputFieldRequirement
                outputs.append(kind(output_field_id=identifier, semantic_ref=ref,
                    logical_role=role, display_label=ref.display_name, **({'entity_alias': entity_alias} if entity_alias else {})))
                seen.add(identifier)
            else:
                existing = next(o for o in outputs if o.output_field_id == identifier)
                if temporal or entity_alias or isinstance(existing, AliasedOutputFieldRequirement):
                    from .slot_reducer import semantic_fingerprint
                    if (getattr(existing, 'entity_alias', None) != entity_alias or existing.logical_role != role
                            or semantic_fingerprint(existing.semantic_ref) != semantic_fingerprint(ref)):
                        raise ValueError('RESULT_COMPARISON_OUTPUT_ID_COLLISION' if temporal else 'RESULT_OUTPUT_OCCURRENCE_ID_COLLISION')
            return identifier

        projection = getattr(payload, 'projection_spec', None)
        if payload.payload_type == 'DATASET_TRANSFORM' and payload.operation.operation_type == 'PROJECT':
            projection = payload.operation.projection_spec
        if projection:
            for item in projection.items:
                output(item.ref, item.role, item.output_field_id, getattr(item, 'entity_alias', None))
        for ref in ([] if temporal else getattr(payload, 'measures', [])):
            output(ref, SemanticRole.MEASURE)
        for ref in getattr(payload, 'group_by', []):
            output(ref, SemanticRole.GROUP_BY)
        time = getattr(payload, 'time', None)
        if time and (not temporal or time.grain != 'NONE') and payload.payload_type in {'TIME_SERIES', 'COMPARISON', 'ANOMALY', 'ROOT_CAUSE', 'FORECAST'}:
            output(time.anchor, SemanticRole.TIME_FIELD)
        if payload.payload_type == 'COMPARISON':
            if temporal:
                from .temporal_comparisons import comparison_outputs
                for item in comparison_outputs(payload.comparison):
                    if item.output_field_id in seen:
                        raise ValueError('RESULT_COMPARISON_OUTPUT_ID_COLLISION')
                    outputs.append(item); seen.add(item.output_field_id)
            else:
                for ref in payload.comparison.output_metrics:
                    output(ref, SemanticRole.MEASURE)
        ordering = []
        bounds = RowBounds()
        if payload.payload_type == 'RANKING':
            rank = payload.ranking
            field_id = next((o.output_field_id for o in outputs if getattr(o.semantic_ref, 'canonical_id', None) == rank.rank_by.canonical_id), None)
            field_id = field_id or output(rank.rank_by, SemanticRole.ORDER_BY)
            output(payload.ranking_target, SemanticRole.GROUP_BY)
            ordering.append(OrderingRequirement(output_field_id=field_id, ref=rank.rank_by,
                            direction=rank.direction, nulls_policy=rank.nulls_policy, ties_policy=rank.ties_policy))
            for ref in rank.stable_tiebreakers:
                ordering.append(OrderingRequirement(output_field_id=output(ref, SemanticRole.ORDER_BY), ref=ref, direction='ASC'))
            bounds = RowBounds(maximum=rank.limit if rank.ties_policy == 'EXCLUDE_TIES' else None)
        if payload.payload_type == 'RELATION_LIST':
            path = payload.relationship_spec
            output(payload.target_entity or payload.relation_target, SemanticRole.TARGET_ENTITY,
                entity_alias=path.nodes[-1].entity_alias if isinstance(path, RelationshipPathSpec) else None)
        limit = getattr(payload, 'limit', None)
        if payload.payload_type == 'DATASET_TRANSFORM' and payload.operation.operation_type == 'LIMIT':
            bounds = RowBounds(maximum=payload.operation.limit)
        elif limit:
            bounds = RowBounds(maximum=limit.limit)
        kind = ('SCALAR' if payload.payload_type == 'SCALAR_AGGREGATE' or (temporal and time.grain == 'NONE' and not payload.group_by) else
                'DISTINCT_TARGETS' if payload.payload_type == 'RELATION_LIST' else
                'TIME_PERIODS' if time and time.grain.value != 'NONE' else
                'GROUPS' if getattr(payload, 'group_by', []) else 'DETAIL_ROWS')
        if kind == 'SCALAR':
            bounds = RowBounds(maximum=1)
        checks = ['output_bindings', 'row_bounds', 'allow_empty', 'allow_truncated', 'numeric_constraints']
        if temporal:
            checks.append('temporal_comparison_arithmetic')
        if ordering:
            checks.append('required_ordering')
        if time and time.grain.value != 'NONE':
            checks.append('required_time_grain')
        unique_outputs = [o.output_field_id for o in outputs if o.logical_role == SemanticRole.TARGET_ENTITY
            or (temporal and o.logical_role in {SemanticRole.GROUP_BY, SemanticRole.TIME_FIELD})]
        if kind == 'DISTINCT_TARGETS' or (temporal and unique_outputs):
            checks.append('cardinality')
        return ResultContract(semantic_fingerprint=plan.semantic_fingerprint, required_outputs=outputs,
                              required_ordering=ordering, row_bounds=bounds,
                              required_time_grain=time.grain if time and time.grain.value != 'NONE' else None,
                              expected_cardinality=CardinalityExpectation(kind=kind, unique_output_field_ids=unique_outputs),
                              snapshot_requirement=plan.snapshot_requirement.data_snapshot_id,
                              proof_requirements=[ProofRequirement(check_id=c) for c in checks])


def prove_result_contract(
    contract: ResultContract,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    truncated: bool,
    output_bindings=(),
    actual_time_grain=None,
    snapshot_id=None,
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
            if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
                numeric_ok = False
                break
            if rule.finite_only and not math.isfinite(value):
                numeric_ok = False
                break
            comparable = value if isinstance(value, Decimal) else Decimal(str(value))
            if rule.minimum is not None and comparable < rule.minimum:
                numeric_ok = False
                break
            if rule.maximum is not None and comparable > rule.maximum:
                numeric_ok = False
                break
    checks["numeric_constraints"] = ProofStatus.PASS if numeric_ok else ProofStatus.FAIL
    if not numeric_ok:
        errors.append("numeric constraint failed")
    bindings = {}
    bindings_ok = True
    for binding in output_bindings:
        if (binding.output_field_id in bindings or binding.status != ProofStatus.PASS or
                binding.semantic_fingerprint != contract.semantic_fingerprint or
                binding.result_column_index >= len(columns) or
                columns[binding.result_column_index] != binding.result_column_name or
                binding.sql_alias != binding.result_column_name):
            bindings_ok = False
        bindings[binding.output_field_id] = binding.result_column_name
    for required in contract.required_outputs:
        if required.required and (required.output_field_id not in bindings or
                                  any(bindings[required.output_field_id] not in row for row in rows)):
            bindings_ok = False
        name = bindings.get(required.output_field_id)
        if name and required.expected_data_type:
            from datetime import date, datetime
            types = {'STRING': (str,), 'DECIMAL': (Decimal, int, float), 'BOOLEAN': (bool,), 'DATE': (date,), 'DATETIME': (datetime,)}
            if any(row.get(name) is not None and (not isinstance(row[name], types[required.expected_data_type]) or
                       (required.expected_data_type == 'DECIMAL' and isinstance(row[name], bool))) for row in rows):
                bindings_ok = False
    if contract.required_outputs:
        checks['output_bindings'] = ProofStatus.PASS if bindings_ok else ProofStatus.FAIL
        if not bindings_ok:
            errors.append('logical output binding proof missing or invalid')
    elif any(p.check_id == 'output_bindings' for p in contract.proof_requirements):
        checks['output_bindings'] = ProofStatus.PASS
    if any(p.check_id == 'temporal_comparison_arithmetic' for p in contract.proof_requirements):
        from .temporal_comparisons import comparison_arithmetic
        ok = bindings_ok and comparison_arithmetic(contract.required_outputs, bindings, rows)
        checks['temporal_comparison_arithmetic'] = ProofStatus.PASS if ok else ProofStatus.FAIL
        if not ok:
            errors.append('temporal comparison arithmetic or period output binding failed')
    if contract.required_time_grain is not None:
        checks['required_time_grain'] = (ProofStatus.UNKNOWN if actual_time_grain is None else
                                       ProofStatus.PASS if actual_time_grain == contract.required_time_grain else ProofStatus.FAIL)
    if contract.snapshot_requirement:
        checks['snapshot'] = ProofStatus.UNKNOWN if snapshot_id is None else ProofStatus.PASS if snapshot_id == contract.snapshot_requirement else ProofStatus.FAIL
    if contract.required_ordering:
        ordering_ok = bindings_ok and all(
            order.output_field_id in bindings and
            (order.nulls_policy != 'EXCLUDE' or all(row.get(bindings[order.output_field_id]) is not None for row in rows))
            for order in contract.required_ordering)
        from functools import cmp_to_key

        def compare(left, right):
            for order in contract.required_ordering:
                name = bindings.get(order.output_field_id)
                if name is None:
                    raise ValueError('missing ordering output binding')
                a, b = left.get(name), right.get(name)
                if a is None or b is None:
                    if order.nulls_policy == 'EXCLUDE':
                        raise ValueError('NULL excluded by ordering contract')
                    diff = 0 if a is b else (-1 if a is None else 1) * (1 if order.nulls_policy == 'FIRST' else -1)
                else:
                    diff = ((a > b) - (a < b)) * (1 if order.direction == 'ASC' else -1)
                if diff:
                    return diff
            return 0

        try:
            ordering_ok = ordering_ok and rows == sorted(rows, key=cmp_to_key(compare))
        except (ValueError, TypeError):
            ordering_ok = False
        checks['required_ordering'] = ProofStatus.PASS if ordering_ok else ProofStatus.FAIL
    cardinality = contract.expected_cardinality
    if cardinality and cardinality.unique_output_field_ids:
        names = [bindings.get(i) for i in cardinality.unique_output_field_ids]
        values = [tuple(row.get(n) for n in names) for row in rows]
        checks['cardinality'] = ProofStatus.PASS if all(names) and len(set(values)) == len(values) else ProofStatus.FAIL
    for keys in contract.uniqueness_keys:
        values = [tuple(row.get(k) for k in keys) for row in rows]
        checks['uniqueness:' + ','.join(keys)] = ProofStatus.PASS if all(k in columns for k in keys) and len(set(values)) == len(values) else ProofStatus.FAIL
    requirements = {r.check_id: r.severity for r in contract.proof_requirements}
    for requirement in contract.proof_requirements:
        checks.setdefault(requirement.check_id, ProofStatus.UNKNOWN)
    typed_checks = [ProofCheck(check_id=k, status=v, severity=requirements.get(k, Severity.BLOCKING)) for k, v in checks.items()]
    blocking = [c.status for c in typed_checks if c.severity == Severity.BLOCKING]
    status = ProofStatus.FAIL if errors or ProofStatus.FAIL in blocking else ProofStatus.UNKNOWN if any(s != ProofStatus.PASS for s in blocking) else ProofStatus.PASS
    return ContractProof(status=status, checks=typed_checks, errors=errors)


def completed_allowed(*proofs: ContractProof) -> bool:
    """Return true only when every blocking proof explicitly passes."""

    return bool(proofs) and all(proof.status == ProofStatus.PASS and not proof.errors and
                               all(c.status == ProofStatus.PASS for c in proof.checks if c.severity == Severity.BLOCKING)
                               for proof in proofs)
