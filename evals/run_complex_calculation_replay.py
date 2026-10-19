"""Replay captured real-model plans with actual persisted synthetic operands.

This verifies deterministic compilation and numerical delivery, not ASL grounding
or production query correctness. It makes no model or business-database calls.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date,datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
os.environ['LANGFUSE_TRACING_ENABLED']='false'


async def run(args):
    import httpx
    from pydantic import SecretStr
    import app.domain.semantic_description as descriptions
    import app.intent.classifier as classifier
    from app.domain.models import PlannerExtraction
    from app.planning.compound_question import CompoundQuestionRouter
    from app.planning.task_dag import database_query_choice
    from minio_followup_store import DatasetScope
    from test_complex_execution_correctness import service,context,IDENTITY
    from run_complex_question_model_evaluation import catalog_context

    if args.output.exists():
        raise SystemExit('Refusing to overwrite evidence')
    identity=json.loads((args.model_run/'identity.json').read_text(encoding='utf8'))
    cases=json.loads((args.model_run/'frozen-inputs.json').read_text(encoding='utf8'))
    responses={}
    for path in sorted(args.model_run.glob('model-response-*.json')):
        item=json.loads(path.read_text(encoding='utf8'))
        responses.setdefault(item['case_id'],[]).append(item['choices'])
    snapshot=json.loads(args.catalog.read_text(encoding='utf8'))
    platform=json.loads(args.rules.read_text(encoding='utf8'))
    if snapshot['catalog_version']!=identity['catalog_version']:
        raise SystemExit('Replay catalog does not match captured generation')
    semantic_context=catalog_context(snapshot,platform['rules'])
    if hashlib.sha256(semantic_context.encode()).hexdigest()!=identity['context_sha256']:
        raise SystemExit('Replay context does not match captured generation')
    descriptions.load_semantic_description=AsyncMock(return_value={'available':False,'source':'','content':''})
    class ReferenceDate(date):
        @classmethod
        def today(cls):
            return cls(2026,10,19)
    classifier.date=ReferenceDate
    results=[]
    for case in cases:
        if not case['family_id'].startswith('Q1_'):
            continue
        record={'case_id':case['case_id'],'question':case['question'],'status':'FAIL',
            'business_database_calls':0,'real_model_calls':0,'asl_grounding_verified':False}
        try:
            replies=list(responses[case['case_id']])
            async def model(request):
                if not replies:
                    raise RuntimeError('Replay exhausted recorded model responses')
                return httpx.Response(200,json={'choices':replies.pop(0)})
            agent=service()
            settings=agent.settings.model_copy(update={'intent_model_enabled':True,
                'multi_question_model_enabled':True,'intent_model_api_key':SecretStr('offline-replay')})
            router=CompoundQuestionRouter(settings,transport=httpx.MockTransport(model))
            outcome=await router.route_and_plan(case['question'],semantic_context=semantic_context,semantic_model_id=81)
            if outcome.plan is None:
                raise ValueError('No executable calculation plan')
            chat,_=context();chat.question=case['question']
            chat._planner_extraction=None
            query_count=0
            result=None
            conversations={task.task_id:task.task_id for task in outcome.plan.tasks}
            for task in outcome.plan.tasks:
                if database_query_choice(task.extraction) is False:
                    result=await agent._respond_pure_computation(chat,IDENTITY,task,conversations)
                    if result.status!='COMPLETED':
                        raise ValueError(result.answer)
                    continue
                query_count+=1
                # These are independent known fixtures, not values inferred
                # from the generated formula or its expected output label.
                if case['case_id'].endswith(':6'):
                    rows=[{'业务日期':'2025-01','含税销售总额':100,'订单笔数':1},
                          {'业务日期':'2025-02','含税销售总额':900,'订单笔数':3}]
                else:
                    rows=[{'含税销售总额':1000,'订单笔数':4,'销售总数量':20}]
                columns=[item['name'] if isinstance(item,dict) else item for item in task.extraction['指标']]
                if (task.extraction.get('时间粒度') or {}).get('unit')=='月':
                    columns=['业务日期',*columns]
                rows=[{column:row[column] for column in columns} for row in rows]
                reference=agent.dataset_store.save_dataset(scope=DatasetScope(IDENTITY.tenant_id,IDENTITY.user_id,
                    chat.application_id,task.task_id,chat.authorized_semantic_scope.fingerprint()),
                    columns=columns,rows=rows,snapshot_id='complex-calculation-fixture-v1',
                    data_as_of=datetime(2026,10,19,tzinfo=timezone.utc),source_type='DATABASE_QUERY',
                    source_ref='SYNTHETIC_FIXTURE_NOT_A_PRODUCTION_QUERY',semantic_model_id=81,
                    transformation_log=({'type':'query_provenance','source_truncated':False,
                        'source_quality_status':'PASS','source_total_row_count':len(rows)},))
                await agent.sessions.put_dataset_reference(reference.to_dict(),recent_limit=20)
            if result is None:
                raise ValueError('Raw inputs were returned without a final calculation')
            payload=result.evidence[0].payload
            operation=payload['operation']
            last=operation['operations'][-1] if operation.get('type')=='pipeline' else operation
            column=last['aggregations'][0]['alias'] if last['type']=='aggregate' else last['result_field']
            values=[row[column] for row in payload['preview']]
            expected=200 if case['case_id'].endswith(':6') else 50 if case['case_id'].endswith(':5') else 250
            if values!=[expected]:
                raise ValueError(f'Expected {[expected]}, got {values}')
            record.update(status='PASS_NUMERICAL_FIXTURE',expected=expected,actual=values,
                synthetic_query_inputs=query_count,operation=operation,
                dataset_id=result.dataset_id)
        except Exception as exc:
            record.update(error_type=type(exc).__name__,error=str(exc))
        results.append(record)
    report={'scope':identity['scope'],'catalog_version':identity['catalog_version'],
        'source_model_run':args.model_run.name,'fixture_id':'complex-calculation-fixture-v1',
        'reference_time':'2026-10-19 Asia/Shanghai','business_database_calls':0,'real_model_calls':0,
        'coverage_limit':'Numerical compilation/execution only; ASL catalog binding and production data remain unverified',
        'code_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'app/planning/task_dag.py','app/planning/calculation_bindings.py','app/services/computation_delivery.py')},
        'results':results}
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'cases':len(results),'numerical_pass':sum(item['status']=='PASS_NUMERICAL_FIXTURE' for item in results),
        'business_database_calls':0,'real_model_calls':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--model-run',type=Path,required=True)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--rules',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
