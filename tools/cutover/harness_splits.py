"""Create sequestered contract-derived validation data, never prompt rules.

The generator is reviewable. Concrete holdout questions are written privately,
not returned, printed or run. This provides procedural separation; it does not
claim independent human adjudication or a representative production sample.
"""
from __future__ import annotations
from datetime import datetime,timezone
import json
from pathlib import Path
import random
import secrets

from tools.cutover.evaluation_contract import digest
from tools.cutover.harness_corpus import enrich


def create_splits(catalog,public_rows,directory,*,per_split=24):
    if type(per_split) is not int or not 1<=per_split<=30:
        raise ValueError('SPLIT_SIZE_OUT_OF_BOUNDS')
    if catalog.get('artifact_hash')!=digest({k:v for k,v in catalog.items() if k!='artifact_hash'}):
        raise ValueError('SPLIT_CATALOG_HASH_INVALID')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    rng=random.Random(secrets.randbits(256))
    metrics=[f for f in catalog['facts'] if f['catalog_type']=='METRIC']
    if len(metrics)<3:raise ValueError('THREE_GOVERNED_METRICS_REQUIRED')
    if (per_split+2)//3>len(metrics):raise ValueError('INSUFFICIENT_DISTINCT_SINGLE_TURN_COMPOSITIONS')
    exposed={digest([r.get('history',[]),r['current_utterance']]) for r in public_rows}
    manifest={'version':'sequestered-contract-splits-v1','created_at':datetime.now(timezone.utc).isoformat(),
        'catalog_hash':catalog['artifact_hash'],'catalog_version':catalog['catalog_version'],
        'scope':catalog['scope'],'label_version':'explicit-metric-operation-contract-v1',
        'label_source':['BUSINESS_CONTRACT','CATALOG_PROVEN','DETERMINISTIC_RULE'],
        'creation_method':'PROGRAMMATIC_COMPOSITION_AFTER_PROMPT_FREEZE',
        'independent_human_adjudication':False,'representativeness':'EVIDENCE_INSUFFICIENT','splits':{}}
    for split,prefix in [('PRIVATE_VALIDATION','PV81'),('BLIND_HOLDOUT','BH81')]:
        rows=[];seen=set();attempts=0
        while len(rows)<per_split:
            attempts+=1
            if attempts>10000:raise ValueError('DISTINCT_SPLIT_GENERATION_EXHAUSTED')
            a,b,c=rng.sample(metrics,3);kind=len(rows)%3
            if kind==0:
                history=[]
                question=(f"请给出{a['name']}的汇总值。" if split=='PRIVATE_VALIDATION' else
                          f"请提供{a['name']}这一指标的整体汇总。")
                expected=[a['fact_id']]
            elif kind==1:
                history=[f"请同时统计{a['name']}和{b['name']}。"]
                question=f"已有指标都保留，另外增加{c['name']}。";expected=[a['fact_id'],b['fact_id'],c['fact_id']]
            else:
                history=[f"请同时统计{a['name']}和{b['name']}。"]
                question=f"删除{b['name']}，另一个指标继续保留。";expected=[a['fact_id']]
            fingerprint=digest([history,question])
            if fingerprint in exposed|seen:continue
            seen.add(fingerprint)
            labels={'canonical_metrics':expected,'turn_relation':'NEW_TASK' if kind==0 else 'FOLLOW_UP'}
            row={'case_id':f'{prefix}-{len(rows)+1:03}','history':history,'current_utterance':question,
                'scope':catalog['scope'],'catalog_ref':catalog['artifact_hash'],'clock':'2026-09-09T09:00:00+08:00',
                'labels':labels,'catalog_evidence':[a['fact_id'],b['fact_id'],c['fact_id']],
                'business_evidence':['EXPLICIT_ADD_PRESERVES_PREVIOUS','EXPLICIT_REMOVE_ONLY_TARGET'],
                'label_status':'REVIEWED_FOR_LISTED_AXES','safety_checks':[],
                'missing_labels':['complete_plan','SQL','source_result'],'initial_pending':False,'dataset':None}
            rows.append(row)
        enriched=enrich(rows,corpus=split,split=split)
        filename=split.lower()+'.jsonl'
        path=directory/filename
        with path.open('x',encoding='utf-8') as stream:
            for row in enriched:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        manifest['splits'][split]={'case_count':len(rows),'case_ids_hash':digest([r['case_id'] for r in rows]),
            'manifest_hash':digest(enriched),'file_sha256':__import__('hashlib').sha256(path.read_bytes()).hexdigest(),
            'public_overlap':len(seen&exposed),'access_status':'SEALED_NOT_VIEWED_OR_RUN',
            'original_utterances_published':False,'prompt_or_rule_training_use':False}
        exposed|=seen
    manifest['holdout_manifest_hash']=manifest['splits']['BLIND_HOLDOUT']['manifest_hash']
    manifest['manifest_hash']=digest(manifest)
    (directory/'split_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return manifest


def authorize_holdout(manifest,freeze):
    if freeze.get('status')!='SEMANTIC_FREEZE_CANDIDATE' or not freeze.get('hard_safety_pass'):
        raise ValueError('HOLDOUT_LOCKED_UNTIL_SEMANTIC_FREEZE_CANDIDATE')
    if freeze.get('holdout_manifest_hash')!=manifest['holdout_manifest_hash']:
        raise ValueError('HOLDOUT_MANIFEST_MISMATCH')
    if manifest['splits']['BLIND_HOLDOUT']['access_status']!='SEALED_NOT_VIEWED_OR_RUN':
        raise ValueError('EXPOSED_HOLDOUT_MUST_BE_PROMOTED_TO_DEV')


def promote_exposed_case(manifest,case_id_hash):
    updated=json.loads(json.dumps(manifest))
    updated['splits']['BLIND_HOLDOUT']['access_status']='PROMOTED_TO_DEV'
    updated.setdefault('exposure_log',[]).append({'case_id_hash':case_id_hash,'status':'PROMOTED_TO_DEV'})
    updated.pop('manifest_hash',None);updated['manifest_hash']=digest(updated)
    return updated
