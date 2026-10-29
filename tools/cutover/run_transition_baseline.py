"""Run the actual V1 rule-service boundary sequentially with all sockets denied."""
from __future__ import annotations
import argparse,json,os,socket
from pathlib import Path
from unittest.mock import patch
from tools.cutover.semantic_evaluator import read_jsonl
from tools.cutover.transition_evaluator import evaluate_transitions,validate_transitions
from tools.cutover.transition_observations import observe_legacy_rules


def run(rows,catalog):
    validate_transitions(rows,catalog)
    def denied(*args,**kwargs):raise RuntimeError('OFFLINE_TRANSITION_NETWORK_DENIED')
    with patch.dict(os.environ,{'PYTHON_DOTENV_DISABLED':'1','LANGFUSE_TRACING_ENABLED':'false'}), patch.object(socket.socket,'connect',denied),patch.object(socket.socket,'connect_ex',denied),patch.object(socket,'create_connection',denied):
        predictions=[]
        for row in rows:
            try:predictions.append(observe_legacy_rules({k:v for k,v in row.items() if k not in {'labels','safety_checks'}}))
            except (ValueError,RuntimeError,KeyError) as exc:
                predictions.append({'case_id':row['case_id'],'scope':row['scope'],'catalog_ref':row['catalog_ref'],
                    'component':'V1_RULE_AND_DATASET_SERVICES','mode':'RULE_SERVICE_OFFLINE','status':'FAILED','error_type':type(exc).__name__})
    report=evaluate_transitions(rows,predictions,catalog,component='V1_RULE_AND_DATASET_SERVICES',mode='RULE_SERVICE_OFFLINE')
    report.update(real_model_calls=0,production_external_writes=0,network_policy='SOCKET_CONNECT_DENIED',clock_policy='PER_CASE_FIXED_CLASSIFIER_DATE')
    return predictions,report


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('gold','catalog','output-dir'):p.add_argument('--'+name,type=Path,required=True)
    a=p.parse_args();rows=read_jsonl(a.gold);catalog=json.loads(a.catalog.read_text(encoding='utf-8'))
    predictions,report=run(rows,catalog);a.output_dir.mkdir(parents=True,exist_ok=False)
    with (a.output_dir/'predictions.jsonl').open('w',encoding='utf-8') as f:
        for r in predictions:f.write(json.dumps(r,ensure_ascii=False)+'\n')
    (a.output_dir/'evaluation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='details'},ensure_ascii=False))
