"""Loss-aware lowering to the actual ASL 2.0 plan-only SQL boundary.

The frozen legacy adapter and public transport remain unchanged. Every output
has a stable alias tied to ResultContract; unsupported semantics emit no ASL.
"""
from dataclasses import dataclass
from decimal import Decimal

from . import models as m
from .authorized_contract import SourceValueBindingEvidence, contract_digest
from .catalog_plans import _row
from .enums import CatalogType
from .pipeline import AuthorizedLogicalPlan
from .result_contract import ResultContractCompiler


class ASL2Unsupported(ValueError):
    """Bounded feature code; never a clarification request."""


def _require(value, code):
    if not value:
        raise ASL2Unsupported(code)


@dataclass(frozen=True)
class ASL2Lowering:
    status: str
    semantic_fingerprint: str
    asl: dict | None
    result_contract: m.ResultContract
    output_bindings: tuple[m.OutputBindingProof, ...] = ()
    blockers: tuple[str, ...] = ()
    implicit_row_cap: int | None = None
    can_execute_safely: bool = False
    mode: str = 'SHADOW_ONLY'
    display_labels: tuple[tuple[str, str | None], ...] = ()
    ordering_contract: dict | None = None
    filter_contract: dict | None = None
    time_storage_receipt: dict | None = None
    compilation_fingerprint: str | None = None

    def __post_init__(self):
        for name in ('asl', 'result_contract', 'output_bindings', 'ordering_contract', 'filter_contract', 'time_storage_receipt'):
            object.__setattr__(self, name, m.freeze_contract(getattr(self, name)))


class _Compiler:
    def __init__(self, session, plan, contract):
        self.session, self.plan, self.contract = session, plan, contract
        payload = plan.payload
        if payload.payload_type == 'DETAIL_ROWS':
            self.subject = _row(session, payload.source_entity)
        else:
            owners = [set(_row(session, o.semantic_ref).get('source_dependency', {}).get('bind_entity', []))
                      for o in contract.required_outputs if isinstance(o.semantic_ref, m.BoundSemanticRef)
                      and o.semantic_ref.catalog_type == 'METRIC']
            common = set.intersection(*owners) if owners else set()
            candidates = session.candidates(CatalogType.ENTITY)
            found = [session._rows[c['candidate_id']].metadata for c in candidates if c['canonical_code'] in common]
            _require(len(found) == 1, 'ASL2_SUBJECT_OWNERSHIP_UNRESOLVED')
            self.subject = found[0]

    def dimension_binding(self, ref):
        row = _row(self.session, ref)
        bindings = [b for b in row.get('bind_entities', [])
                    if str(b.get('entity')) == str(self.subject.get('entity_id'))]
        _require(len(bindings) == 1, 'ASL2_DIMENSION_OWNER_UNRESOLVED')
        binding = bindings[0]
        _require(all(binding.get(k) for k in ('attr', 'mappingTable', 'mappingColumn')),
                 'ASL2_DIMENSION_MAPPING_INCOMPLETE')
        return binding

    def field(self, ref):
        row = _row(self.session, ref)
        if ref.catalog_type == 'ATTRIBUTE':
            value = row.get('field_mapping')
        elif ref.catalog_type == 'PHYSICAL_COLUMN':
            value = str(row.get('parent')) + '.' + str(row.get('field_name'))
        elif ref.catalog_type == 'DIMENSION':
            binding = self.dimension_binding(ref)
            value = binding['mappingTable'] + '.' + binding['mappingColumn']
        else:
            raise ASL2Unsupported('ASL2_FILTER_FIELD_TYPE_UNSUPPORTED')
        _require(isinstance(value, str) and value.count('.') == 1, 'ASL2_PHYSICAL_MAPPING_REQUIRED')
        return value

    def value(self, value, field_ref):
        if isinstance(value, m.EntityValueRef):
            proof = next((p for p in self.plan.permission_proofs
                          if isinstance(p, SourceValueBindingEvidence) and p.ref == value.ref), None)
            if proof:
                return proof.canonical_value
            row = _row(self.session, value.ref)
            field = _row(self.session, field_ref)
            _require(field_ref.catalog_type == 'DIMENSION' and row.get('parent') == field.get('dim_code'),
                     'ASL2_ENUM_FIELD_OWNERSHIP_UNPROVEN')
            _require(isinstance(row.get('code'), str), 'ASL2_ENUM_VALUE_UNPROVEN')
            return row['code']
        if isinstance(value, (m.ListValue, m.RangeValue)):
            values = value.values if isinstance(value, m.ListValue) else [value.start, value.end]
            return [self.value(item, field_ref) for item in values]
        if isinstance(value, m.NumberValue):
            if value.value == value.value.to_integral_value():
                return int(value.value)
            number = float(value.value)
            _require(Decimal(str(number)) == value.value, 'ASL2_DECIMAL_PRECISION_UNREPRESENTABLE')
            return number
        if isinstance(value, m.DateTimeValue):
            raise ASL2Unsupported('ASL2_DATETIME_STORAGE_TIMEZONE_UNPROVEN')
        if isinstance(value, m.DateValue):
            return value.value.isoformat()
        if isinstance(value, (m.StringValue, m.BooleanValue, m.EnumValue)):
            return value.value
        if isinstance(value, m.NullValue):
            return None
        raise ASL2Unsupported('ASL2_NULL_VALUE_UNSUPPORTED')

    def filter_tree(self, expression):
        if expression is None:
            return None
        if isinstance(expression, m.BooleanFilterGroup):
            return dict(operator=expression.operator, children=[self.filter_tree(child) for child in expression.children])
        _require(not isinstance(expression, m.AliasedPredicate), 'ASL2_ENTITY_OCCURRENCE_UNSUPPORTED')
        operators = {'EQ':'=', 'NE':'!=', 'GT':'>', 'GTE':'>=', 'LT':'<', 'LTE':'<=',
                     'IN':'IN', 'NOT_IN':'NOT IN', 'LIKE':'LIKE', 'BETWEEN':'BETWEEN',
                     'NOT_LIKE':'NOT LIKE', 'IS_NULL':'IS NULL', 'IS_NOT_NULL':'IS NOT NULL'}
        _require(expression.operator in operators, 'ASL2_FILTER_OPERATOR_UNSUPPORTED')
        return dict(field=self.field(expression.field_ref), operator=operators[expression.operator],
                    value=self.value(expression.value, expression.field_ref))

    def filters(self, expression):
        tree = self.filter_tree(expression)
        def mode_dependent(value):
            if isinstance(value, list): return any(mode_dependent(v) for v in value)
            return isinstance(value, str) and ('\\' in value or '\x00' in value)
        def legacy(node):
            if node is None: return []
            if 'children' in node:
                if node['operator'] != 'AND': return None
                branches = [legacy(child) for child in node['children']]
                return None if any(b is None for b in branches) else [leaf for b in branches for leaf in b]
            return None if node['operator'] in {'NOT LIKE', 'IS NULL', 'IS NOT NULL'} or mode_dependent(node['value']) else [node]
        leaves = legacy(tree)
        # Keep ASL's existing shape; a private contract is mandatory when the
        # public AND-only list cannot represent the expression.
        self.filter_contract = None if leaves is not None else dict(contract='pinned-filter-tree-v1',
            semantic_fingerprint=self.plan.semantic_fingerprint, expression=tree)
        return leaves if leaves is not None else []

    def build(self):
        payload = self.plan.payload
        asl = dict(version='2.0', intent='query', subject={'entity':self.subject['entity_code']},
            metrics=[], dimensions=[], filters=self.filters(payload.filters), time_context=None,
            sort=None, limit=None, having=[], ambiguity=[], projection_mode='ROWS')
        planned = []
        for output in self.contract.required_outputs:
            ref = output.semantic_ref
            _require(isinstance(ref, m.BoundSemanticRef), 'ASL2_COMPUTED_OUTPUT_UNSUPPORTED')
            _require(not isinstance(output, (m.AliasedOutputFieldRequirement, m.TemporalOutputFieldRequirement)),
                     'ASL2_OUTPUT_OCCURRENCE_UNSUPPORTED')
            alias = 'v2_' + contract_digest([self.plan.semantic_fingerprint, output.output_field_id])[:40]
            if ref.catalog_type == 'METRIC' and output.logical_role in {'MEASURE', 'ORDER_BY'}:
                item = dict(name=ref.canonical_code, alias=alias, time_anchor=None)
                asl['metrics'].append(item); family = 'metrics'
            elif ref.catalog_type in {'ATTRIBUTE', 'PHYSICAL_COLUMN', 'DIMENSION'}:
                if ref.catalog_type == 'DIMENSION':
                    binding = self.dimension_binding(ref)
                    name, attr = ref.canonical_code, str(binding['attr'])
                else:
                    name, attr = self.field(ref), None
                item = dict(name=name, attr=attr, alias=alias, level=None, granularity=None,
                            include_null_group=True)
                asl['dimensions'].append(item); family = 'dimensions'
            else:
                raise ASL2Unsupported('ASL2_OUTPUT_ROLE_UNSUPPORTED')
            planned.append((family, item, output.output_field_id))
        # ASL/SQL emits dimensions first. No custom projection order may be lost.
        ordered = [p for family in ('dimensions','metrics') for p in planned if p[0] == family]
        projection = payload.projection_spec.items
        if projection:
            _require(all(p.alias_policy == 'STABLE_ID' for p in projection), 'ASL2_GOVERNED_ALIAS_POLICY_UNPROVEN')
            _require([p[2] for p in ordered] == [p.output_field_id for p in projection],
                     'ASL2_PROJECTION_ORDER_UNREPRESENTABLE')
        bindings = tuple(m.OutputBindingProof(output_field_id=field_id,
            asl_projection_id=f'{family}:{item["name"]}', sql_alias=item['alias'],
            result_column_index=index, result_column_name=item['alias'], status='UNKNOWN',
            semantic_fingerprint=self.plan.semantic_fingerprint)
            for index,(family,item,field_id) in enumerate(ordered))
        limit = getattr(payload, 'limit', None)
        # No silent completeness claim at SQL Translator's 10,000-row ceiling.
        cap = None
        if self.contract.expected_cardinality.kind == 'SCALAR':
            asl['limit'] = 1
        elif payload.payload_type == 'RANKING':
            asl['limit'] = payload.ranking.limit
        elif limit:
            asl['limit'] = limit.limit
        else:
            cap = asl['limit'] = 10000
        return asl, bindings, cap

    def ranking(self, asl, bindings):
        """Private SQL policy; ASL's single sort cannot encode this contract."""
        if self.plan.payload.payload_type != 'RANKING':
            return None
        rank = self.plan.payload.ranking
        _require(rank.ties_policy == 'EXCLUDE_TIES', 'ASL2_RANK_TIES_CONTRACT_UNSUPPORTED')
        # An extra grouping field would change the population being ranked.
        group_ids = {r.canonical_id for r in self.plan.payload.group_by}
        _require(self.plan.payload.ranking_target.canonical_id in group_ids,
                 'ASL2_RANK_TARGET_GRAIN_UNPROVEN')
        for output in self.contract.required_outputs:
            if output.semantic_ref.catalog_type != 'METRIC':
                _require(output.semantic_ref.canonical_id in group_ids, 'ASL2_RANK_ORDER_GRAIN_UNPROVEN')
        by_id = {b.output_field_id:b.sql_alias for b in bindings}
        orders = [dict(sql_alias=by_id[o.output_field_id], direction=o.direction, nulls_policy=o.nulls_policy)
                  for o in self.contract.required_ordering]
        primary = next((family, item) for family in ('metrics', 'dimensions') for item in asl[family]
                       if item['alias'] == orders[0]['sql_alias'])
        asl['sort'] = dict(field=primary[1]['name'], field_type='metric' if primary[0] == 'metrics' else 'dimension',
                           direction=rank.direction)
        return dict(contract='pinned-ranking-v1', semantic_fingerprint=self.plan.semantic_fingerprint,
                    output_aliases=[b.sql_alias for b in bindings], order_by=orders,
                    ties_policy=rank.ties_policy, limit=rank.limit)


def lower_asl2(session, plan, *, time_storage=None, time_evidence_digest=None, allow_test_time_storage=False):
    """Use the same live scoped session before catalog acceptance, never history."""
    session._check()
    _require(isinstance(plan, AuthorizedLogicalPlan), 'ASL2_CURRENT_AUTHORIZED_PLAN_REQUIRED')
    plan = AuthorizedLogicalPlan.model_validate_json(plan.model_dump_json())
    _require(plan.permission_requirement == session.context, 'ASL2_CURRENT_SCOPE_PIN_MISMATCH')
    from .pipeline import collect_bound_refs
    session._require_refs(collect_bound_refs(plan.payload))
    from .catalog_plans import validate_catalog_payload
    validate_catalog_payload(session, plan.payload)
    contract = ResultContractCompiler.compile(plan)
    blockers = []
    kind = plan.payload.payload_type
    if kind not in {'SCALAR_AGGREGATE','GROUPED_AGGREGATE','DETAIL_ROWS','RANKING'}:
        blockers.append('ASL2_PAYLOAD_' + kind + '_UNSUPPORTED')
    time = getattr(plan.payload, 'time', None)
    if time and time.range is not None and time_storage is None:
        blockers.append('ASL2_TIME_STORAGE_TIMEZONE_UNPROVEN')
    if time and (time.grain != 'NONE' or time.comparison is not None
                 or time.missing_period_policy != 'LEAVE_MISSING' or time.include_incomplete_period):
        blockers.append('ASL2_TIME_POLICY_UNREPRESENTABLE')
    if contract.required_ordering and kind != 'RANKING':
        blockers.append('ASL2_ORDERING_POLICY_UNREPRESENTABLE')
    if blockers:
        return ASL2Lowering('UNSUPPORTED',plan.semantic_fingerprint,None,contract,blockers=tuple(blockers))
    try:
        compiler = _Compiler(session,plan,contract)
        asl, bindings, cap = compiler.build()
        time_receipt = None
        if time and time.range is not None:
            from .time_storage import bounded_time_predicates
            predicates, time_receipt = bounded_time_predicates(session, plan, compiler.field(time.anchor),
                time_storage, time_evidence_digest, allow_test_only=allow_test_time_storage)
            previous = compiler.filter_tree(plan.payload.filters)
            expression = dict(operator='AND', children=[previous, predicates]) if previous else predicates
            compiler.filter_contract = dict(contract='pinned-filter-tree-v1',
                semantic_fingerprint=plan.semantic_fingerprint, expression=expression)
            asl['filters'] = []
        ordering = compiler.ranking(asl, bindings)
        labels = {o.output_field_id:o.display_label for o in contract.required_outputs}
        labels.update({p.output_field_id:p.display_label for p in plan.payload.projection_spec.items if p.display_label is not None})
        return ASL2Lowering('SUPPORTED_PLAN_ONLY',plan.semantic_fingerprint,asl,contract,bindings,
            implicit_row_cap=cap,display_labels=tuple(labels.items()),ordering_contract=ordering,
            filter_contract=compiler.filter_contract, time_storage_receipt=time_receipt,
            compilation_fingerprint=contract_digest(dict(plan=plan.model_dump(mode='json'),
                time_evidence_digest=time_evidence_digest if time_receipt else None)))
    except ASL2Unsupported as exc:
        return ASL2Lowering('UNSUPPORTED',plan.semantic_fingerprint,None,contract,blockers=(str(exc),))


def bind_result_columns(lowering, columns, *, truncated, row_count):
    """Bind actual returned column metadata; this does not prove source results.

    The existing ResultContract prover remains responsible for row/cardinality,
    order, types and numeric checks. A reached implicit cap is not completeness.
    """
    _require(lowering.status == 'SUPPORTED_PLAN_ONLY', 'ASL2_RESULT_PLAN_REQUIRED')
    _require(columns == [b.sql_alias for b in lowering.output_bindings], 'ASL2_RESULT_COLUMNS_MISMATCH')
    _require(truncated is False and type(row_count) is int and row_count >= 0,
             'ASL2_RESULT_COMPLETENESS_UNPROVEN')
    _require(lowering.implicit_row_cap is None or row_count < lowering.implicit_row_cap,
             'ASL2_RESULT_IMPLICIT_LIMIT_REACHED')
    return tuple(b.model_copy(update={'status':'PASS'}) for b in lowering.output_bindings)


def prove_asl2_result(lowering, *, columns, rows, truncated, snapshot_id=None):
    """Structural proof for a trusted result, never a SQL execution receipt."""
    from .result_contract import prove_result_contract
    bindings = bind_result_columns(lowering, columns, truncated=truncated, row_count=len(rows))
    return prove_result_contract(lowering.result_contract, columns=columns, rows=rows,
        truncated=truncated, output_bindings=bindings, snapshot_id=snapshot_id)
