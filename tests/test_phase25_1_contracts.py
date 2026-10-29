"""Executable closure invariants with independent expected values and adversarial inputs."""
from __future__ import annotations

import itertools
import json
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from app.semantic_v2.models import *
from app.semantic_v2.enums import *
from app.semantic_v2.pipeline import *
from app.semantic_v2.registries import PayloadContractRegistry, SlotDefinitionRegistry
from app.semantic_v2.result_contract import ResultContractCompiler, completed_allowed, prove_result_contract
from app.semantic_v2.slot_reducer import TaskPatch, apply_task_patch, semantic_fingerprint
from app.semantic_v2.state_machine import *
from app.semantic_v2.migration import SchemaMigrationRegistry
from tools.phase25_1.fixtures import NOW, ref, time_spec, logical, conversation, operation, candidate_resolution, permission, snapshot, versions, authorizations


def dump_ref(code='sales'):
    return ref(code).model_dump(mode='json')


@pytest.mark.parametrize('kind', ['TIME_SERIES', 'COMPARISON', 'FORECAST', 'ANOMALY', 'ROOT_CAUSE'])
def test_metric_analyses_do_not_require_group_by(kind):
    data = dict(payload_type=kind, measures=[ref()], time=time_spec())
    if kind == 'COMPARISON':
        data['comparison'] = ComparisonSpec(comparison_type='YOY', baseline=TimeBaseline(relative_period='PREVIOUS_YEAR'), calculation='GROWTH_RATE', output_metrics=[ref()])
    if kind == 'FORECAST':
        data.update(horizon_periods=3, horizon_grain='MONTH', algorithm=AlgorithmRef(algorithm_id='deterministic_forecast_selector', algorithm_version='forecast-selector-v1', policy_source='SYSTEM_POLICY'))
    if kind == 'ANOMALY':
        data['algorithm'] = AlgorithmRef(algorithm_id='robust_anomaly', algorithm_version='analysis-contract-v2', policy_source='SYSTEM_POLICY')
    if kind == 'ROOT_CAUSE':
        data['decomposition_dimensions'] = [ref('region', SemanticRole.GROUP_BY, CatalogType.DIMENSION)]
    payload = TypeAdapter(PlanPayload).validate_python(data)
    assert payload.group_by == []
    assert logical(payload).query_shape == PayloadContractRegistry.resolve_query_shape(kind)


@pytest.mark.parametrize('kind', ['GROUPED_AGGREGATE', 'RANKING'])
def test_grouped_payload_still_requires_nonempty_grouping(kind):
    with pytest.raises(ValidationError):
        TypeAdapter(PlanPayload).validate_python(dict(payload_type=kind, measures=[ref()], group_by=[]))


@pytest.mark.parametrize('payload_type', list(PayloadContractRegistry.definitions))
def test_registry_rejects_incompatible_route_and_goals(payload_type):
    definition = PayloadContractRegistry.get(payload_type)
    payload = type('Payload', (), {'payload_type': payload_type})()
    bad_route = next(r for r in ServiceRoute if r not in definition.allowed_service_routes)
    with pytest.raises(ValueError, match='PLAN_VALIDATION_FAILURE'):
        PayloadContractRegistry.validate(payload, bad_route, definition.required_analysis_goals)
    bad_goal = next(g for g in AnalysisGoal if g not in definition.allowed_analysis_goals)
    with pytest.raises(ValueError, match='PLAN_VALIDATION_FAILURE'):
        PayloadContractRegistry.validate(payload, definition.allowed_service_routes[0], [bad_goal])


def test_query_shape_is_derived_unserialized_and_plan_is_deeply_immutable():
    original = TimeSeriesPayload(measures=[ref()], time=time_spec())
    plan = logical(original)
    assert 'query_shape' not in plan.model_dump() and 'query_shape' not in LogicalPlan.model_json_schema()['properties']
    assert plan.query_shape == QueryShape.TIME_SERIES
    for mutate in (lambda: plan.payload.measures.append(ref('volume')), lambda: setattr(plan.payload.time, 'grain', TimeGrain.YEAR), lambda: setattr(plan, 'task_id', 'other'), lambda: delattr(plan.payload, 'time')):
        with pytest.raises((TypeError, ValidationError)):
            mutate()
    original.measures.append(ref('volume'))
    assert len(plan.payload.measures) == 1
    assert LogicalPlan.model_validate_json(plan.model_dump_json()) == plan


def test_llm_schemas_have_no_authoritative_plan_fields():
    for cls in (CurrentTurnSemanticParse, CandidateSelectionDecision):
        schema = json.dumps(cls.model_json_schema())
        for field in ('canonical_id', 'result_contract', 'execution_backend', 'task_version', 'state_version'):
            assert f'"{field}"' not in schema
    with pytest.raises(ValidationError):
        CurrentTurnSemanticParse(canonical_id='invented')


def test_compiler_requires_selected_catalog_candidates_and_deterministic_outputs():
    payload = TimeSeriesPayload(measures=[ref()], time=time_spec())
    parsed = CurrentTurnParser.parse(text='销售趋势', turn_id='turn', text_ref='message', parsed=CurrentTurnSemanticParse())
    resolution = TurnResolutionResult(dialogue_act='NEW_TASK', target_topic_id='topic', target_task_id='task',
        referential_completeness=TurnReferentialCompleteness(relation='SELF_CONTAINED', depends_on_history=False),
        task_patch=TaskPatch(base_task_version=0), semantic_resolution=candidate_resolution(payload), decision=ProceedDecision())
    args = dict(parse=parsed, resolution=resolution, payload=payload, service_route='DATA_QUERY', analysis_goals=['TREND'],
                task_version=1, permission=permission(), snapshot=snapshot(), authorizations=authorizations(payload), version_metadata=versions())
    plan = LogicalPlanCompiler.compile(**args)
    assert ResultContractCompiler.compile(plan).semantic_fingerprint == plan.semantic_fingerprint
    executable = compile_executable_plan(plan)
    assert executable.execution_backend == ExecutionBackend.SEMANTIC_QUERY
    assert executable.adapter_report.can_execute_safely is False
    bad = payload.model_copy(update={'measures': [ref('invented')]})
    with pytest.raises(ValueError, match='not selected'):
        LogicalPlanCompiler.compile(**dict(args, payload=bad))


@pytest.mark.parametrize('field,value', [('catalog_version', 'different'), ('semantic_model_id', 'different'), ('business_domain_ids', ['outside'])])
def test_recursive_snapshot_validation_covers_filter_nested_refs(field, value):
    bad = ref('filter', SemanticRole.FILTER_FIELD, CatalogType.ATTRIBUTE).model_copy(update={field: value})
    payload = ScalarAggregatePayload(measures=[ref()], filters=Predicate(field_ref=bad, operator='EQ', value=StringValue(value='x'), source='USER_EXPLICIT', scope='TASK'))
    with pytest.raises(ValueError, match='snapshot/scope mismatch'):
        logical(payload)


@pytest.mark.parametrize('location', ['measure', 'filter', 'time', 'ranking', 'projection', 'relationship', 'comparison'])
def test_permission_validator_walks_every_nested_reference(location):
    r = ref('private')
    objects = {
        'measure': [r],
        'filter': Predicate(field_ref=r, operator='EQ', value=NumberValue(value='2'), source='USER_EXPLICIT', scope='TASK'),
        'time': time_spec(),
        'ranking': RankingSpec(rank_by=r, direction='DESC', limit=5),
        'projection': ProjectionSpec(items=[ProjectionItem(output_field_id='private', ref=r, role='PROJECTION_FIELD', position=0)]),
        'relationship': RelationshipSpec(relation_ref=r, source_ref=ref(), target_ref=ref('target'), cardinality='ONE_TO_MANY'),
        'comparison': ComparisonSpec(comparison_type='RATIO', calculation='RATIO', baseline=SemanticBaseline(ref=r), output_metrics=[ref()]),
    }
    with pytest.raises(ValueError, match='PERMISSION_DENIED'):
        validate_bound_ref_scope_and_permission(objects[location], snapshot(), permission(), ())


def test_candidate_invariants_and_scoring_cannot_be_forged():
    contract = candidate_resolution(ScalarAggregatePayload(measures=[ref()]))
    data = contract.model_dump()
    data['candidate_sets'][0]['candidates'][0]['permission_allowed'] = False
    with pytest.raises(ValidationError):
        SemanticResolutionContract.model_validate(data)
    data = contract.model_dump()
    data['plan_candidates'][0]['semantic_candidate_ids'] = ['missing']
    with pytest.raises(ValidationError):
        SemanticResolutionContract.model_validate(data)
    score = PlanCandidateScore(retrieval_score=1, constraint_score=0, permission_score=1, executability_score=0)
    assert score.total_score == .5
    with pytest.raises(ValidationError):
        PlanCandidateScore(**score.model_dump(), total_score=.99)


@pytest.mark.parametrize('action', ['NEW_TASK', 'ADD', 'REPLACE', 'DRILL_DOWN', 'CHAT'])
def test_control_actions_are_closed(action):
    with pytest.raises(ValidationError):
        ControlPayload(control_action=action)


@pytest.mark.parametrize('kind', ['FILTER', 'SORT', 'LIMIT', 'PROJECT', 'DRILL_DOWN'])
def test_dataset_operation_rejects_missing_required_value_and_unrelated_fields(kind):
    with pytest.raises(ValidationError):
        DatasetTransformPayload(source_dataset_id='d', operation={'operation_type': kind})
    with pytest.raises(ValidationError):
        DatasetTransformPayload(source_dataset_id='d', operation={'operation_type': kind, 'unknown': True})


@pytest.mark.parametrize('operator,value,valid', [
    ('IN', ListValue(values=[StringValue(value='a')]), True), ('IN', StringValue(value='a'), False),
    ('BETWEEN', RangeValue(start=NumberValue(value='1'), end=NumberValue(value='2')), True),
    ('BETWEEN', ListValue(values=[NumberValue(value='1')]), False),
    ('LIKE', StringValue(value='x%'), True), ('LIKE', NumberValue(value='1'), False),
    ('IS_NULL', NullValue(), True), ('IS_NULL', StringValue(value=''), False),
    ('GT', NumberValue(value='0.1'), True), ('GT', BooleanValue(value=True), False),
    ('AND', StringValue(value='a'), False), ('OR', StringValue(value='a'), False), ('NOT', NullValue(), False),
    ('EQ', NullValue(), False), ('NOT_IN', ListValue(values=[NumberValue(value='2')]), True),
])
def test_filter_operator_value_contract(operator, value, valid):
    args = dict(field_ref=ref(), operator=operator, value=value, source='USER_EXPLICIT', scope='TASK')
    if valid:
        assert Predicate(**args).operator.value == operator
    else:
        with pytest.raises(ValidationError):
            Predicate(**args)


def test_decimal_temporal_and_range_contracts():
    assert NumberValue(value='0.1').value + NumberValue(value='0.2').value == Decimal('0.3')
    assert NumberValue.model_validate_json(NumberValue(value='0.1').model_dump_json()).value == Decimal('0.1')
    for build in (lambda: DateTimeValue(value='2026-01-01T00:00:00'),
                  lambda: ListValue(values=[NullValue(), StringValue(value='a')]),
                  lambda: RangeValue(start=NumberValue(value='2'), end=NumberValue(value='1')),
                  lambda: RangeValue(start=NumberValue(value='2'), end=StringValue(value='3'))):
        with pytest.raises(ValidationError):
            build()
    for update in ({'timezone': 'Invalid/Zone'}, {'calendar': 'FISCAL'}, {'source': 'SYSTEM_DEFAULT'}):
        with pytest.raises(ValidationError):
            TimeSpec.model_validate(dict(time_spec().model_dump(), **update))


@pytest.mark.parametrize('permutation', list(itertools.permutations(range(4))))
def test_atomic_patch_permutations_have_same_fingerprint(permutation):
    ops = [operation('CLEAR', operation_id='clear'), operation('INHERIT', value=[dump_ref('historic')], operation_id='inherit'),
           operation('ADD', value=dump_ref('volume'), operation_id='add-v'), operation('ADD', value=dump_ref('sales'), operation_id='add-s')]
    patch = TaskPatch.compile([ops[i] for i in permutation], base_task_version=1)
    result = apply_task_patch(TaskSemanticState(metrics=[ref('obsolete')]), patch)
    expected = TaskSemanticState(metrics=[ref('sales'), ref('volume')])
    assert result.semantic_fingerprint == semantic_fingerprint(expected)
    assert {r.canonical_id for r in result.semantics.metrics} == {'fixture:sales', 'fixture:volume'}


def test_reducer_noop_add_duplicate_replace_clear_barrier_and_conflicts():
    state = TaskSemanticState(metrics=[ref()])
    add = TaskPatch.compile([operation('ADD', value=dump_ref())], base_task_version=1)
    assert apply_task_patch(state, add).changed is False
    replace = TaskPatch.compile([operation('REPLACE', value=[dump_ref('volume')])], base_task_version=1)
    assert [r.canonical_id for r in apply_task_patch(state, replace).semantics.metrics] == ['fixture:volume']
    clear = TaskPatch.compile([operation('CLEAR')], base_task_version=1)
    cleared = apply_task_patch(state, clear)
    inherit = TaskPatch.compile([operation('INHERIT', value=[dump_ref()])], base_task_version=1)
    assert apply_task_patch(cleared.semantics, inherit, clear_barriers=cleared.clear_barriers).semantics.metrics == []
    with pytest.raises(ValueError):
        TaskPatch.compile([operation('SET', value=[dump_ref()]), operation('REPLACE', value=[dump_ref('volume')])], base_task_version=1)
    with pytest.raises(ValueError):
        TaskPatch.compile([operation('SET', slot='made_up_slot', value=[])], base_task_version=1)


def test_mutation_is_atomic_idempotent_and_noop_does_not_version():
    state = conversation()
    mutation = StateMutation(mutation_id='m', message_id='message', turn_id='turn', task_id='task:a', expected_state_version=0,
        base_task_version=1, task_patch=TaskPatch.compile([operation('ADD', value=dump_ref('volume'))], base_task_version=1), created_at=NOW)
    next_state = apply_state_mutation(state, mutation)
    assert next_state.tasks['task:a'].active_version == 2 and state.tasks['task:a'].active_version == 1
    assert apply_state_mutation(next_state, mutation) == next_state
    with pytest.raises(ValueError):
        apply_state_mutation(next_state, mutation.model_copy(update={'turn_id': 'different'}))
    noop = StateMutation(mutation_id='n', message_id='n', turn_id='n', task_id='task:a', expected_state_version=1,
        base_task_version=2, task_patch=TaskPatch(base_task_version=2), created_at=NOW)
    assert apply_state_mutation(next_state, noop).tasks['task:a'].active_version == 2


@pytest.mark.parametrize('change', ['active_topic', 'topic_stack', 'topic_task', 'active_task', 'task_topic', 'active_version', 'duplicate_version', 'last_executed', 'dataset', 'pending'])
def test_conversation_rejects_orphan_references(change):
    data = conversation().model_dump()
    if change == 'active_topic': data['active_topic_id'] = 'missing'
    if change == 'topic_stack': data['topic_stack'].append('missing')
    if change == 'topic_task': data['topics']['topic:a']['task_ids'].append('missing')
    if change == 'active_task': data['topics']['topic:a']['active_task_id'] = 'task:b'
    if change == 'task_topic': data['tasks']['task:a']['topic_id'] = 'missing'
    if change == 'active_version': data['tasks']['task:a']['active_version'] = 100
    if change == 'duplicate_version': data['tasks']['task:a']['versions'] *= 2
    if change == 'last_executed': data['tasks']['task:a']['last_executed_version'] = 100
    if change == 'dataset': data['datasets']['orphan'] = {'dataset_id': 'orphan', 'task_id': 'missing', 'task_version': 1}
    if change == 'pending': data['pending_records']['orphan'] = {'pending_id': 'orphan', 'task_id': 'missing', 'task_version': 1, 'slot_path': 'metrics', 'question': 'choose', 'asked_at': NOW}
    with pytest.raises(ValidationError):
        ConversationState.model_validate(data)


def test_readiness_cannot_change_turn_relation_and_defaults_do_not_remove_dependency():
    parsed = CurrentTurnParser.parse(text='那它呢', turn_id='turn', text_ref='message', parsed=CurrentTurnSemanticParse(reference_signals=['PRONOUN']))
    result = TurnResolver.resolve(parsed, state=conversation(), task_patch=TaskPatch(base_task_version=1), semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))
    assert result.referential_completeness.depends_on_history and result.target_task_id == 'task:a'
    blocker = ReadinessBlocker(blocker_id='missing', plan_path='metrics', blocker_type='SURFACE_PARSE_FAILURE', source_stage='PARSE', message_code='MISSING_PARSE')
    assert ExecutionReadiness.evaluate([blocker]).decision_type == 'SYSTEM_REPAIR'
    assert result.referential_completeness.relation == 'CURRENT_TASK'


@pytest.mark.parametrize('reason', [r for r in ErrorType if r != ErrorType.USER_AMBIGUITY])
def test_system_errors_cannot_create_business_pending(reason):
    with pytest.raises(ValidationError):
        ReadinessBlocker(blocker_id='b', plan_path='metrics', blocker_type=reason, source_stage='PLAN', user_action_required=True, message_code='x')
    with pytest.raises(ValidationError):
        ClarificationDecision(reason_type=reason, blocking=True, base_task_version=1, state_version=1)


def test_report_delivery_retains_root_cause_and_help_is_not_chat():
    payload = RootCausePayload(measures=[ref()], time=time_spec(), decomposition_dimensions=[ref('region', SemanticRole.GROUP_BY, CatalogType.DIMENSION)])
    plan = logical(payload, delivery=DeliverySpec(modes=['REPORT']))
    assert plan.payload.payload_type == 'ROOT_CAUSE' and plan.analysis_goals == [AnalysisGoal.ROOT_CAUSE]
    assert logical(CapabilityHelpPayload()).query_shape == QueryShape.CAPABILITY_HELP
    with pytest.raises(ValidationError): ReportPayload(source_task_ids=[])


@pytest.mark.parametrize('kind,catalog', [('METRIC','METRIC'), ('FIELD','ATTRIBUTE'), ('COLUMN','PHYSICAL_COLUMN'), ('TABLE','PHYSICAL_TABLE'), ('ENTITY','ENTITY'), ('DATASET','DATASET'), ('REPORT','REPORT')])
def test_lineage_accepts_typed_nonmetric_targets(kind, catalog):
    payload = LineagePayload(lineage_target={'target_type':kind, 'ref': ref('target', SemanticRole.SOURCE_ENTITY, CatalogType(catalog))})
    assert logical(payload).query_shape == QueryShape.LINEAGE_GRAPH


def test_proofs_require_logical_output_identity_and_advisory_unknown_is_warning_only():
    payload = RankingPayload(measures=[ref()], group_by=[ref('hospital', SemanticRole.GROUP_BY, CatalogType.ENTITY)],
        ranking_target=ref('hospital', SemanticRole.GROUP_BY, CatalogType.ENTITY), ranking=RankingSpec(rank_by=ref(), direction='DESC', limit=5))
    contract = ResultContractCompiler.compile(logical(payload))
    assert contract.row_bounds.maximum == 5 and contract.required_ordering[0].direction == 'DESC'
    proof = prove_result_contract(contract, columns=['sales'], rows=[{'sales': 3}], truncated=False)
    assert not completed_allowed(proof)
    passed = ContractProof(status='PASS', checks=[ProofCheck(check_id='advisory', status='UNKNOWN', severity='ADVISORY')])
    assert completed_allowed(passed)
    with pytest.raises(ValidationError): ContractProof(status='PASS', checks=[ProofCheck(check_id='blocking', status='UNKNOWN')])


def test_adapter_cannot_claim_unimplemented_compensation():
    from app.semantic_v2.legacy_adapter import assess_legacy_adapter
    plan = logical(DatasetTransformPayload(source_dataset_id='d', operation=DatasetLimitOperation(limit=5)))
    assert not assess_legacy_adapter(plan).can_execute_safely
    with pytest.raises(ValidationError):
        AdapterReport(adapter_status='LOSSY_COMPENSATED', can_execute_safely=True)


def test_dataset_truncation_limits_global_operations():
    base = dict(source_dataset_id='A1', root_dataset_id='A0', parent_dataset_ids=['A0'], source_row_count=5,
                source_total_row_count=22, source_truncated=True, source_completeness='PARTIAL',
                source_ordering_proof=ProofCheck(check_id='order', status='PASS'), snapshot_id='snapshot', data_as_of=NOW)
    assert DatasetTransformSafetyContract(**base, requested_operation='LIMIT').safe_to_execute
    assert not DatasetTransformSafetyContract(**base, requested_operation='GLOBAL_TOP_N').safe_to_execute
    assert not DatasetTransformSafetyContract(**base, requested_operation='GLOBAL_AGGREGATE').safe_to_execute


def test_schema_migration_keeps_history_readable_without_inventing_algorithms():
    rows = [json.loads(line) for line in Path('docs/phase25/typed_logical_plan_v0_2_examples.jsonl').read_text(encoding='utf-8').splitlines()]
    chat = rows[0]['plan']
    result = SchemaMigrationRegistry.migrate(chat)
    assert result.status == 'LOSSLESS' and result.source_fixture == chat
    parsed = PlanEnvelope.model_validate(result.migrated)
    assert PlanEnvelope.model_validate_json(parsed.model_dump_json()) == parsed
    unsupported = SchemaMigrationRegistry.migrate({'schema_version':'0.2', 'payload':{'payload_type':'FORECAST','horizon':3}})
    assert unsupported.status == 'UNSUPPORTED' and unsupported.source_fixture['payload']['horizon'] == 3
    for cls in (LogicalPlan, ExecutablePlan, CurrentTurnParseResult, TurnResolutionResult, TaskSemanticState, ConversationState):
        Draft202012Validator.check_schema(cls.model_json_schema())


def test_core_semantic_state_has_no_any_or_untyped_slot_map():
    schema = TaskSemanticState.model_json_schema()
    assert 'slots' not in schema['properties']
    assert set(SlotDefinitionRegistry.definitions) == set(TaskSemanticState.model_fields)
    assert all(str(f.annotation) != 'typing.Any' for f in TaskSemanticState.model_fields.values())


def test_explicit_operations_override_system_default_in_any_input_order():
    explicit = operation('SET', value=[dump_ref('explicit')], operation_id='explicit')
    default = operation('SET', value=[dump_ref('default')], operation_id='default').model_copy(update={'source':'SYSTEM_DEFAULT'})
    for ops in ([default, explicit], [explicit, default]):
        patch = TaskPatch.compile(ops, base_task_version=1)
        assert apply_task_patch(TaskSemanticState(), patch).semantics.metrics[0].canonical_code == 'explicit'


def test_clarification_does_not_expose_unauthorized_option_labels():
    from app.semantic_v2.clarification import authorized_clarification_options
    visible = ClarificationOption(option_id='visible', display_label='public', canonical_ref=ref(), evidence=['fixture'])
    hidden = ClarificationOption(option_id='hidden', display_label='private metric', canonical_ref=ref('private'), evidence=['fixture'])
    assert authorized_clarification_options([visible, hidden], snapshot=snapshot(), permission=permission(),
                                            authorizations=authorizations(ScalarAggregatePayload(measures=[ref()]))) == [visible]


def test_compatibility_envelope_cannot_bypass_adapter_implementation_gate():
    from app.semantic_v2.legacy_adapter import assess_legacy_adapter
    from tests.test_semantic_v2_contracts import plan
    envelope = plan(payload=ScalarAggregatePayload(measures=[ref()]))
    assert assess_legacy_adapter(envelope).adapter_status == AdapterStatus.UNSUPPORTED
    assert assess_legacy_adapter(envelope).can_execute_safely is False


def test_single_row_ordering_still_enforces_excluded_nulls():
    contract = ResultContract(semantic_fingerprint='f', required_outputs=[OutputFieldRequirement(output_field_id='metric', semantic_ref=ref(), logical_role='MEASURE')],
        required_ordering=[OrderingRequirement(output_field_id='metric', direction='DESC', nulls_policy='EXCLUDE')])
    binding = OutputBindingProof(output_field_id='metric', asl_projection_id='p', sql_alias='x', result_column_index=0,
                                 result_column_name='x', status='PASS', semantic_fingerprint='f')
    proof = prove_result_contract(contract, columns=['x'], rows=[{'x':None}], truncated=False, output_bindings=[binding])
    assert proof.status == ProofStatus.FAIL
    assert next(c for c in proof.checks if c.check_id == 'required_ordering').status == ProofStatus.FAIL


def test_execution_success_cannot_be_asserted_with_empty_proof_checks():
    proof = ContractProof(status='PASS')
    with pytest.raises(ValidationError, match='explicit checks'):
        ExecutionAttemptRecord(execution_id='x', task_id='t', task_version=1, attempt_number=1,
            status='SUCCEEDED', started_at=NOW, completed_at=NOW, execution_backend='SEMANTIC_QUERY',
            snapshot_id='s', catalog_version='c', vector_index_version='v', semantic_model_version='m', policy_version='p',
            proof_chain=ProofChain(plan=proof, asl=proof, sql_plan=proof, result=proof))
