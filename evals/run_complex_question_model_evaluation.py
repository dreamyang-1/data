"""Opt-in, isolated live planner evaluation against a frozen authorized catalog.

No SQL, business-data retrieval, production session/cache or catalog publication.
Model results are evidence to review, never automatic semantic Gold labels.
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'
CURRENT_CASE = ContextVar('complex_eval_case', default='')


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def text(value):
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':'))
            if not isinstance(value, str) else value).replace('|', '／').replace('\n', ' ')


def catalog_context(snapshot, rules):
    lines = ['评测时钟：2026-10-19，Asia/Shanghai。以下为冻结的当前授权目录元数据，不是业务查询结果。']
    for document in snapshot['documents']:
        lines += [f"业务域：{text(document.get('business_domain'))}",
                  '| 指标 | 同义词 | 单位 | 业务定义 | 计算规则 |', '| --- | --- | --- | --- | --- |']
        for metric in document.get('metrics', []):
            lines.append('| ' + ' | '.join(text(metric.get(key)) for key in (
                'metric_name','synonyms','unit','business_definition','calculation_rule')) + ' |')
        lines += ['| 实体 | 同义词 | 业务定义 | 属性 |', '| --- | --- | --- | --- |']
        for entity in document.get('entities', []):
            attributes = [{'name':a.get('attr_name'),'description':a.get('description')}
                          for a in entity.get('attributes', [])]
            lines.append('| ' + ' | '.join(text(value) for value in (
                entity.get('entity_name'),entity.get('entity_alias'),entity.get('business_definition'),attributes)) + ' |')
            if entity.get('relations'):
                lines.append('目录关系（' + text(entity.get('entity_name')) + '）：' + text(entity['relations']))
        lines += ['| 维度 | 同义词 | 业务定义 |', '| --- | --- | --- |']
        for dimension in document.get('dimensions', []):
            lines.append('| ' + ' | '.join(text(dimension.get(key)) for key in ('dim_name','synonyms','business_definition')) + ' |')
    from app.services.general_rules import render_rules
    return '\n'.join(lines) + render_rules(rules)


def cases_from_seed(seed, originals_only=False):
    cases=[]
    for family in seed['families']:
        variants=[('original',family['original'])]
        if not originals_only:
            variants += [('paraphrase',q) for q in family['candidate_paraphrases']]
            variants += [('contrast',c['text']) for c in family['contrast_cases']]
        for index,(kind,question) in enumerate(variants):
            cases.append({'case_id':f"{family['id']}:{index}",'family_id':family['id'],
                          'kind':kind,'question':question})
    return cases


async def run(args):
    if not args.live_model:
        raise SystemExit('Explicit --live-model is required; no evaluation was started')
    import httpx
    from app.config import Settings
    from app.planning.compound_question import CompoundQuestionRouter
    import app.domain.semantic_description as descriptions

    snapshot=json.loads(args.catalog.read_text(encoding='utf8'))
    material={key:value for key,value in snapshot.items() if key!='catalog_version'}
    canonical=json.dumps(material,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    if digest(canonical)!=snapshot['catalog_version']:
        raise SystemExit('Frozen catalog digest mismatch')
    if snapshot['scope']!={'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'}:
        raise SystemExit('This seed evaluation requires its explicit 81/[205] scope')
    platform=json.loads(args.rules.read_text(encoding='utf8'))
    context=catalog_context(snapshot,platform['rules'])
    seed=json.loads(args.seeds.read_text(encoding='utf8'))
    cases=cases_from_seed(seed,args.originals_only)
    if args.holdouts_only:
        if not args.holdouts:
            raise SystemExit('--holdouts-only requires --holdouts')
        cases=[]
    if args.holdouts:
        cases += [{**case,'case_id':f"{case['family_id']}:holdout",'kind':'holdout'}
                  for case in json.loads(args.holdouts.read_text(encoding='utf8'))['cases']]
    if args.family_id:
        cases=[case for case in cases if case['family_id'] in args.family_id]
        if not cases:
            raise SystemExit('No cases match the requested families')
    if args.case_id:
        cases=[case for case in cases if case['case_id'] in args.case_id]
        if not cases:
            raise SystemExit('No cases match the requested case IDs')
    if args.limit:
        cases=cases[:args.limit]
    settings=Settings().model_copy(update={'env':'test','multi_question_model_enabled':True})
    if not settings.intent_model_api_key or not settings.intent_model_api_key.get_secret_value():
        raise SystemExit('Configured model credential is unavailable')
    if args.output.exists():
        raise SystemExit('Refusing to overwrite evaluation evidence')
    args.output.mkdir(parents=True)
    identity={'scope':snapshot['scope'],'catalog_version':snapshot['catalog_version'],
        'context_sha256':digest(context),'rules_sha256':digest(args.rules.read_bytes()),
        'seed_sha256':digest(args.seeds.read_bytes()),'model':settings.intent_model_name,
        'holdouts_sha256':digest(args.holdouts.read_bytes()) if args.holdouts else None,
        'model_endpoint_hash':digest(settings.intent_model_base_url),'temperature':0,'enable_thinking':False,
        'reference_time':'2026-10-19 Asia/Shanghai','history':[],'pending':None,
        'data_watermark':'NO_BUSINESS_DATA_QUERIED; downstream deterministic fixtures are evaluated separately',
        'production_session_writes':0,'business_database_calls':0,'semantic_catalog_mutations':0,
        'code_hashes':{name:digest((ROOT/name).read_bytes()) for name in (
            'app/planning/compound_question.py','app/planning/task_dag.py','app/planning/calculation_bindings.py',
            'app/planning/structured_extraction_prompt.txt','app/services/orchestrator.py',
            'app/services/computation_delivery.py','app/services/period_computation.py','app/services/query_output_bindings.py',
            'minio_followup_store.py','evals/run_complex_question_model_evaluation.py')}}
    (args.output/'identity.json').write_text(json.dumps(identity,ensure_ascii=False,indent=2),encoding='utf8')
    (args.output/'frozen-inputs.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2),encoding='utf8')
    # Every planner use sees this same frozen availability decision. No live
    # metadata refresh or business access may happen inside a case.
    async def frozen_description(*a,**kw):
        return {'available':False,'source':'frozen-authorized-catalog','content':''}
    descriptions.load_semantic_description=frozen_description
    # Deterministic relative-month compilation must use the same frozen clock
    # as the context supplied to the model.
    import app.intent.classifier as classifier
    from datetime import date
    class EvaluationDate(date):
        @classmethod
        def today(cls):
            return cls(2026,10,19)
    classifier.date=EvaluationDate
    expected=httpx.URL(settings.intent_model_base_url)
    calls=[]
    class ModelOnlyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self,request):
            if (request.url.scheme,request.url.host,request.url.port)!=(expected.scheme,expected.host,expected.port):
                raise RuntimeError('Evaluation forbids non-model HTTP calls')
            started=time.monotonic()
            async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
                response=await transport.handle_async_request(request)
                body=await response.aread()
            calls.append({'case_id':CURRENT_CASE.get(),'status':response.status_code,
                'request_sha256':digest(request.content),'response_sha256':digest(body),
                'elapsed_seconds':round(time.monotonic()-started,3)})
            # Keep replayable model output in the private run folder; never
            # persist request headers, credentials or business query results.
            if response.status_code==200:
                payload=json.loads(body)
                record={'case_id':CURRENT_CASE.get(),'request_sha256':digest(request.content),
                        'choices':payload.get('choices',[])}
                target=args.output/f'model-response-{len(calls):04d}.json'
                target.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
            # aread() already decoded content-encoding; the replacement response
            # must not decompress those bytes a second time.
            headers=[(key,value) for key,value in response.headers.raw
                     if key.lower() not in {b'content-encoding',b'content-length',b'transfer-encoding'}]
            return httpx.Response(response.status_code,headers=headers,content=body,request=request)
    semaphore=asyncio.Semaphore(args.concurrency)
    async def evaluate(case):
        async with semaphore:
            token=CURRENT_CASE.set(case['case_id']);started=time.monotonic()
            result={**case,'evaluation_status':'UNREVIEWED'}
            try:
                router=CompoundQuestionRouter(settings,transport=ModelOnlyTransport())
                outcome=await router.route_and_plan(case['question'],semantic_context=context,semantic_model_id=81)
                result['judgment']=asdict(outcome.judgment)
                result['plan']=outcome.plan.model_dump(mode='json') if outcome.plan else None
                single=outcome.single
                result['single']={'intent':str(single.single_intent),
                    'question':single.single_task_question,
                    'extraction':single.single_extraction.model_dump(mode='json') if single.single_extraction else None} if single else None
            except Exception as exc:
                result.update(evaluation_status='EXECUTION_ERROR',error_type=type(exc).__name__)
                from app.planning.task_dag import TaskPlanningError
                if isinstance(exc,TaskPlanningError):
                    result['planning_error']=str(exc)
                    result['evaluation_status']='PLANNING_BLOCKED'
            finally:
                CURRENT_CASE.reset(token)
            result['elapsed_seconds']=round(time.monotonic()-started,3)
            result['model_calls']=[call for call in calls if call['case_id']==case['case_id']]
            target=args.output/(digest(case['case_id'])[:16]+'.json')
            target.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf8')
            print(json.dumps({'case_id':case['case_id'],'state':result['evaluation_status'],
                              'seconds':result['elapsed_seconds']},ensure_ascii=False),flush=True)
            return result
    results=await asyncio.gather(*(evaluate(case) for case in cases))
    (args.output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf8')
    (args.output/'calls.json').write_text(json.dumps(calls,indent=2),encoding='utf8')
    print(json.dumps({'cases':len(results),'model_calls':len(calls),'business_database_calls':0}),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--live-model',action='store_true')
    p.add_argument('--catalog',type=Path,required=True)
    p.add_argument('--rules',type=Path,required=True)
    p.add_argument('--seeds',type=Path,default=ROOT/'evals/complex_question_semantic_families_20261019.json')
    p.add_argument('--holdouts',type=Path)
    p.add_argument('--family-id',action='append',default=[])
    p.add_argument('--case-id',action='append',default=[])
    p.add_argument('--holdouts-only',action='store_true')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--originals-only',action='store_true')
    p.add_argument('--limit',type=int)
    p.add_argument('--concurrency',type=int,choices=range(1,5),default=2)
    asyncio.run(run(p.parse_args()))
