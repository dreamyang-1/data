"""Opt-in Round 5.10 scalar V2 live read-only E2E.

The runner is intentionally isolated from HTTP routes and production state. It
allows only scope 81/[205], data source 58, scalar plans, three named business
questions, two independent validation SELECTs, and a connection-local timeout.
Raw SQL, parameters, model exchanges and result values stay in the caller's
private output directory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from time import monotonic
from unittest.mock import patch

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[2]
SERVICE_ROOT=ROOT.parent/'Oagnet'
SQL_ROOT=ROOT.parent/'sql-translator'
sys.path[:0]=[str(ROOT),str(SERVICE_ROOT),str(SQL_ROOT)]

from app.config import Settings
from app.domain.models import ChatRequest,TrustedIdentity
from app.semantic_v2.authorized_contract import ScopedArtifact,contract_digest
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.isolated_execution import (IsolatedExecutionAdapter,
    IsolatedExecutionStore,prepare_execution)
from app.semantic_v2.pipeline import AuthorizedLogicalPlan
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.state_machine import ConversationState
from app.semantic_v2.time_storage import TimeStorageContract
from pinned_catalog import translate_pinned_catalog
from semantic_scope import RequestScope
from sql_translator_prod import SQLTranslatorProd
from bound_sql import statement_fingerprint
from tools.cutover import live_followup_round5_1 as native
from tools.cutover.round53_live_recheck import sources
from tools.cutover.round55_live_chains import freeze_sources

CLOCK='2026-09-09T09:00:00+08:00'
SCOPE={'semantic_model_id':81,'business_domain_ids':[205]}
EXPECTED={
    'Q0':{'question':'查询含税销售总额','conversation':'round510-q0','metric':'sales_total_including_tax','year':None},
    'Q1':{'question':'查询去年江苏省订单笔数','conversation':'round510-q1-q2','metric':'order_count','year':2025},
    'Q2':{'question':'换今年','conversation':'round510-q1-q2','metric':'order_count','year':2026},
}


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')


def file_hash(path):return sha256(path.read_bytes()).hexdigest()


def receipt_payload(receipt):
    return {'request_fingerprint':receipt.request_fingerprint,'status':receipt.status,
        'stages':list(receipt.stages),'attempt':receipt.attempt.model_dump(mode='json') if receipt.attempt else None,
        'reason_code':receipt.reason_code,'result_digest':receipt.result_digest,'provenance':receipt.provenance}


def planner(pin,scope,asl,**policy):
    request_scope=RequestScope.from_request({'authorized_semantic_scope':scope.model_dump(mode='json')})
    return translate_pinned_catalog(pin,request_scope,asl,**policy)


def redis_config():
    values=dotenv_values(ROOT.parent/'.env')
    required=('REDIS_HOST','REDIS_PORT','REDIS_DB')
    if any(values.get(k) in (None,'') for k in required):raise ValueError('DATA_SOURCE_CONFIG_UNAVAILABLE')
    return {'host':values['REDIS_HOST'],'port':int(values['REDIS_PORT']),
        'db':int(str(values['REDIS_DB']).split()[0]),'password':values.get('REDIS_PASSWORD') or None,
        'decode_responses':True,'socket_timeout':10,'socket_connect_timeout':10,'protocol':2}


def prepare_native(request,result,publication):
    session=ScopedPlanSession(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),publication)
    session.restore(result.next_state,kind='CONVERSATION')
    restored=session.restore(result.plan_state,kind='LAST_REQUEST')
    plan=AuthorizedLogicalPlan.model_validate(restored)
    if plan.payload.payload_type!='SCALAR_AGGREGATE':raise ValueError('NON_SCALAR_PLAN_FORBIDDEN')
    evidence=None
    if plan.payload.time is not None:
        fields=[f for table in session._pin.snapshot['physical_catalog']['tables'] for f in table['fields']
            if f['field_id']==24400]
        if len(fields)!=1:raise ValueError('CURRENT_TIME_FIELD_EVIDENCE_MISMATCH')
        metadata=fields[0]
        evidence=TimeStorageContract(evidence_version='round59-user-declaration-beijing-v1',
            provenance='DECLARED',evidence_reference='USER_DECLARATION_ROUND59_BEIJING',
            context=session.context,field_canonical_id=plan.payload.time.anchor.canonical_id,
            field_mapping='sales_order.created_date',physical_field_id=24400,table_id=1880,
            data_source_id=58,physical_field_digest=digest(metadata),storage_timezone='Asia/Shanghai',
            storage_semantics='LOCAL_WALL_DATETIME',fractional_seconds_precision=0,
            applicability=plan.payload.time.range)
    options={} if evidence is None else {'time_storage':evidence,'time_evidence_digest':evidence.fingerprint}
    prepared=prepare_execution(session,plan,sql_planner=planner,**options)
    return session,plan,prepared,evidence


def validate_prepared(case_id,plan,prepared):
    expected=EXPECTED[case_id];sql=prepared.sql_receipt
    if str(sql.get('data_source_id'))!='58':raise ValueError('UNAUTHORIZED_DATA_SOURCE')
    SQLTranslatorProd.validate_read_only_sql(sql['sql'])
    if not sql['sql'].rstrip().endswith('LIMIT 1'):raise ValueError('SCALAR_LIMIT_CONTRACT_MISSING')
    measures=[v.canonical_code for v in plan.payload.measures]
    if measures!=[expected['metric']]:raise ValueError('METRIC_SEMANTICS_MISMATCH')
    tables=SQLTranslatorProd._sql_involved_tables(sql['sql'])
    if case_id=='Q0':
        if tables!={'sales_order'} or sql.get('sql_parameters') not in (None,{}):
            raise ValueError('Q0_PLAN_SCOPE_MISMATCH')
        if 'SUM(sales_order.amount_with_tax)' not in sql['sql']:
            raise ValueError('Q0_FORMULA_MISMATCH')
    else:
        if tables!={'sales_order','hospital','dim_province'}:
            raise ValueError('ANNUAL_PLAN_TABLE_SCOPE_MISMATCH')
        if 'COUNT(DISTINCT sales_order.order_key)' not in sql['sql']:
            raise ValueError('ORDER_COUNT_FORMULA_MISMATCH')
        values=list((sql.get('sql_parameters') or {}).values())
        target=['江苏省',f"{expected['year']}-01-01 00:00:00",f"{expected['year']+1}-01-01 00:00:00"]
        if values!=target:raise ValueError('ANNUAL_PLAN_PARAMETER_MISMATCH')
        if 'hospital.hospital_id = sales_order.hospital_id' not in sql['sql'] or \
                'hospital.province_id = dim_province.province_id' not in sql['sql']:
            raise ValueError('ANNUAL_PLAN_JOIN_MISMATCH')
    return {'case_id':case_id,'plan_id':plan.plan_id,'task_id':plan.task_id,'task_version':plan.task_version,
        'semantic_fingerprint':plan.semantic_fingerprint,'prepared_fingerprint':prepared.fingerprint,
        'sql_digest':digest(sql['sql']),'parameter_digest':digest(sql.get('sql_parameters')),
        'tables':sorted(tables),'metric':measures[0],'year':expected['year']}


async def recognize(case_id,model,catalog,raw,snapshot,observations,evidence_root,*,state=None,plans=()):
    expected=EXPECTED[case_id];activation=state.context.catalog_pin.activation_id if state else None
    denied=[]
    with native.network_guard(),sources(evidence_root),ExitStack() as stack:
        stack.enter_context(patch.object(sys,'path',[*sys.path,str(SERVICE_ROOT),str(SERVICE_ROOT/'tests')]))
        publication=native.frozen_publication(raw,catalog,denied,native_source_values=True,
            recorded_activation_id=activation)
        source=native.FrozenSourceValues(observations,catalog=catalog,snapshot=snapshot,
            expected_hash=observations['artifact_hash'],allow_synthetic=False)
        import catalog_value_sources
        stack.enter_context(patch.object(catalog_value_sources,'observe',source.observe))
        stack.enter_context(patch.object(catalog_value_sources,'observe_probe',source.observe_probe))
        engine=RawTurnPlanner(model,publication,clock=lambda:datetime.fromisoformat(CLOCK))
        request=ChatRequest(**SCOPE,application_id='isolated-evaluation',conversation_id=expected['conversation'],
            message_id='round510-'+case_id.lower(),question=expected['question'])
        model.begin_turn('R510-'+case_id,0);started=len(model.calls);began=monotonic()
        result=await asyncio.wait_for(engine.run(request,TrustedIdentity(tenant_id='evaluation',user_id='evaluation'),
            state=state,plans=plans),timeout=125)
        elapsed=monotonic()-began
        if denied:raise ValueError('UNRECORDED_SOURCE_READ')
        return request,result,publication,source,elapsed,deepcopy(model.calls[started:]),deepcopy(model.exchanges)


def execute_prepared(case_id,prepared,result,translator,source,records):
    state=ConversationState.model_validate(result.next_state.payload)
    store=IsolatedExecutionStore(prepared.plan.permission_requirement,state,provenance='LIVE_READ_ONLY')
    calls=[]
    def transport(request):
        if request.data_source_id!='58' or request.context!=prepared.plan.permission_requirement:
            raise ValueError('EXECUTION_SCOPE_PIN_MISMATCH')
        started=monotonic()
        db_result=SQLTranslatorProd.execute_sql_on_data_source(ds_config=source,**request.executor_arguments())
        elapsed=monotonic()-started
        calls.append({'request_fingerprint':request.fingerprint,'prepared_fingerprint':request.prepared_fingerprint,
            'sql':request.sql,'parameters':deepcopy(request.parameters),'sql_digest':digest(request.sql),
            'parameter_digest':digest(request.parameters),'result':deepcopy(db_result),'seconds':elapsed})
        return {'request_fingerprint':request.fingerprint,'prepared_fingerprint':request.prepared_fingerprint,
            'context_fingerprint':request.context.fingerprint(),'data_source_id':request.data_source_id,
            'provenance':'LIVE_READ_ONLY','submitted':db_result.get('business_query_submitted') is True,
            'result':db_result}
    adapter=IsolatedExecutionAdapter(transport=transport,store=store,
        clock=lambda:datetime.now(timezone.utc).astimezone())
    receipt=adapter.execute(prepared,current_context=store.context,message_id='round510-execute-'+case_id.lower(),
        expected_state_version=store.state.state_version)
    # Exact idempotency check must reuse the receipt without another SELECT.
    again=adapter.execute(prepared,current_context=store.context,message_id='round510-execute-'+case_id.lower(),
        expected_state_version=state.state_version)
    if again!=receipt or len(calls)!=1:raise ValueError('EXECUTION_IDEMPOTENCY_FAILURE')
    records.extend(calls)
    return store,receipt,calls[0]


def independent_validation(year,translator,source):
    sql=("SELECT COUNT(DISTINCT so.order_key) AS reference_order_count FROM sales_order AS so "
         "INNER JOIN hospital AS h ON h.hospital_id = so.hospital_id "
         "INNER JOIN dim_province AS p ON p.province_id = h.province_id "
         "WHERE p.province_name = %(v2_p0)s AND so.created_date >= %(v2_p1)s "
         "AND so.created_date < %(v2_p2)s")
    parameters={'v2_p0':'江苏省','v2_p1':f'{year}-01-01 00:00:00',
        'v2_p2':f'{year+1}-01-01 00:00:00'}
    started=monotonic();result=SQLTranslatorProd.execute_sql_on_data_source(sql,source,parameters=parameters,
        parameter_fingerprint=statement_fingerprint(sql,parameters),require_consistent_snapshot=True,
        execution_timeout_ms=30_000)
    return {'year':year,'sql':sql,'parameters':parameters,'sql_digest':digest(sql),
        'parameter_digest':digest(parameters),'result':result,'seconds':monotonic()-started}


async def run(args):
    if not (args.allow_model_calls and args.allow_read_only_source_58):raise ValueError('EXPLICIT_OPT_IN_REQUIRED')
    if datetime.now(timezone.utc)>=datetime.fromisoformat(args.deadline):raise ValueError('ROUND510_DEADLINE_EXCEEDED')
    args.output.mkdir(parents=True,exist_ok=False)
    settings=Settings().model_copy(update={'intent_model_max_retries':0,'intent_model_timeout_seconds':60})
    if settings.intent_model_name!='qwen3.7-max' or settings.intent_model_enable_thinking is not False:
        raise ValueError('FROZEN_MODEL_CONFIG_MISMATCH')
    catalog=native.read(ROOT/'docs/cutover/evaluation_gates/frozen_catalog.json')
    raw=(ROOT/'.eval_private/harness-20260909T083000Z/catalog_snapshot.json').read_bytes()
    snapshot=native.read(args.evidence/'live_catalog_snapshot.json')
    observations=native.read(args.evidence/'source_observations.json')
    if sha256(raw).hexdigest()!=catalog['source_snapshot_sha256'] or digest(snapshot)!=digest(json.loads(raw)):
        raise ValueError('FROZEN_CURRENT_CATALOG_MISMATCH')
    preflight=native.read(args.preflight);physical=native.read(args.physical_preflight)
    if preflight['status']!='PASS' or physical['status']!='PASS' or preflight['metadata_or_source_checks']+physical['metadata_or_source_checks']>8:
        raise ValueError('LIVE_PREFLIGHT_FAILED')
    source_translator=SQLTranslatorProd(redis_config());source=source_translator.fetch_data_source('81','58')
    if not source or str(source.get('id'))!='58' or str(source.get('semantic_model_id'))!='81':
        raise ValueError('SOURCE_58_SCOPE_RESOLUTION_FAILED')
    source_safe={'id':'58','semantic_model_id':'81','db_type':source.get('db_type'),
        'identity_digest':digest({'id':source.get('id'),'semantic_model_id':source.get('semantic_model_id'),
            'db_name':source.get('db_name'),'db_type':source.get('db_type')})}
    runtime_files=[ROOT/'app/semantic_v2/isolated_execution.py',ROOT/'app/semantic_v2/time_storage.py',
        ROOT/'app/semantic_v2/asl2.py',ROOT/'app/semantic_v2/catalog_bridge.py',
        SQL_ROOT/'sql_translator_prod.py',SQL_ROOT/'pinned_catalog.py',SQL_ROOT/'bound_sql.py']
    runtime_hashes={str(p.relative_to(ROOT.parent)).replace('\\','/'):file_hash(p) for p in runtime_files}
    source_hashes=freeze_sources(args.evidence)
    write(args.output/'input_freeze.json',{'started_at':datetime.now(timezone.utc).isoformat(),
        'deadline':args.deadline,'questions':EXPECTED,'scope':SCOPE,'data_source':source_safe,'clock':CLOCK,
        'model':{'name':'qwen3.7-max','thinking':False,'temperature':0,'retry':0,'max_calls':8},
        'catalog':{'version':catalog['catalog_version'],'snapshot_sha256':sha256(raw).hexdigest(),
            'source':'FROZEN_CERTIFIED_SNAPSHOT; CURRENT_SNAPSHOT_DIGEST_MATCHED'},
        'preflight_hash':file_hash(args.preflight),'physical_preflight_hash':file_hash(args.physical_preflight),
        'runtime_hashes':runtime_hashes,'source_hashes':source_hashes,'business_select_budget':3,
        'independent_select_budget':2,'metadata_source_checks':6,'public_route_changed':False})
    model=native.ModelRecorder(settings,max_calls=8,capture_exchanges=True)
    captures={};preparations={};stores={};receipts={};executions=[];status='PARTIAL'
    try:
        with sources(args.evidence):
            for case_id in ('Q0','Q1'):
                request,result,publication,source_reads,elapsed,calls,exchanges=await recognize(case_id,model,catalog,raw,
                    snapshot,observations,args.evidence)
                if result.plan is None:raise ValueError(case_id+'_PLAN_REQUIRED')
                session,plan,prepared,time_evidence=prepare_native(request,result,publication)
                check=validate_prepared(case_id,plan,prepared)
                store,receipt,execution=execute_prepared(case_id,prepared,result,source_translator,source,executions)
                captures[case_id]=(request,result,publication)
                preparations[case_id]=prepared;stores[case_id]=store;receipts[case_id]=receipt
                write(args.output/(case_id.lower()+'.json'),{'case_id':case_id,'question':EXPECTED[case_id]['question'],
                    'recognition':result.model_dump(mode='json'),'model_calls':calls,'model_exchanges':exchanges,
                    'recognition_seconds':elapsed,'source_calls':source_reads.calls,'plan_validation':check,
                    'time_evidence':time_evidence.model_dump(mode='json') if time_evidence else None,
                    'lowering':asdict(prepared.lowering),'sql_receipt':prepared.sql_receipt,
                    'execution':execution,'receipt':receipt_payload(receipt),'state':store.state.model_dump(mode='json')})
                if receipt.status!='SUCCEEDED':raise ValueError(case_id+'_EXECUTION_FAILED')
            q1_request,q1_result,q1_publication=captures['Q1'];q1_store=stores['Q1']
            state=q1_store.scoped_state(q1_result.next_state)
            request,result,publication,source_reads,elapsed,calls,exchanges=await recognize('Q2',model,catalog,raw,
                snapshot,observations,args.evidence,state=state,plans=(q1_result.plan_state,))
            if result.plan is None:raise ValueError('Q2_PLAN_REQUIRED')
            session,plan,prepared,time_evidence=prepare_native(request,result,publication)
            check=validate_prepared('Q2',plan,prepared)
            if plan.task_id!=preparations['Q1'].plan.task_id or plan.task_version!=preparations['Q1'].plan.task_version+1:
                raise ValueError('Q2_TASK_CONTINUITY_MISMATCH')
            q2_before=ConversationState.model_validate(result.next_state.payload)
            q1_receipt=receipts['Q1']
            if q1_receipt.attempt.execution_id not in q2_before.execution_attempts or \
                    q1_receipt.attempt.dataset_id not in q2_before.datasets:
                raise ValueError('Q1_EXECUTION_STATE_DROPPED')
            store,receipt,execution=execute_prepared('Q2',prepared,result,source_translator,source,executions)
            preparations['Q2']=prepared;stores['Q2']=store;receipts['Q2']=receipt
            write(args.output/'q2.json',{'case_id':'Q2','question':EXPECTED['Q2']['question'],
                'recognition':result.model_dump(mode='json'),'model_calls':calls,'model_exchanges':exchanges,
                'recognition_seconds':elapsed,'source_calls':source_reads.calls,'plan_validation':check,
                'time_evidence':time_evidence.model_dump(mode='json'),'lowering':asdict(prepared.lowering),
                'sql_receipt':prepared.sql_receipt,'execution':execution,'receipt':receipt_payload(receipt),
                'state':store.state.model_dump(mode='json')})
            if receipt.status!='SUCCEEDED':raise ValueError('Q2_EXECUTION_FAILED')
        validations=[]
        for year in (2025,2026):validations.append(independent_validation(year,source_translator,source))
        for case_id,validation in zip(('Q1','Q2'),validations):
            main=next(iter(next(v for v in executions if v['request_fingerprint']==receipts[case_id].request_fingerprint)['result']['data'][0].values()))
            reference=validation['result']['data'][0]['reference_order_count'] if validation['result'].get('success') else None
            validation['matches_main']=validation['result'].get('success') is True and main==reference
            validation['main_value_digest']=digest(main);validation['reference_value_digest']=digest(reference)
        write(args.output/'independent_validation.json',validations)
        q2_state=stores['Q2'].state;q1_attempt=receipts['Q1'].attempt;q2_attempt=receipts['Q2'].attempt
        state_checks={'q1_attempt_preserved':q1_attempt.execution_id in q2_state.execution_attempts,
            'q1_dataset_preserved':q1_attempt.dataset_id in q2_state.datasets,
            'q2_attempt_saved':q2_attempt.execution_id in q2_state.execution_attempts,
            'q2_dataset_saved':q2_attempt.dataset_id in q2_state.datasets,
            'latest_dataset_is_q2':q2_state.tasks[q2_attempt.task_id].last_dataset_id==q2_attempt.dataset_id,
            'q1_q2_sql_distinct':executions[1]['sql_digest']!=executions[2]['sql_digest'] or
                executions[1]['parameter_digest']!=executions[2]['parameter_digest'],
            'other_tasks_unchanged':len(q2_state.tasks)==1}
        if not all(state_checks.values()) or not all(v['matches_main'] for v in validations):
            raise ValueError('STATE_OR_INDEPENDENT_VALUE_VALIDATION_FAILED')
        if len(executions)!=3 or len(model.calls)>8:raise ValueError('EXECUTION_BUDGET_VIOLATION')
        status='COMPLETE'
        summary={'status':'SCALAR_LIVE_READ_ONLY_E2E_COMPLETE','completed_at':datetime.now(timezone.utc).isoformat(),
            'cases':{k:{'recognition':'PLAN','execution':receipts[k].status,
                'request_fingerprint':receipts[k].request_fingerprint,'result_digest':receipts[k].result_digest,
                'sql_digest':next(v for v in executions if v['request_fingerprint']==receipts[k].request_fingerprint)['sql_digest'],
                'snapshot_id_digest':digest(receipts[k].attempt.snapshot_id)} for k in ('Q0','Q1','Q2')},
            'state_checks':state_checks,'independent_validation':[{'year':v['year'],'matches_main':v['matches_main'],
                'main_value_digest':v['main_value_digest'],'reference_value_digest':v['reference_value_digest'],
                'seconds':v['seconds']} for v in validations],
            'counts':{'model_requests':len(model.calls),'business_selects':3,'independent_selects':2,
                'metadata_source_checks':6,'retries':0,'sql_writes':0,'production_state_writes':0},
            'runtime_hashes':runtime_hashes,'source_hashes':source_hashes,'catalog_version':catalog['catalog_version'],
            'source':source_safe,'model_calls':model.calls,'state_store':'ISOLATED_IN_MEMORY_LIVE_READ_ONLY',
            'public_route_changed':False,'production_process_restarted':False,'blind_access':0}
        write(args.output/'result.json',summary)
    finally:
        await model.upstream.aclose()
        write(args.output/'final_counter.json',{'status':status,'model_requests':len(model.calls),
            'business_select_attempts':len(executions),'production_state_writes':0,'sql_writes':0})
    if {str(p):file_hash(p) for p in runtime_files}!={str(p):v for p,v in zip(runtime_files,runtime_hashes.values())}:
        raise ValueError('RUNTIME_CHANGED_DURING_E2E')
    print(json.dumps({'status':'SCALAR_LIVE_READ_ONLY_E2E_COMPLETE','model_requests':len(model.calls),
        'business_selects':3,'independent_selects':2,'state_checks':state_checks},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--preflight',type=Path,required=True)
    parser.add_argument('--physical-preflight',type=Path,required=True)
    parser.add_argument('--deadline',required=True)
    parser.add_argument('--allow-model-calls',action='store_true',required=True)
    parser.add_argument('--allow-read-only-source-58',action='store_true',required=True)
    asyncio.run(run(parser.parse_args()))
