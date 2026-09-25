"""Opt-in typed execution preparation and isolated lifecycle adapter.

No HTTP route, default transport, database configuration or production store.
The injected transport uses the existing execute_sql_on_data_source protocol;
its request/response binding is a trusted local adapter boundary, not a remote
signed execution receipt. Results never become public API or persistence artifacts.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from decimal import Decimal
import math
from threading import RLock
from typing import Callable

from . import models as m
from .asl2 import ASL2Lowering, lower_asl2, prove_asl2_result
from .authorized_contract import (AuthorizedScopeContext, ScopedArtifact, contract_digest,
                                  scoped_artifact_material)
from .pipeline import AuthorizedLogicalPlan
from .result_contract import completed_allowed
from .state_machine import ConversationState, DatasetState, StateTransitionError


def require(condition, code):
    if not condition:
        raise ValueError(code)


def proof(*checks, evidence):
    return m.ContractProof(status='PASS', checks=[m.ProofCheck(check_id=c,status='PASS',severity='BLOCKING',
        evidence_ids=[evidence]) for c in checks],evidence_ids=[evidence])


@dataclass(frozen=True)
class PreparedExecution:
    plan: AuthorizedLogicalPlan
    lowering: ASL2Lowering
    sql_receipt: dict
    state_identity: dict
    fingerprint: str

    def __post_init__(self):
        for name in ('sql_receipt','state_identity'):
            object.__setattr__(self,name,m.freeze_contract(getattr(self,name)))


def preparation_digest(plan, lowering, sql_receipt, state_identity):
    material=asdict(lowering)
    material['result_contract']=lowering.result_contract.model_dump(mode='json')
    material['output_bindings']=[b.model_dump(mode='json') for b in lowering.output_bindings]
    return contract_digest(dict(plan=plan.model_dump(mode='json'),
        lowering=material, sql_receipt=sql_receipt,state_identity=state_identity))


def prepare_execution(session, plan, *, sql_planner, **time_options):
    """Revalidate current plan/pin and invoke the native deterministic planner."""
    plan=AuthorizedLogicalPlan.model_validate_json(plan.model_dump_json())
    require(plan.payload.payload_type=='SCALAR_AGGREGATE','EXECUTION_PAYLOAD_UNSUPPORTED')
    lowering=lower_asl2(session,plan,**time_options)
    result=session._plan_asl2(lowering,sql_planner)
    if result is None:
        raise ValueError(lowering.blockers[0])
    report=result.get('semantic_validation_report',{})
    require(report.get('status')=='PASS', 'EXECUTION_SQL_SEMANTIC_VALIDATION_REQUIRED')
    identity=session._state_identity()
    return PreparedExecution(plan,lowering,result,identity,preparation_digest(plan,lowering,result,identity))


@dataclass(frozen=True)
class TypedExecutionRequest:
    """Internal scalar parameter protocol; source credentials are never carried."""
    fingerprint: str
    prepared_fingerprint: str
    context: AuthorizedScopeContext
    plan_id: str
    task_id: str
    task_version: int
    message_id: str
    data_source_id: str
    sql: str
    parameters: dict | None
    parameter_fingerprint: str | None
    provenance: str = 'TEST_ONLY'

    def __post_init__(self):
        object.__setattr__(self,'parameters',m.freeze_contract(self.parameters))

    def executor_arguments(self):
        """Exact keyword protocol of SQLTranslatorProd.execute_sql_on_data_source."""
        return dict(sql=self.sql, parameters=deepcopy(self.parameters),
                    parameter_fingerprint=self.parameter_fingerprint, require_consistent_snapshot=True,
                    execution_timeout_ms=30_000,
                    preserve_decimal=self.provenance=='LIVE_READ_ONLY')


@dataclass(frozen=True)
class IsolatedExecutionReceipt:
    request_fingerprint: str
    status: str
    stages: tuple[str,...]
    attempt: m.ExecutionAttemptRecord | None
    reason_code: str | None = None
    result_digest: str | None = None
    provenance: str = 'TEST_ONLY'

    def __post_init__(self):
        object.__setattr__(self,'attempt',m.freeze_contract(self.attempt))


class IsolatedExecutionStore:
    """In-memory isolated store. No production persistence/export adapter."""

    def __init__(self, context, state, *, provenance='TEST_ONLY'):
        require(provenance in {'TEST_ONLY','LIVE_READ_ONLY'},'ISOLATED_EXECUTION_PROVENANCE_INVALID')
        self.context=context
        self.provenance=provenance
        self._state=ConversationState.model_validate_json(state.model_dump_json())
        self._lock=RLock()
        self._messages={}
        self._receipts={}

    @property
    def state(self):
        with self._lock:
            return self._state.model_copy(deep=True)

    def compare_and_swap(self, expected_version, state):
        with self._lock:
            if self._state.state_version!=expected_version or state.state_version!=expected_version+1:
                raise StateTransitionError('conversation state version conflict')
            for name in ('conversation_id','tenant_id','user_id','application_id'):
                require(getattr(state,name)==getattr(self._state,name),'EXECUTION_STATE_IDENTITY_MISMATCH')
            self._state=ConversationState.model_validate_json(state.model_dump_json())

    def scoped_state(self, previous):
        """Seal the exact isolated state for another native V2 turn."""
        previous=ScopedArtifact.model_validate(previous.model_dump(mode='json'))
        require(previous.kind=='CONVERSATION' and previous.context==self.context,
                'EXECUTION_STATE_ARTIFACT_MISMATCH')
        before=ConversationState.model_validate(previous.payload)
        current=self.state
        for name in ('conversation_id','tenant_id','user_id','application_id'):
            require(getattr(before,name)==getattr(current,name),'EXECUTION_STATE_IDENTITY_MISMATCH')
        payload=current.model_dump(mode='json')
        material=scoped_artifact_material(payload,previous.source_value_bindings)
        return ScopedArtifact(kind='CONVERSATION',context=self.context,payload=payload,
            source_value_bindings=previous.source_value_bindings,payload_digest=contract_digest(material))


def _state_with_attempt(state, attempt, *, dataset_id=None):
    data=state.model_dump(mode='json')
    data['execution_attempts'][attempt.execution_id]=attempt.model_dump(mode='json')
    task=data['tasks'][attempt.task_id]
    if dataset_id is not None:
        require(attempt.status=='SUCCEEDED','EXECUTION_SUCCESS_PROOF_REQUIRED')
        data['datasets'][dataset_id]=DatasetState(dataset_id=dataset_id,task_id=attempt.task_id,
            task_version=attempt.task_version).model_dump(mode='json')
        task['last_executed_version']=attempt.task_version
        task['last_dataset_id']=dataset_id
    data['state_version']+=1
    return ConversationState.model_validate(data)


def _response_matches(request, response):
    return (isinstance(response,dict) and response.get('request_fingerprint')==request.fingerprint
        and response.get('prepared_fingerprint')==request.prepared_fingerprint
        and response.get('context_fingerprint')==request.context.fingerprint()
        and str(response.get('data_source_id'))==request.data_source_id
        and response.get('provenance')==request.provenance)


def _validate_response(request, lowering, response):
    require(_response_matches(request,response), 'EXECUTION_RESULT_IDENTITY_MISMATCH')
    result=response.get('result',{})
    if isinstance(result,dict) and result.get('error_code')=='SQL_EXECUTION_TIMEOUT':
        raise TimeoutError('isolated read-only execution timeout')
    require(isinstance(result,dict) and result.get('success') is True,'EXECUTION_TRANSPORT_FAILURE')
    require(response.get('submitted') is True,'EXECUTION_SUBMISSION_UNPROVEN')
    rows,columns=result.get('data'),result.get('columns')
    require(isinstance(rows,list) and isinstance(columns,list) and all(isinstance(r,dict) for r in rows)
        and all(set(r)==set(columns) for r in rows)
        and type(result.get('row_count')) is int and result['row_count']==len(rows)
        and not result.get('download_url') and not result.get('preview_truncated')
        and result.get('truncated',False) is False, 'EXECUTION_RESULT_COMPLETENESS_UNPROVEN')
    require(len(rows)==1 and lowering.result_contract.expected_cardinality.kind=='SCALAR',
            'EXECUTION_RESULT_GRAIN_MISMATCH')
    required={'consistent_snapshot','read_only_transaction','column_contract_valid','row_contract_valid',
              'row_count_reconciled'}
    if request.provenance=='LIVE_READ_ONLY':required.add('statement_timeout_enforced')
    checks=result.get('quality_checks',{})
    require(result.get('quality_status')=='PASS' and all(checks.get(k) is True for k in required)
        and isinstance(result.get('snapshot_id'),str) and bool(result['snapshot_id']),
        'EXECUTION_RESULT_SNAPSHOT_UNPROVEN')
    # Numeric measures are numeric even when the older structural contract did
    # not export a dtype. Do not infer arbitrary attribute types from row values.
    by_id={b.output_field_id:b.result_column_name for b in lowering.output_bindings}
    for output in lowering.result_contract.required_outputs:
        if output.semantic_ref.catalog_type=='METRIC':
            require(all(r.get(by_id[output.output_field_id]) is None or
                (type(r[by_id[output.output_field_id]]) in (int,float,Decimal)
                 and _finite_number(r[by_id[output.output_field_id]])) for r in rows),
                'EXECUTION_RESULT_MEASURE_TYPE_MISMATCH')
    result_proof=prove_asl2_result(lowering,columns=columns,rows=rows,truncated=False,snapshot_id=result['snapshot_id'])
    require(completed_allowed(result_proof),'EXECUTION_RESULT_CONTRACT_FAILURE')
    return result, result_proof


def _finite_number(value):
    """Validate Decimal without converting it through binary float."""
    return value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)


class IsolatedExecutionAdapter:
    """No default transport. A caller must inject a bounded isolated operation."""
    def __init__(self, *, transport: Callable, store: IsolatedExecutionStore, clock: Callable):
        require(type(store) is IsolatedExecutionStore and callable(transport),'ISOLATED_EXECUTION_ONLY')
        self.transport,self.store,self.clock=transport,store,clock

    def execute(self, prepared, *, current_context, message_id, expected_state_version):
        require(isinstance(prepared,PreparedExecution),'EXECUTION_PREPARATION_REQUIRED')
        plan,low,sql=prepared.plan,prepared.lowering,prepared.sql_receipt
        require(current_context==plan.permission_requirement==self.store.context,'EXECUTION_SCOPE_PIN_MISMATCH')
        require(prepared.fingerprint==preparation_digest(plan,low,sql,prepared.state_identity),
                'EXECUTION_PREPARATION_IDENTITY_MISMATCH')
        require(low.status=='SUPPORTED_PLAN_ONLY' and low.semantic_fingerprint==plan.semantic_fingerprint
            and low.result_contract.semantic_fingerprint==plan.semantic_fingerprint,'EXECUTION_PLAN_MISMATCH')
        require(isinstance(message_id,str) and 0<len(message_id)<=500,'EXECUTION_MESSAGE_ID_INVALID')
        identifier=contract_digest([current_context.fingerprint(),message_id,prepared.fingerprint])
        parameter=sql.get('sql_parameter_contract',{})
        request=TypedExecutionRequest(identifier,prepared.fingerprint,current_context,plan.plan_id,plan.task_id,
            plan.task_version,message_id,str(sql['data_source_id']),sql['sql'],sql.get('sql_parameters'),
            parameter.get('statement_fingerprint'),self.store.provenance)
        store=self.store
        with store._lock:
            previous=store._messages.get(message_id)
            require(previous is None or previous==identifier,'EXECUTION_MESSAGE_CONTENT_CONFLICT')
            if previous is not None:
                return store._receipts[identifier]
            state=store.state
            require(all(getattr(state,k)==v for k,v in prepared.state_identity.items()),'EXECUTION_STATE_IDENTITY_MISMATCH')
            task=state.tasks.get(plan.task_id)
            require(state.state_version==expected_state_version and task is not None
                and task.active_version==plan.task_version,'EXECUTION_STATE_VERSION_CONFLICT')
            version=next(v for v in task.versions if v.version==task.active_version)
            require(version.plan_id==plan.plan_id and task.status=='RESOLVED','EXECUTION_TASK_PLAN_MISMATCH')
            namespace='isolated-live-read-only' if store.provenance=='LIVE_READ_ONLY' else 'isolated-test-only'
            attempt=m.ExecutionAttemptRecord(execution_id=namespace+':'+identifier,task_id=plan.task_id,
                task_version=plan.task_version,attempt_number=1+sum(a.task_id==plan.task_id and a.task_version==plan.task_version
                    for a in state.execution_attempts.values()),status='RUNNING',started_at=self.clock(),
                execution_backend='SEMANTIC_QUERY',snapshot_id=namespace+':pending:'+identifier,
                catalog_version=current_context.catalog_pin.catalog_version,
                vector_index_version=current_context.catalog_pin.vector_index_version,
                semantic_model_version=plan.snapshot_requirement.semantic_model_version,
                policy_version=plan.version_metadata.policy_version,asl_digest=contract_digest(low.asl),
                sql_digest=contract_digest(dict(sql=request.sql,parameters=request.parameters)))
            running=_state_with_attempt(state,attempt)
            store.compare_and_swap(state.state_version,running)
            store._messages[message_id]=identifier
            stages=['PLAN_VALIDATED','INPUT_COMPILED','SUBMISSION_ATTEMPTED']
            store._receipts[identifier]=IsolatedExecutionReceipt(identifier,'RUNNING',tuple(stages),attempt,
                provenance=store.provenance)
        try:
            response=self.transport(request)
            if _response_matches(request,response) and response.get('submitted') is True:
                stages.append('EXECUTION_SUBMITTED')
            stages.append('RESULT_RETURNED')
            result,result_proof=_validate_response(request,low,response)
            stages.append('RESULT_VALIDATED')
            result_digest=contract_digest(result)
            chain=m.ProofChain(plan=proof('current_authorized_plan','current_task_version',evidence=prepared.fingerprint),
                asl=proof('native_typed_lowering','typed_scalar_features_supported',evidence=low.compilation_fingerprint),
                sql_plan=proof('native_pinned_sql_validation','scope_pin_projection_parameters',
                    evidence=contract_digest(sql)),result=result_proof)
            data=attempt.model_dump(mode='json')
            data.update(status='SUCCEEDED',completed_at=self.clock(),snapshot_id=result['snapshot_id'],
                dataset_id=namespace+'-dataset:'+identifier,proof_chain=chain.model_dump(mode='json'))
            terminal=m.ExecutionAttemptRecord.model_validate(data)
            with store._lock:
                store.compare_and_swap(running.state_version,_state_with_attempt(running,terminal,dataset_id=terminal.dataset_id))
                stages.append('SUCCESS_RECEIPT_SAVED')
                receipt=IsolatedExecutionReceipt(identifier,'SUCCEEDED',tuple(stages),terminal,
                    result_digest=result_digest,provenance=store.provenance)
                store._receipts[identifier]=receipt
            return receipt
        except Exception as exc:
            # No exception body (SQL, credentials or result text) enters trace.
            reason=('EXECUTION_TIMEOUT_OUTCOME_UNKNOWN' if isinstance(exc,TimeoutError) else
                'EXECUTION_RECEIPT_STATE_CONFLICT' if isinstance(exc,StateTransitionError) else
                str(exc) if isinstance(exc,ValueError) and str(exc).startswith(('EXECUTION_','ASL2_'))
                else 'EXECUTION_TRANSPORT_FAILURE')
            data=attempt.model_dump(mode='json')
            data.update(status='FAILED',completed_at=self.clock(),error_type='RESULT_CONTRACT_FAILURE'
                if reason.startswith(('EXECUTION_RESULT_','ASL2_RESULT_')) else 'EXECUTION_FAILURE')
            terminal=m.ExecutionAttemptRecord.model_validate(data)
            with store._lock:
                try:store.compare_and_swap(running.state_version,_state_with_attempt(running,terminal))
                except StateTransitionError:pass  # Preserve newer state; never publish success.
                receipt=IsolatedExecutionReceipt(identifier,'FAILED',tuple(stages),terminal,reason_code=reason,
                    provenance=store.provenance)
                store._receipts[identifier]=receipt
            return receipt
