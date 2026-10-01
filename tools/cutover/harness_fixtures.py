"""Typed state preconditions; never manufacture execution receipts or aliases."""
from __future__ import annotations

from datetime import datetime,timedelta
from types import SimpleNamespace

from app.domain.models import ChatRequest,TrustedIdentity
from app.semantic_v2 import models as m
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.pending_recognition import PendingResume,pending_identity
from app.semantic_v2.state_machine import (ConversationState,TaskState,TaskVersion,TopicState,
                                         StateEvent,apply_state_event,PendingClarification,PendingBlocker)
from tools.cutover.evaluation_contract import digest


class FixtureGap(ValueError):
    pass


class ImplementationGap(ValueError):
    pass


def request_for(case, *, message_id='fixture'):
    return ChatRequest(semantic_model_id=case['scope']['semantic_model_id'],
        business_domain_ids=case['scope']['business_domain_ids'],
        database_id=case.get('database_id'),knowledge_base_names=case.get('knowledge_base_names',[]),
        conversation_id=case['case_id'],application_id='isolated-evaluation',message_id=message_id,
        question=case['current_utterance'])


def pending_fixture(case,publication,*,candidate_codes):
    """A declared Pending precondition, not a claim that history text caused it.

    Uses the actual typed Pending/Task/Resume contracts, state-event builder,
    identity function and scope seal/restore. All choices bind current catalog
    candidates. No synonym collision is asserted or added to the catalog.
    """
    if len(candidate_codes)<2 or len(set(candidate_codes))!=len(candidate_codes):
        raise FixtureGap('PENDING_PRECONDITION_NEEDS_DISTINCT_GOVERNED_OPTIONS')
    request=request_for(case);identity=TrustedIdentity(tenant_id='evaluation',user_id='evaluation')
    session=ScopedPlanSession(request,identity,publication);now=datetime.fromisoformat(case['clock'])
    candidates={v['canonical_code']:v for v in session.candidates(m.CatalogType.METRIC)}
    if not set(candidate_codes)<=candidates.keys():raise FixtureGap('PENDING_PRECONDITION_CATALOG_OPTIONS_MISSING')
    options=[m.ClarificationOption(option_id='fixture-option:'+str(i),
        display_label=candidates[code]['display_name'],
        canonical_ref=session.bind(candidates[code]['candidate_id'],'MEASURE'),
        evidence=['USER_DECLARED_PENDING_PRECONDITION']) for i,code in enumerate(candidate_codes)]
    blocker=PendingBlocker(blocker_id='fixture-metric-choice',plan_path='metrics',
        expected_answer_type='OPTION_ID',candidate_ids=[o.canonical_ref.canonical_id for o in options],
        information_gain=1,already_asked=True,options=options)
    task=TaskState(task_id='fixture-task:'+case['case_id'],topic_id='fixture-topic:'+case['case_id'],
        active_version=1,status='PROVISIONAL',versions=[TaskVersion(version=1,status='PROVISIONAL',
        semantics=m.TaskSemanticState(),created_at=now)])
    state=ConversationState(state_version=0,**session._state_identity())
    state=apply_state_event(state,event=StateEvent.NEW_TOPIC,expected_state_version=0,
        payload={'task':task,'topic':TopicState(topic_id=task.topic_id,title='Pending fixture',last_accessed_at=now)})
    operations={blocker.blocker_id:'SET'}
    pending_id=pending_identity(task.task_id,'SCALAR_AGGREGATE',operations,[blocker])
    pending=PendingClarification(pending_id=pending_id,task_id=task.task_id,task_version=1,
        topic_id=task.topic_id,slot_path='metrics',question=' / '.join(o.display_label for o in options),
        asked_at=now,created_at=now,updated_at=now,blockers=[blocker],active_blocker_id=blocker.blocker_id,
        asked_slots=['metrics'],clarification_rounds=1)
    state.pending_records[pending_id]=pending
    state=ConversationState.model_validate_json(state.model_dump_json())
    resume=PendingResume(pending_id=pending_id,task_id=task.task_id,task_version=1,
        payload_type='SCALAR_AGGREGATE',operations=operations)
    session.accept_catalog()
    state_artifact=session.seal(kind='CONVERSATION',payload=state)
    pending_artifact=session.seal(kind='PENDING',payload=resume)
    restored=ScopedPlanSession(request,identity,publication)
    assert restored.restore(state_artifact,kind='CONVERSATION')==state.model_dump(mode='json')
    assert restored.restore(pending_artifact,kind='PENDING')==resume.model_dump(mode='json')
    restored.accept_catalog()
    receipt={'format':'TYPED_PENDING_PRECONDITION_V1','origin':'BUSINESS_CONTRACT_PRECONDITION',
        'history_text_executed':False,'catalog_synonym_collision_claimed':False,
        'state_hash':digest(state_artifact.model_dump(mode='json')),
        'pending_hash':digest(pending_artifact.model_dump(mode='json')),'version':1,
        'remaining_questions':1,'scope_fingerprint':request.authorized_semantic_scope.fingerprint(),
        'clarification_reason':'USER_DECLARED_CHOICE_PENDING','serialization_roundtrip':'PASS'}
    return (state_artifact,pending_artifact,{},[SimpleNamespace(plan=None,
        pending_state=pending_artifact,next_state=state_artifact)]),receipt


def dataset_fixture(case):
    """Real V1 DatasetReference + V2 safety contract. No fake V2 adapter."""
    from minio_followup_store import DatasetReference,DatasetScope,dataset_reference_from_dict,dataset_source_complete
    source=case.get('dataset')
    if not isinstance(source,dict) or type(source.get('source_complete')) is not bool:
        raise FixtureGap('DATASET_PRECONDITION_PROOF_NOT_SPECIFIED')
    if source.get('data_origin')!='SYNTHETIC_EXPLICIT_FIXTURE_NOT_SOURCE_BUSINESS_ROWS':
        raise FixtureGap('DATASET_SYNTHETIC_PROVENANCE_REQUIRED')
    request=request_for(case);now=datetime.fromisoformat(case['clock'])
    identifier='fixture-dataset:'+case['case_id'];snapshot='fixture-snapshot:'+digest(source)[:24]
    scope=DatasetScope(tenant_id='evaluation',user_id='evaluation',application_id=request.application_id,
        conversation_id=case['case_id'],authorized_semantic_scope_fingerprint=request.authorized_semantic_scope.fingerprint())
    scope.validate();complete=source['source_complete'];count=len(source['rows'])
    provenance={'type':'query_provenance','truncated':not complete,'source_truncated':not complete,
        'ordering':[],'source_total_row_count':count if complete else None,
        'global_topn_proof':None,'origin':'SYNTHETIC_FIXTURE'}
    reference=DatasetReference(dataset_id=identifier,bucket='isolated-fixtures',object_name='private/'+snapshot,
        scope=scope,columns=tuple(source['columns']),row_count=count,byte_size=0,snapshot_id=snapshot,
        data_as_of=now.isoformat(),created_at=now.isoformat(),expires_at=(now+timedelta(hours=2)).isoformat(),
        source_type='QUERY_RESULT',source_ref='fixture-task:'+case['case_id'],
        semantic_model_id=request.semantic_model_id,business_domain_ids=tuple(request.business_domain_ids),
        metric_ids=tuple(source.get('metric_ids',())),transformation_log=(provenance,))
    restored=dataset_reference_from_dict(reference.to_dict())
    assert restored==reference
    assert dataset_source_complete(restored,count)==complete
    return reference,{'format':'NATIVE_DATASET_REFERENCE_FIXTURE_V1','reference_hash':digest(reference.to_dict()),
        'scope_fingerprint':scope.authorized_semantic_scope_fingerprint,'source_complete':complete,
        'row_count':count,'snapshot_hash':digest(snapshot),'truncated':not complete,
        'ranking_proof':'NOT_PROVEN','ordering_proof':'NOT_PROVEN',
        'serialization_roundtrip':'PASS','v2_adapter':'IMPLEMENTATION_GAP'}


def verify_v2_dataset_gap(case,publication,reference):
    """Exercise the current actual boundary and preserve its refusal."""
    request=request_for(case);session=ScopedPlanSession(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
    session.accept_catalog()
    try:session.seal(kind='DATASET',payload=reference.to_dict())
    except ValueError as exc:
        if str(exc)=='PLAN_ONLY_CANNOT_CREATE_EXECUTED_RESULT':
            raise ImplementationGap('V2_EXECUTED_DATASET_RECEIPT_ADAPTER_MISSING') from None
        raise
    raise FixtureGap('DATASET_CAPABILITY_CHANGED_REAUDIT_REQUIRED')
