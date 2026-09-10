"""Native planner + native executor on a fake DB driver; never business SQL."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
from unittest.mock import Mock

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.authorized_contract import AuthorizedScopeContext
from app.semantic_v2.isolated_execution import (prepare_execution, IsolatedExecutionAdapter, IsolatedExecutionStore)
from app.semantic_v2.state_machine import ConversationState, TaskState, TaskVersion, TopicState, DatasetState
from test_v2_asl2_lowering import provider, current, payload, args, sql
from test_v2_time_storage_lowering import timed, evidence
from sql_translator_prod import SQLTranslatorProd
from test_execution_evidence import FakeConnection, FakeCursor

NOW=datetime.fromisoformat('2026-09-09T09:00:00+08:00')


def state_for(prepared):
    plan=prepared.plan
    return ConversationState(**prepared.state_identity,state_version=1,active_topic_id=plan.topic_id,
        topic_stack=[plan.topic_id],topics={plan.topic_id:TopicState(topic_id=plan.topic_id,title='TEST_ONLY',
            active_task_id=plan.task_id,task_ids=[plan.task_id],last_accessed_at=NOW)},
        tasks={plan.task_id:TaskState(task_id=plan.task_id,topic_id=plan.topic_id,active_version=plan.task_version,
            status='RESOLVED',versions=[TaskVersion(version=plan.task_version,status='RESOLVED',plan_id=plan.plan_id,
                created_at=NOW,semantics=m.TaskSemanticState(metrics=plan.payload.measures))])})


def prepared_for(provider, time=False):
    session=current(provider);p=timed(session) if time else payload(session)
    plan=session._compile_logical(**args(session,p)).logical_plan
    e=evidence(session,p) if time else None
    return prepare_execution(session,plan,sql_planner=sql, **(dict(time_storage=e,
        time_evidence_digest=e.fingerprint,allow_test_time_storage=True) if e else {}))


class Cursor(FakeCursor):
    def __init__(self,columns,value=3):
        super().__init__();self.columns=columns;self.value=value;self.bound=[]
    def execute(self,sql,parameters=None):
        super().execute(sql)
        if sql.startswith('SELECT ') and ' AS data_as_of' not in sql:
            self.description=[(c,) for c in self.columns]
            self.bound.append((sql,deepcopy(parameters)))
    def fetchall(self):return [dict.fromkeys(self.columns,self.value)]


def fake_transport(monkeypatch,prepared,change=None):
    columns=[b.sql_alias for b in prepared.lowering.output_bindings]
    cursor=Cursor(columns);connection=FakeConnection(cursor)
    monkeypatch.setattr('sql_translator_prod.pymysql.connect',Mock(return_value=connection))
    def call(request):
        result=SQLTranslatorProd.execute_sql_on_data_source(ds_config={'db_type':'mysql',
            'id':request.data_source_id,'semantic_model_id':request.context.authorized_scope.semantic_model_id},
            **request.executor_arguments())
        response=dict(request_fingerprint=request.fingerprint,prepared_fingerprint=request.prepared_fingerprint,
            context_fingerprint=request.context.fingerprint(),data_source_id=request.data_source_id,
            provenance=request.provenance,submitted=bool(cursor.bound),result=result)
        if change:change(response)
        return response
    return Mock(side_effect=call),cursor,connection


@pytest.mark.parametrize('time',[False,True])
def test_typed_preparation_native_execution_result_and_saved_attempt(monkeypatch,provider,time):
    prepared=prepared_for(provider,time);state=state_for(prepared)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state)
    transport,cursor,connection=fake_transport(monkeypatch,prepared)
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    receipt=adapter.execute(prepared,current_context=store.context,message_id='execute',expected_state_version=1)
    assert receipt.status=='SUCCEEDED',receipt
    assert receipt.stages==('PLAN_VALIDATED','INPUT_COMPILED','SUBMISSION_ATTEMPTED','EXECUTION_SUBMITTED','RESULT_RETURNED',
        'RESULT_VALIDATED','SUCCESS_RECEIPT_SAVED')
    assert receipt.provenance=='TEST_ONLY' and receipt.attempt.status=='SUCCEEDED'
    assert cursor.bound==[(prepared.sql_receipt['sql'],prepared.sql_receipt.get('sql_parameters'))]
    assert connection.closed and cursor.executed[:2]==[
        'SET SESSION MAX_EXECUTION_TIME = 30000','SET TRANSACTION READ ONLY']
    assert store.state.tasks[prepared.plan.task_id].last_dataset_id==receipt.attempt.dataset_id
    assert state.state_version==1 and state.tasks[prepared.plan.task_id].last_dataset_id is None
    again=adapter.execute(prepared,current_context=store.context,message_id='execute',expected_state_version=1)
    assert again==receipt and transport.call_count==1


@pytest.mark.parametrize('fault',['request','prepared','scope','source','provenance','columns','row_keys','dtype',
    'nan','grain','row_count','truncated','preview','download','quality','snapshot','failure','timeout','exception','invalid_metadata','submission'])
def test_bad_transport_or_result_never_replaces_successful_dataset(monkeypatch,provider,fault):
    prepared=prepared_for(provider);state=state_for(prepared);task=state.tasks[prepared.plan.task_id]
    state.datasets['old']=DatasetState(dataset_id='old',task_id=task.task_id,task_version=task.active_version)
    task.last_dataset_id='old'
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state)
    def change(response):
        result=response['result'];column=result['columns'][0]
        if fault in ('request','prepared','scope','source','provenance'):
            response[{'request':'request_fingerprint','prepared':'prepared_fingerprint','scope':'context_fingerprint',
                'source':'data_source_id','provenance':'provenance'}[fault]]='wrong'
        elif fault=='columns':result['columns']=['wrong']
        elif fault=='row_keys':result['data'][0]['extra']=5
        elif fault=='dtype':result['data'][0][column]='3'
        elif fault=='nan':result['data'][0][column]=float('nan')
        elif fault=='grain':result['data']=[];result['row_count']=0
        elif fault=='row_count':result['row_count']=99
        elif fault=='truncated':result['truncated']=True
        elif fault=='preview':result['preview_truncated']=True
        elif fault=='download':result['download_url']='test-only-export'
        elif fault=='quality':result['quality_checks']['consistent_snapshot']=False
        elif fault=='snapshot':result.pop('snapshot_id')
        elif fault=='failure':result['success']=False
        elif fault=='timeout':raise TimeoutError('private SQL text')
        elif fault=='invalid_metadata':result['unserializable_metadata']=object()
        elif fault=='submission':response['submitted']=False
        else:raise RuntimeError('private SQL text')
    transport,_,_=fake_transport(monkeypatch,prepared,change)
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    receipt=adapter.execute(prepared,current_context=store.context,message_id='execute',expected_state_version=1)
    assert receipt.status=='FAILED' and 'SUCCESS_RECEIPT_SAVED' not in receipt.stages
    assert store.state.tasks[task.task_id].last_dataset_id=='old'
    assert len(store.state.datasets)==1 and receipt.attempt.status=='FAILED'
    assert 'private' not in str(receipt)
    if fault in ('timeout','exception','submission'):
        assert 'EXECUTION_SUBMITTED' not in receipt.stages
    assert adapter.execute(prepared,current_context=store.context,message_id='execute',expected_state_version=1)==receipt
    assert transport.call_count==1


@pytest.mark.parametrize('fault',['scope','pin','plan','sql','parameters','bindings','state_version','task_version','state_identity','task_plan'])
def test_invalid_prepared_or_current_state_blocks_submission(provider,fault):
    prepared=prepared_for(provider,True);state=state_for(prepared);context=prepared.plan.permission_requirement
    store=IsolatedExecutionStore(context,state);current_context=context
    if fault in ('scope','pin'):
        d=context.model_dump(mode='json')
        if fault=='scope':d['authorized_scope']['semantic_model_id']=82
        else:d['catalog_pin']['activation_id']='wrong'
        current_context=AuthorizedScopeContext.model_validate(d)
    elif fault=='plan':prepared=replace(prepared,plan=prepared.plan.model_copy(update={'plan_id':'wrong'}))
    elif fault in ('sql','parameters'):
        d=json.loads(json.dumps(prepared.sql_receipt))
        if fault=='sql':d['sql']='SELECT 1'
        else:d['sql_parameters']['v2_p0']='2099-01-01 00:00:00'
        prepared=replace(prepared,sql_receipt=d)
    elif fault=='bindings':prepared=replace(prepared,lowering=replace(prepared.lowering,output_bindings=()))
    else:
        d=state.model_dump(mode='json');t=d['tasks'][prepared.plan.task_id]
        if fault=='state_version':d['state_version']=2
        elif fault=='state_identity':d['conversation_id']='other'
        elif fault=='task_plan':t['versions'][0]['plan_id']='wrong'
        else:t['versions'][0]['version']=2;t['active_version']=2
        store=IsolatedExecutionStore(context,ConversationState.model_validate(d))
    transport=Mock(side_effect=AssertionError('Transport must not run'))
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    with pytest.raises(ValueError):adapter.execute(prepared,current_context=current_context,message_id='x',expected_state_version=1)
    transport.assert_not_called()


def test_newer_state_during_transport_is_preserved_and_same_message_never_reexecutes(monkeypatch,provider):
    prepared=prepared_for(provider);store=IsolatedExecutionStore(prepared.plan.permission_requirement,state_for(prepared))
    def change(response):
        state=store.state;d=state.model_dump(mode='json');d['state_version']+=1
        d['recent_turn_ids'].append('newer-turn')
        store.compare_and_swap(state.state_version,ConversationState.model_validate(d))
    transport,_,_=fake_transport(monkeypatch,prepared,change)
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    receipt=adapter.execute(prepared,current_context=store.context,message_id='x',expected_state_version=1)
    assert receipt.status=='FAILED' and receipt.reason_code=='EXECUTION_RECEIPT_STATE_CONFLICT'
    assert store.state.recent_turn_ids==['newer-turn'] and store.state.datasets=={}
    adapter.execute(prepared,current_context=store.context,message_id='x',expected_state_version=1)
    assert transport.call_count==1


def test_unknown_time_evidence_never_reaches_sql_planner_or_transport(provider):
    session=current(provider);plan=session._compile_logical(**args(session,timed(session))).logical_plan
    planner=Mock(side_effect=AssertionError('No SQL planning'))
    with pytest.raises(ValueError,match='TIME_STORAGE_TIMEZONE_UNPROVEN'):
        prepare_execution(session,plan,sql_planner=planner)
    planner.assert_not_called()


def test_grouped_shape_requires_a_separate_adapter_contract(provider):
    session=current(provider);plan=session._compile_logical(**args(session,payload(session,'GROUPED_AGGREGATE'))).logical_plan
    with pytest.raises(ValueError,match='EXECUTION_PAYLOAD_UNSUPPORTED'):
        prepare_execution(session,plan,sql_planner=Mock())


def test_historical_task_execution_does_not_modify_other_task_or_active_topic(monkeypatch,provider):
    prepared=prepared_for(provider);state=state_for(prepared)
    old=state.tasks[prepared.plan.task_id]
    other=old.model_copy(update={'task_id':'another-task','topic_id':'another-topic'},deep=True)
    state.tasks[other.task_id]=other
    state.topics[other.topic_id]=TopicState(topic_id=other.topic_id,title='Other',active_task_id=other.task_id,
        task_ids=[other.task_id],last_accessed_at=NOW)
    state.active_topic_id=other.topic_id;state.topic_stack.append(other.topic_id)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state)
    transport,_,_=fake_transport(monkeypatch,prepared)
    receipt=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW).execute(prepared,
        current_context=store.context,message_id='historical-execution',expected_state_version=1)
    assert receipt.status=='SUCCEEDED'
    assert store.state.tasks[other.task_id]==other and store.state.active_topic_id==other.topic_id
    assert old.last_dataset_id is None


def test_retry_while_original_submission_is_running_does_not_submit_twice(monkeypatch,provider):
    prepared=prepared_for(provider);store=IsolatedExecutionStore(prepared.plan.permission_requirement,state_for(prepared))
    nested=[]
    def change(response):
        nested.append(adapter.execute(prepared,current_context=store.context,message_id='x',expected_state_version=1))
    transport,_,_=fake_transport(monkeypatch,prepared,change)
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    done=adapter.execute(prepared,current_context=store.context,message_id='x',expected_state_version=1)
    assert nested[0].status=='RUNNING' and done.status=='SUCCEEDED' and transport.call_count==1


def test_same_message_cannot_retarget_another_plan(monkeypatch,provider):
    a=prepared_for(provider);b=prepared_for(provider,True)
    assert a.fingerprint!=b.fingerprint
    store=IsolatedExecutionStore(a.plan.permission_requirement,state_for(a))
    transport,_,_=fake_transport(monkeypatch,a)
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW)
    assert adapter.execute(a,current_context=store.context,message_id='x',expected_state_version=1).status=='SUCCEEDED'
    with pytest.raises(ValueError,match='EXECUTION_MESSAGE_CONTENT_CONFLICT'):
        adapter.execute(b,current_context=store.context,message_id='x',expected_state_version=store.state.state_version)
    assert transport.call_count==1


def test_snapshot_failure_prevents_the_native_driver_business_query(monkeypatch,provider):
    prepared=prepared_for(provider,True)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state_for(prepared))
    transport,cursor,connection=fake_transport(monkeypatch,prepared)
    cursor.snapshot_supported=False
    receipt=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW).execute(prepared,
        current_context=store.context,message_id='read-only',expected_state_version=1)
    assert receipt.status=='FAILED' and cursor.bound==[]
    assert 'EXECUTION_SUBMITTED' not in receipt.stages
    assert cursor.executed==['SET SESSION MAX_EXECUTION_TIME = 30000','SET TRANSACTION READ ONLY']
    assert store.state.datasets=={} and connection.closed and connection.rolled_back


def test_live_read_only_provenance_is_explicit_and_requires_timeout_proof(monkeypatch,provider):
    prepared=prepared_for(provider,True);state=state_for(prepared)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state,provenance='LIVE_READ_ONLY')
    transport,_,_=fake_transport(monkeypatch,prepared)
    receipt=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW).execute(prepared,
        current_context=store.context,message_id='live-read-only',expected_state_version=1)
    assert receipt.status=='SUCCEEDED' and receipt.provenance=='LIVE_READ_ONLY'
    assert receipt.attempt.execution_id.startswith('isolated-live-read-only:')
    assert receipt.attempt.dataset_id.startswith('isolated-live-read-only-dataset:')
    request=transport.call_args.args[0]
    assert request.provenance=='LIVE_READ_ONLY' and request.executor_arguments()['execution_timeout_ms']==30_000


def test_live_read_only_response_without_timeout_proof_never_publishes_success(monkeypatch,provider):
    prepared=prepared_for(provider);state=state_for(prepared)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state,provenance='LIVE_READ_ONLY')
    def remove_timeout(response):response['result']['quality_checks'].pop('statement_timeout_enforced')
    transport,_,_=fake_transport(monkeypatch,prepared,remove_timeout)
    receipt=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW).execute(prepared,
        current_context=store.context,message_id='live-read-only',expected_state_version=1)
    assert receipt.status=='FAILED' and receipt.reason_code=='EXECUTION_RESULT_SNAPSHOT_UNPROVEN'
    assert store.state.datasets=={}


@pytest.mark.parametrize('provenance',['LIVE','PRODUCTION',''])
def test_isolated_store_rejects_unrecognized_provenance(provider,provenance):
    prepared=prepared_for(provider)
    with pytest.raises(ValueError,match='ISOLATED_EXECUTION_PROVENANCE_INVALID'):
        IsolatedExecutionStore(prepared.plan.permission_requirement,state_for(prepared),provenance=provenance)


def test_isolated_execution_state_round_trips_through_scoped_artifact(monkeypatch,provider):
    prepared=prepared_for(provider,True);state=state_for(prepared)
    from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest,scoped_artifact_material
    payload=state.model_dump(mode='json');previous=ScopedArtifact(kind='CONVERSATION',
        context=prepared.plan.permission_requirement,payload=payload,
        payload_digest=contract_digest(scoped_artifact_material(payload,())))
    store=IsolatedExecutionStore(previous.context,state,provenance='LIVE_READ_ONLY')
    transport,_,_=fake_transport(monkeypatch,prepared)
    receipt=IsolatedExecutionAdapter(transport=transport,store=store,clock=lambda:NOW).execute(prepared,
        current_context=store.context,message_id='live',expected_state_version=1)
    sealed=store.scoped_state(previous)
    assert receipt.status=='SUCCEEDED' and sealed.context==previous.context
    restored=ConversationState.model_validate(sealed.payload)
    assert restored.state_version==3 and restored.tasks[prepared.plan.task_id].last_dataset_id==receipt.attempt.dataset_id
    assert scoped_artifact_material(sealed.payload,sealed.source_value_bindings)


def test_scoped_state_rejects_another_scope(provider):
    prepared=prepared_for(provider);state=state_for(prepared)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state,provenance='LIVE_READ_ONLY')
    from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest,scoped_artifact_material
    context=prepared.plan.permission_requirement.model_copy(deep=True)
    data=context.model_dump(mode='json');data['authorized_scope']['semantic_model_id']=82
    other=AuthorizedScopeContext.model_validate(data);payload=state.model_dump(mode='json')
    artifact=ScopedArtifact(kind='CONVERSATION',context=other,payload=payload,
        payload_digest=contract_digest(scoped_artifact_material(payload,())))
    with pytest.raises(ValueError,match='EXECUTION_STATE_ARTIFACT_MISMATCH'):
        store.scoped_state(artifact)
