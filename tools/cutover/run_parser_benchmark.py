"""Opt-in real-model benchmark of the existing V2 current-turn parser.

This is component evidence, not a full V1/V2 or SQL-result benchmark. No catalog,
Redis, SQL, result store, production orchestrator or tracing client is called.
Only curated current utterances, existing schema/prompt and fixed clock are sent
to the already configured intent endpoint. Gold labels/history stay local.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import time
from pathlib import Path
from datetime import datetime, timezone
import httpx
from app.config import Settings
from app.semantic_v2.recognition import PARSE_PROMPT, PROMPT_VERSION, EDIT_SLOTS, current_turn_schema
from app.semantic_v2.recognition_client import RecognitionModelClient, RecognitionFailure
from app.semantic_v2.pipeline import CurrentTurnSemanticParse, CurrentTurnParser
from app.semantic_v2.recognition_repairs import repair_model_parse
from tools.cutover.evaluation_contract import digest
from tools.cutover.semantic_evaluator import read_jsonl, validate_gold, evaluate


def normalize(parsed):
    if parsed.topic_shift_signals:
        relation='NEW_TASK'
    elif 'HISTORICAL' in parsed.reference_signals:
        relation='RETURN_TO_TOPIC'
    elif parsed.reference_signals or parsed.followup_signals:
        relation='FOLLOW_UP'
    else:
        relation='NEW_TASK'
    operations=sorted({m.operation_hint for m in parsed.operation_markers if m.operation_hint in {'ADD','REPLACE','REMOVE','CLEAR'}})
    return {'turn_relation':relation,'query_shape':parsed.query_shape_prediction.value if parsed.query_shape_prediction else None,
        'operation':operations[0] if len(operations)==1 else None,
        'mentions':[{'start':m.start_char,'end':m.end_char,'roles':[r.value for r in m.candidate_roles]} for m in parsed.mentions]}


async def run(args):
    catalog=json.loads(args.catalog.read_text(encoding='utf-8'));rows=read_jsonl(args.gold)
    validate_gold(rows,catalog)
    selected=rows if args.limit is None else rows[:args.limit]
    if args.case_ids:
        wanted=set(args.case_ids.split(','))
        selected=[r for r in rows if r['case_id'] in wanted]
        if {r['case_id'] for r in selected}!=wanted:raise ValueError('UNKNOWN_CASE_ID')
    base=Settings()
    settings=base.model_copy(update={'intent_model_name':args.model,
        'intent_model_enable_thinking':args.thinking,'intent_model_max_retries':0})
    if not settings.intent_model_api_key:raise ValueError('MODEL_NOT_CONFIGURED')
    args.output_dir.mkdir(parents=True,exist_ok=False)
    requests=[]; predictions=[]; usage=[]; sem=asyncio.Semaphore(args.concurrency)
    # Capture only status, usage and returned model ID. Never persist headers,
    # raw response bodies, chain of thought or provider error messages.
    upstream=httpx.AsyncHTTPTransport(retries=0)
    class ReceiptTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self,request):
            body=json.loads(request.content)
            requests.append({'input_hash':digest(body['messages']),'model':body['model'],
                'thinking':body['enable_thinking'],'schema_transport':body['response_format']['type']})
            response=await upstream.handle_async_request(request)
            await response.aread()
            try:
                data=response.json()
                usage.append({'status':response.status_code,'model':data.get('model'),
                    'usage':data.get('usage'),'provider_code':(data.get('error') or {}).get('code')})
            except (ValueError,TypeError):usage.append({'status':response.status_code})
            return response
        async def aclose(self):
            pass
    transport=ReceiptTransport();model=RecognitionModelClient(settings,transport=transport)
    async def one(row):
        async with sem:
            start=time.monotonic()
            out={'case_id':row['case_id'],'scope':catalog['scope'],'catalog_ref':catalog['artifact_hash']}
            parsed=None
            try:
                parsed=await model.complete(stage='v2_current_turn',instruction=PARSE_PROMPT,
                    context={'question':row['current_utterance'],'turn_id':row['case_id'],
                        'clock':row['clock'],'slots':list(EDIT_SLOTS)},output_model=CurrentTurnSemanticParse,
                    schema=current_turn_schema())
                parsed, repairs=repair_model_parse(parsed,text=row['current_utterance'],turn_id=row['case_id'])
                out['input_repairs']=repairs
                checked=CurrentTurnParser.parse(text=row['current_utterance'],turn_id=row['case_id'],text_ref=row['case_id'],parsed=parsed)
                out.update(status='OK',prediction=normalize(checked))
            except (RecognitionFailure,ValueError) as exc:
                out.update(status='FAILED',error_type=type(exc).__name__,
                    reason=str(exc) if isinstance(exc,RecognitionFailure) else 'CURRENT_TURN_VALIDATION_FAILED')
                if parsed is not None:
                    ids={m.mention_id for m in parsed.mentions}
                    refs=[*parsed.negations,*parsed.temporal_expressions,
                        *(i for group in parsed.coordination_groups for i in group),
                        *(i for group in parsed.explicit_slot_mentions.values() for i in group),
                        *(m.mention_id for m in parsed.operation_markers)]
                    out['diagnostics']={'invalid_surface_spans':sum(row['current_utterance'][m.start_char:m.end_char]!=m.surface for m in parsed.mentions),
                        'foreign_turn_mentions':sum(m.source_turn_id!=row['case_id'] for m in parsed.mentions),
                        'duplicate_mention_ids':len(parsed.mentions)-len(ids),
                        'missing_mention_references':len(set(refs)-ids)}
            out['latency_seconds']=round(time.monotonic()-start,3);predictions.append(out)
            with (args.output_dir/'predictions.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps(out,ensure_ascii=False)+'\n')
            if len(predictions)%10==0:print(json.dumps({'completed':len(predictions),'total':len(selected)}),flush=True)
    try:
        await asyncio.gather(*(one(row) for row in selected))
    finally:
        await upstream.aclose()
    report=evaluate(selected,predictions,catalog)
    report.update({'component':'EXISTING_V2_CURRENT_TURN_PARSER','model':args.model,'thinking':args.thinking,
        'observed_at':datetime.now(timezone.utc).isoformat(),'prompt_version':PROMPT_VERSION,
        'prompt_hash':digest(PARSE_PROMPT),'schema_hash':digest(current_turn_schema()),
        'gold_hash':digest(rows),'requests':requests,'request_count':len(requests),'response_usage':usage,
        'temperature':0,'retries':0,'timeout_seconds':settings.intent_model_timeout_seconds,
        'production_default_model_changed':False,'catalog_usage':'FROZEN_LABEL_EVIDENCE_ONLY_NO_RETRIEVAL_IN_CURRENT_TURN_STAGE',
        'history_usage':'CURRENT_TURN_STAGE_DOES_NOT_RECEIVE_HISTORY','sql_executions':0,'production_state_writes':0,
        'latency_seconds':{'mean':sum(p['latency_seconds'] for p in predictions)/len(predictions),
            'p95':sorted(p['latency_seconds'] for p in predictions)[min(len(predictions)-1,int(len(predictions)*.95))]}})
    (args.output_dir/'evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in {'details','requests','response_usage'}},ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('gold','catalog','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--model',required=True);p.add_argument('--thinking',action='store_true')
    p.add_argument('--concurrency',type=int,choices=range(1,5),default=2)
    p.add_argument('--limit',type=int)
    p.add_argument('--case-ids')
    p.add_argument('--allow-model-calls',action='store_true',required=True)
    a=p.parse_args()
    if a.limit is not None and a.limit<=0:p.error('limit must be positive')
    asyncio.run(run(a))
