"""Plan-only integration with real catalog publication code and offline stores."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError

from app.domain.models import ChatRequest, TrustedIdentity
from app.semantic_v2.authorized_contract import AuthorizedVersionMetadata, ScopedArtifact, contract_digest
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.enums import CatalogType, SemanticRole
from app.semantic_v2.models import (ChatPayload, DatasetTransformPayload, ScalarAggregatePayload,
    GroupedAggregatePayload, ProceedDecision, SemanticResolutionContract)
from app.semantic_v2.pipeline import CurrentTurnSemanticParse, LogicalPlan, AuthorizedLogicalPlan
from app.semantic_v2.slot_reducer import TaskPatch
from tools.phase25_1.fixtures import candidate_resolution, logical, ref as old_ref

PROJECT = Path(__file__).resolve().parents[1]
OAGNET = PROJECT/'Oagnet' if (PROJECT/'Oagnet').exists() else PROJECT.parent/'Oagnet'
sys.path.extend([str(OAGNET), str(OAGNET/'tests')])
from test_catalog_publication import authority, publish, reseal, system


def request(domains=(205,), **updates):
    values=dict(conversation_id='conversation',message_id='message',question='Sales',application_id='app',
        semantic_model_id=81,business_domain_ids=list(domains))
    values.update(updates)
    return ChatRequest(**values)


IDENTITY = TrustedIdentity(tenant_id='tenant',user_id='user',roles=['irrelevant-role'])


@pytest.fixture
def provider():
    service,store,registry,redis,_,overrides=system()
    for model,domains in [(81,[205]),(81,[]),(81,[206]),(82,[205])]:
        publish(service, model=model, domains=domains)
    return service,store,registry,redis,overrides


def session(provider, req=None, identity=IDENTITY):
    return ScopedPlanSession(req or request(),identity,provider[0])


def measure(current):
    c=current.candidates(CatalogType.METRIC)[0]
    return current.bind(c['candidate_id'],SemanticRole.MEASURE)


def compile_plan(current, payload=None):
    from app.semantic_v2.registries import PayloadContractRegistry
    payload=payload or ScalarAggregatePayload(measures=[measure(current)])
    route=PayloadContractRegistry.get(payload.payload_type).allowed_service_routes[0]
    semantic=candidate_resolution(payload) if hasattr(payload,'measures') else SemanticResolutionContract(status='UNRESOLVED')
    resolution=current.resolve_turn(parsed=CurrentTurnSemanticParse(),task_patch=TaskPatch(base_task_version=0),semantic_resolution=semantic)
    return current.compile(parsed=CurrentTurnSemanticParse(),resolution=resolution,payload=payload,
        service_route=route,analysis_goals=sorted(PayloadContractRegistry.get(payload.payload_type).required_analysis_goals),task_version=1,
        versions=AuthorizedVersionMetadata(prompt_version='fixture',policy_version='fixture',adapter_version='fixture'))


@pytest.mark.parametrize('domains',[[],[205]])
def test_current_scope_compiles_without_database_or_invented_acl(provider,domains):
    current=session(provider,request(domains))
    result=compile_plan(current)
    plan=result.logical_plan
    assert plan.schema_version=='0.2.2'
    assert result.backend_contract.mode=='SHADOW_ONLY'
    assert plan.permission_requirement.authorized_scope==request(domains).authorized_semantic_scope
    assert plan.snapshot_requirement.database_id is None
    assert plan.snapshot_requirement.business_domain_ids==[str(d) for d in domains]
    assert 'row_scope_hash' not in plan.permission_requirement.model_dump()
    assert AuthorizedLogicalPlan.model_validate_json(plan.model_dump_json())==plan
    with pytest.raises((TypeError,ValidationError)):
        plan.payload.measures.clear()


def test_model_owned_global_dimension_does_not_invent_shared_domain_grant(provider):
    current=session(provider,request([]))
    metric=measure(current)
    candidates=current.candidates(CatalogType.DIMENSION)
    global_candidate=next(c for c in candidates if ':dim:' in current._rows[c['candidate_id']].metadata['catalog_logical_id']
                          and current._rows[c['candidate_id']].metadata['business_domain_id']==-1)
    dimension=current.bind(global_candidate['candidate_id'],SemanticRole.GROUP_BY)
    assert dimension.business_domain_ids==()
    result=compile_plan(current,GroupedAggregatePayload(measures=[metric],group_by=[dimension]))
    assert result.logical_plan.snapshot_requirement.business_domain_ids==[]


def test_legacy_permission_branch_stays_closed_to_empty_owned_domain():
    value=old_ref().model_copy(update={'business_domain_ids':()})
    with pytest.raises(ValueError,match='snapshot/scope mismatch'):
        logical(ScalarAggregatePayload(measures=[value]))


@pytest.mark.parametrize('mutation',['canonical_id','canonical_code','display_name','semantic_model_id','catalog_version','business_domain_ids'])
def test_payload_cannot_forge_or_relabel_a_pinned_reference(provider,mutation):
    current=session(provider);bound=measure(current)
    wrong=bound.model_copy(update={mutation:('206',) if mutation=='business_domain_ids' else 'invented'})
    with pytest.raises(ValueError,match='PINNED_CATALOG_BINDING_REQUIRED'):
        compile_plan(current,ScalarAggregatePayload(measures=[wrong]))


def test_attribute_is_not_silently_promoted_to_metric(provider):
    current=session(provider)
    candidate=current.candidates(CatalogType.ATTRIBUTE)[0]
    with pytest.raises(ValueError,match='incompatible catalog role'):
        current.bind(candidate['candidate_id'],SemanticRole.MEASURE)
    with pytest.raises(ValueError,match='unknown pinned candidate'):
        current.bind('model-invented-handle',SemanticRole.MEASURE)


@pytest.mark.parametrize('fault',['authority','record','activation','finish_receipt'])
def test_full_acceptance_failure_prevents_plan_and_state_output(provider,fault,monkeypatch):
    current=session(provider);bound=measure(current)
    if fault=='authority':
        changed=authority(formula='SUM(other)');provider[4][(81,(205,))]=changed
    if fault=='record':
        next(iter(provider[1].records.values())).text='tampered'
    if fault=='activation':
        scope={'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'}
        key=provider[2]._key(scope,'active')
        provider[3].set(key,None)
    if fault=='finish_receipt':
        finish=current._pin.finish
        monkeypatch.setattr(current._pin,'finish',lambda:dict(finish(),catalog_version='wrong'))
    with pytest.raises(ValueError):
        compile_plan(current,ScalarAggregatePayload(measures=[bound]))
    with pytest.raises(ValueError,match='ACCEPTANCE_REQUIRED'):
        current.seal(kind='LAST_REQUEST',payload={})


KINDS=['PENDING','TASK_FRAME','LAST_REQUEST','DAG_RESUME','RESPONSE_CACHE','DATASET','RESULT_ARTIFACT','SEMANTIC_BINDINGS','CONVERSATION']


def stored_artifact(previous, kind, payload):
    # Fixture of a trusted persisted artifact. Plan-only sessions cannot create
    # executed datasets/results; those require a future execution adapter.
    if kind=='DATASET':payload=dict(task_id='task',task_version=1,**payload)
    return ScopedArtifact(kind=kind,context=previous.context,payload=payload,payload_digest=contract_digest(payload))


@pytest.mark.parametrize('kind',KINDS)
@pytest.mark.parametrize('changed_scope',[{'semantic_model_id':82},{'business_domain_ids':[]},{'business_domain_ids':[206]},
    {'database_id':7},{'knowledge_base_names':['other']}])
def test_all_state_families_reject_current_scope_change(provider,kind,changed_scope):
    previous=session(provider);compile_plan(previous,ChatPayload())
    artifact=stored_artifact(previous,kind,{'dataset_id':'data'})
    current=session(provider,request(**changed_scope))
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        current.restore(artifact,kind=kind)


@pytest.mark.parametrize('changed',[{'conversation_id':'other'},{'application_id':'other'}])
def test_conversation_namespace_cannot_reuse_other_state_or_cache(provider,changed):
    previous=session(provider);old_key=previous.cache_key('same-semantics');compile_plan(previous,ChatPayload())
    artifact=previous.seal(kind='PENDING',payload={})
    current=session(provider,request(**changed))
    assert current.cache_key('same-semantics')!=old_key
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        current.restore(artifact,kind='PENDING')
    assert compile_plan(current,ChatPayload()).logical_plan.plan_id!=compile_plan(session(provider),ChatPayload()).logical_plan.plan_id


def test_current_message_and_roles_do_not_change_grants_or_same_scope_state(provider):
    previous=session(provider);key=previous.cache_key('same');compile_plan(previous,ChatPayload())
    artifact=previous.seal(kind='PENDING',payload={'asked':True})
    current=session(provider,request(message_id='next'),TrustedIdentity(tenant_id='tenant',user_id='user',roles=['admin']))
    assert current.context.authorized_scope==previous.context.authorized_scope
    assert current.cache_key('same')==key
    assert current.restore(artifact,kind='PENDING')=={'asked':True}


def test_model_wide_history_cannot_expand_current_explicit_request(provider):
    previous=session(provider,request([]));compile_plan(previous,ChatPayload())
    artifact=previous.seal(kind='TASK_FRAME',payload={})
    current=session(provider,request([205],history=[]))
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        current.restore(artifact,kind='TASK_FRAME')
    assert current.context.authorized_scope.business_domain_ids==(205,)


def test_multi_domain_rejected_before_provider_is_called():
    class Never:
        def pin(self,*_):pytest.fail('must not query model-wide')
    with pytest.raises(ValueError,match='EXPLICIT_MULTI_DOMAIN_NOT_SUPPORTED'):
        ScopedPlanSession(request([205,206]),IDENTITY,Never())


def test_dataset_transform_requires_matching_scope_bound_state(provider):
    payload=DatasetTransformPayload(source_dataset_id='data',operation={'operation_type':'LIMIT','limit':5})
    with pytest.raises(ValueError,match='SCOPED_DATASET_RESTORE_REQUIRED'):
        compile_plan(session(provider),payload)
    previous=session(provider);compile_plan(previous,ChatPayload())
    state=stored_artifact(previous,'DATASET',{'dataset_id':'data'})
    current=session(provider)
    current.restore(state,kind='DATASET')
    assert compile_plan(current,payload).backend_contract.mode=='SHADOW_ONLY'


def test_artifact_corruption_and_kind_substitution_are_rejected(provider):
    previous=session(provider);compile_plan(previous,ChatPayload())
    artifact=stored_artifact(previous,'DATASET',{'dataset_id':'data'})
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        session(provider).restore(artifact,kind='PENDING')
    corrupt=artifact.model_dump(mode='json');corrupt['payload']['dataset_id']='other'
    with pytest.raises(ValueError,match='SCOPED_ARTIFACT_CORRUPT'):
        ScopedArtifact.model_validate(corrupt)


@pytest.mark.parametrize('kind',['DATASET','RESULT_ARTIFACT'])
def test_plan_only_cannot_mint_executed_result_state(provider,kind):
    current=session(provider);compile_plan(current,ChatPayload())
    with pytest.raises(ValueError,match='PLAN_ONLY_CANNOT_CREATE_EXECUTED_RESULT'):
        current.seal(kind=kind,payload={'dataset_id':'invented'})


def test_dataset_invalidated_or_domain_206_cannot_be_reused_in_205(provider):
    other=session(provider,request([206]))
    artifact=stored_artifact(other,'DATASET',{'dataset_id':'data'})
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        session(provider).restore(artifact,kind='DATASET')
    current=session(provider)
    artifact=stored_artifact(current,'DATASET',{'dataset_id':'data','status':'INVALIDATED'})
    with pytest.raises(ValueError,match='SCOPED_DATASET_INVALIDATED'):
        current.restore(artifact,kind='DATASET')


def test_catalog_activation_change_invalidates_cache_and_state(provider):
    current=session(provider);key=current.cache_key('same');compile_plan(current,ChatPayload())
    state=current.seal(kind='PENDING',payload={})
    publish(provider[0], publication_id='next-published-fixture')
    replacement=session(provider)
    assert replacement.cache_key('same')!=key
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        replacement.restore(state,kind='PENDING')


@pytest.mark.parametrize('key',['tenant_id','user_id'])
def test_principal_namespace_change_cannot_reuse_cache(provider,key):
    original=session(provider)
    changed=IDENTITY.model_copy(update={key:'other'})
    assert session(provider,identity=changed).cache_key('same')!=original.cache_key('same')


@pytest.mark.parametrize('field,value',[('database_id','99'),('business_domain_ids',[]),
    ('knowledge_base_names',['invented']),('semantic_model_id','82'),('catalog_version','other'),
    ('catalog_publish_id','other'),('vector_index_version','other')])
def test_serialized_plan_scope_and_snapshot_must_agree(provider,field,value):
    plan=compile_plan(session(provider)).logical_plan.model_dump(mode='json')
    plan['snapshot_requirement'][field]=value
    with pytest.raises(ValueError,match='scope/pin mismatch'):
        AuthorizedLogicalPlan.model_validate(plan)


def test_missing_membership_or_mixed_contract_version_cannot_validate(provider):
    plan=compile_plan(session(provider)).logical_plan.model_dump(mode='json')
    with pytest.raises(ValueError,match='membership required'):
        AuthorizedLogicalPlan.model_validate(dict(plan,permission_proofs=[]))
    with pytest.raises(ValueError,match='incompatible scope contract version'):
        LogicalPlan.model_validate(dict(plan,schema_version='0.2.1'))


def test_physical_database_mapping_must_not_guess_id_equivalence(provider):
    current=session(provider,request(database_id=7))
    candidate=current.candidates(CatalogType.PHYSICAL_COLUMN)[0]
    with pytest.raises(ValueError,match='DATABASE_SCOPE_EVIDENCE_REQUIRED'):
        current.bind(candidate['candidate_id'],SemanticRole.PROJECTION_FIELD)
    # Optional database/KB remain exact context for semantic planning/state.
    assert compile_plan(current).logical_plan.snapshot_requirement.database_id=='7'


def test_restored_semantic_bindings_are_revalidated_against_current_pin(provider):
    current=session(provider);bound=measure(current);compile_plan(current,ChatPayload())
    artifact=current.seal(kind='SEMANTIC_BINDINGS',payload=[bound.model_dump(mode='json')])
    following=session(provider,request(message_id='next'))
    restored=following.restore(artifact,kind='SEMANTIC_BINDINGS')
    assert compile_plan(following,ScalarAggregatePayload(measures=restored)).logical_plan.payload.measures[0]==bound
    forged=deepcopy(artifact.model_dump(mode='json'));forged['payload'][0]['business_domain_ids']=['206']
    forged['payload_digest']=contract_digest(forged['payload'])
    with pytest.raises(ValueError,match='SCOPED_STATE_BINDING_MISMATCH'):
        session(provider).restore(ScopedArtifact.model_validate(forged),kind='SEMANTIC_BINDINGS')


def test_task_patch_cannot_introduce_foreign_bound_state(provider):
    from tools.phase25_1.fixtures import operation
    current=session(provider)
    patch=TaskPatch.compile([operation('SET',value=[old_ref().model_dump(mode='json')],base=0)],base_task_version=0)
    with pytest.raises(ValueError,match='PINNED_CATALOG_BINDING_REQUIRED'):
        current.resolve_turn(parsed=CurrentTurnSemanticParse(),task_patch=patch,
            semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'))


def test_schema_export_and_model_output_authority_boundaries(provider):
    import json
    from jsonschema import Draft202012Validator
    from app.semantic_v2.schema import draft_2020_12_schema
    from app.semantic_v2.pipeline import CandidateSelectionDecision
    schema=draft_2020_12_schema(AuthorizedLogicalPlan)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(compile_plan(session(provider)).logical_plan.model_dump(mode='json'))
    for model in [CurrentTurnSemanticParse,CandidateSelectionDecision]:
        text=json.dumps(model.model_json_schema())
        for field in ['authorized_scope','permission_proofs','catalog_pin','state_namespace']:
            assert '"'+field+'"' not in text


def test_history_restore_checks_current_namespace_before_resolver(provider):
    from app.semantic_v2.state_machine import ConversationState
    current=session(provider)
    raw=ConversationState(conversation_id='foreign',application_id='app',tenant_id='tenant',user_id='user',state_version=0).model_dump(mode='json')
    # Simulate a wrongly keyed trusted-store artifact; the embedded identity is
    # checked too, even if its context label matches the current session.
    artifact=stored_artifact(current,'CONVERSATION',raw)
    with pytest.raises(ValueError,match='SCOPED_STATE_REUSE_REJECTED'):
        current.resolve_turn(parsed=CurrentTurnSemanticParse(),task_patch=TaskPatch(base_task_version=0),
            semantic_resolution=SemanticResolutionContract(status='UNRESOLVED'),state=artifact)


def test_foreign_resolution_or_changed_parse_cannot_bypass_scoped_resolver(provider):
    from tools.phase25_1.fixtures import candidate_resolution
    current=session(provider);payload=ScalarAggregatePayload(measures=[measure(current)])
    parsed=CurrentTurnSemanticParse()
    resolution=current.resolve_turn(parsed=parsed,task_patch=TaskPatch(base_task_version=0),semantic_resolution=candidate_resolution(payload))
    altered=resolution.model_copy(update={'target_task_id':'foreign'})
    with pytest.raises(ValueError,match='CURRENT_SCOPED_TURN_RESOLUTION_REQUIRED'):
        current.compile(parsed=parsed,resolution=altered,payload=payload,service_route='DATA_QUERY',analysis_goals=['AGGREGATE'],
            task_version=1,versions=AuthorizedVersionMetadata(prompt_version='fixture',policy_version='fixture',adapter_version='fixture'))


def test_task_baseline_requires_exact_restored_task_version(provider):
    from app.semantic_v2.models import ComparisonPayload, ComparisonSpec, TaskBaseline, TimeRange, TimeSpec
    from tools.phase25_1.fixtures import conversation
    current=session(provider);bound=measure(current)
    time_ref=current.bind(current.candidates(CatalogType.DIMENSION)[0]['candidate_id'],SemanticRole.TIME_FIELD)
    payload=ComparisonPayload(measures=[bound],time=TimeSpec(anchor=time_ref,
        range=TimeRange(start='2025-01-01T00:00:00Z',end_exclusive='2026-01-01T00:00:00Z'),
        grain='YEAR',timezone='UTC',source='USER_EXPLICIT',as_of='2026-01-01T00:00:00Z'),
        comparison=ComparisonSpec(comparison_type='YOY',baseline=TaskBaseline(task_id='task:a',task_version=1),calculation='GROWTH_RATE',output_metrics=[bound]))
    with pytest.raises(ValueError,match='SCOPED_TASK_RESTORE_REQUIRED'):
        compile_plan(current,payload)
    task=conversation().tasks['task:a'].model_dump(mode='json')
    task['versions'][0]['semantics']['metrics']=[bound.model_dump(mode='json')]
    current.restore(stored_artifact(current,'TASK_FRAME',task),kind='TASK_FRAME')
    # The baseline can now be validated structurally against the restored scope.
    assert ('task:a',1) in current._tasks and ('task:a',2) not in current._tasks
    assert compile_plan(current,payload).logical_plan.payload.comparison.baseline.task_version==1


def test_dataset_reference_cannot_relabel_its_owning_task(provider):
    from app.semantic_v2.authorized_contract import validate_authorized_refs
    from app.semantic_v2.models import SourceDatasetRef
    current=session(provider)
    current.restore(stored_artifact(current,'DATASET',{'dataset_id':'data'}),kind='DATASET')
    valid=SourceDatasetRef(dataset_id='data',source_task_id='task',source_task_version=1,snapshot_id='snapshot')
    proofs=tuple(current._datasets.values())
    # The current DatasetState proves ownership but carries no executed snapshot.
    # SourceDatasetRef remains closed until the execution adapter supplies it.
    with pytest.raises(ValueError,match='dataset snapshot evidence required'):
        validate_authorized_refs(valid,current._snapshot,current.context,proofs)
    with pytest.raises(ValueError,match='dataset origin mismatch'):
        validate_authorized_refs(valid.model_copy(update={'source_task_id':'foreign'}),current._snapshot,current.context,proofs)


def test_dataset_snapshot_contract_requires_exact_trusted_receipt(provider):
    from app.semantic_v2.authorized_contract import validate_authorized_refs
    from app.semantic_v2.models import SourceDatasetRef
    current=session(provider)
    current.restore(stored_artifact(current,'DATASET',{'dataset_id':'data'}),kind='DATASET')
    source=SourceDatasetRef(dataset_id='data',source_task_id='task',source_task_version=1,snapshot_id='snapshot')
    # Structural contract fixture only; the plan-only session never mints this
    # executed-snapshot receipt. Runtime execution wiring remains a cutover gate.
    proof=current._datasets['data'].model_copy(update={'snapshot_id':'snapshot'})
    validate_authorized_refs(source,current._snapshot,current.context,(proof,))
    with pytest.raises(ValueError,match='dataset snapshot evidence required'):
        validate_authorized_refs(source.model_copy(update={'snapshot_id':'other'}),current._snapshot,current.context,(proof,))
