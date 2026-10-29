"""Build a reviewed 100-case axis corpus from an actual sealed catalog capture.

Only a minimal semantic fact projection is exported. Credentials, locators,
source business rows and the full private snapshot are never copied to Git.
The listed labels are engineer-reviewed; no row is auto-labeled COMPLETE.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from tools.cutover.evaluation_contract import digest
from tools.cutover.semantic_evaluator import validate_gold


def freeze(snapshot, raw_hash, observed_at):
    if snapshot.get('contract_version') != 'catalog-release-v1' or snapshot['catalog_version'] != digest({k:v for k,v in snapshot.items() if k!='catalog_version'}):
        raise ValueError('CAPTURE_DIGEST_INVALID')
    scope=snapshot['scope']
    if scope != {'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'}:
        raise ValueError('REVIEWED_SCOPE_REQUIRED')
    facts=[]
    for doc in snapshot['documents']:
        if doc['semantic_model']['id']!=81 or doc['business_domain']['id']!=205:
            raise ValueError('CAPTURE_OWNER_MISMATCH')
        for e in doc['entities']:
            facts.append(dict(fact_id='ENTITY:'+e['entity_code'],catalog_type='ENTITY',code=e['entity_code'],name=e['entity_name']))
            for a in e['attributes']:
                facts.append(dict(fact_id='ATTRIBUTE:'+e['entity_code']+'.'+a['attr_code'],catalog_type='ATTRIBUTE',
                    code=a['attr_code'],name=a['attr_name'],owner=e['entity_code']))
        for m in doc['metrics']:
            facts.append(dict(fact_id='METRIC:'+m['metric_code'],catalog_type='METRIC',code=m['metric_code'],name=m['metric_name']))
        for d in doc['dimensions']:
            facts.append(dict(fact_id='DIMENSION:'+d['dim_code'],catalog_type='DIMENSION',code=d['dim_code'],name=d['dim_name']))
    if len({f['fact_id'] for f in facts})!=len(facts):raise ValueError('AMBIGUOUS_FACT_ID')
    artifact=dict(contract='frozen-evaluation-catalog-v1',scope=scope,catalog_version=snapshot['catalog_version'],
        source_snapshot_sha256=raw_hash,source_identity_hash=snapshot['source_identity_hash'],observed_at=observed_at,
        certification='VERIFIED_SCOPED_CAPTURE_FOR_OFFLINE_EVALUATION',
        certification_checks=['capture digest','exact 81/205 ownership','unique fact IDs','projection from authoritative scoped snapshot'],
        certification_limits=['not native publication','not business identity completeness','not full SQL/result gold'],facts=facts)
    return {**artifact,'artifact_hash':digest(artifact)}


def build(catalog):
    facts=catalog['facts']; by_id={f['fact_id']:f for f in facts}; rows=[]
    def mention(text,surface,roles):
        start=text.index(surface)
        return dict(surface=surface,start=start,end=start+len(surface),roles=roles)
    def add(text,labels,refs,history=(),source='FROZEN_CATALOG_BUSINESS_TEMPLATE',family='MENTION_ROLE'):
        rows.append(dict(case_id=f'G81-{len(rows)+1:03}',family=family,source=source,
            history=list(history),current_utterance=text,scope=catalog['scope'],catalog_ref=catalog['artifact_hash'],
            clock='2026-09-09T09:00:00+08:00',labels=labels,catalog_evidence=refs,
            label_status='REVIEWED_FOR_LISTED_AXES',review_method='ENGINEER_REVIEW_AGAINST_CATALOG_AND_EXPLICIT_USER_CONTRACT',
            missing_labels=['SQL','source_result','canonical_binding','complete_task_state'],
            strict_identity_required_for_labeled_axes=False))
    for f in [f for f in facts if f['catalog_type']=='ENTITY']:
        surface='城市' if f['code']=='city' else f['name']
        text='列出'+surface+('记录' if f['code'] in {'sales_order','product_dept_relation'} else '名单')
        add(text,{'query_shape':'DETAIL_ROWS','turn_relation':'NEW_TASK',
            'mentions':[mention(text,surface,['SUBJECT_ENTITY','TARGET_ENTITY'])]},[f['fact_id']])
    for f in [f for f in facts if f['catalog_type']=='METRIC']:
        for prefix in ('查询','请告诉我'):
            text=prefix+f['name']
            add(text,{'query_shape':'SCALAR_AGGREGATE','turn_relation':'NEW_TASK',
                'mentions':[mention(text,f['name'],['MEASURE'])]},[f['fact_id']])
    metric=by_id['METRIC:order_count']
    for f in [f for f in facts if f['catalog_type']=='DIMENSION']:
        text='按'+f['name']+'统计订单笔数'
        labels={'turn_relation':'NEW_TASK','mentions':[mention(text,f['name'],
            ['GROUP_BY','TIME_FIELD'] if f['name']=='交易日期' else ['GROUP_BY']),mention(text,'订单笔数',['MEASURE'])]}
        if f['name']!='交易日期':labels['query_shape']='GROUPED_AGGREGATE'
        add(text,labels,[f['fact_id'],metric['fact_id']])
    projection_ids=['hospital.hospital_name','hospital.hospital_id','hospital.hospital_code',
        'product.product_name','product.product_code','product.specification','salesperson.salesperson_name',
        'salesperson.salesperson_code','project.project_name','project.project_code','sales_company.guoyao_name',
        'sales_company.guoyao_code','department.dept_name','department.dept_code','dealer.dealer_name',
        'dealer.dealer_code','city.city_name','province.province_name']
    for key in projection_ids:
        f=by_id['ATTRIBUTE:'+key];text='结果中只返回'+f['name']
        add(text,{'mentions':[mention(text,f['name'],['PROJECTION_FIELD'])]},[f['fact_id']],
            ['列出'+by_id['ENTITY:'+f['owner']]['name']+'名单'])
    # Current user requirements, independent of generated parser predictions.
    turns=[
      ('上海最近一年销售额','那江苏呢？','FOLLOW_UP',None),
      ('上海最近一年销售额','江苏有哪些医院？','NEW_TASK',None),
      ('销售额和销售量','再加订单笔数','FOLLOW_UP','ADD'),
      ('上海销售额','换成江苏','FOLLOW_UP','REPLACE'),
      ('销售额和订单笔数','不要订单笔数','FOLLOW_UP','REMOVE'),
      ('上海销售额','不限地区','FOLLOW_UP','CLEAR'),
      ('上海销售额；不限地区','按季度','FOLLOW_UP',None),
      ('待选：销售额还是销售量','江苏有哪些医院？','NEW_TASK',None),
      ('江苏销售额','回到刚才上海那个问题','RETURN_TO_TOPIC',None),
      ('按医院统计销售额','再加销售总成本','FOLLOW_UP','ADD'),
      ('按医院统计销售额','把指标换成订单笔数','FOLLOW_UP','REPLACE'),
      ('含税销售总额和销售总成本','去掉销售总成本','FOLLOW_UP','REMOVE'),
      ('只看上海的医院','取消地区限制','FOLLOW_UP','CLEAR'),
      ('销售额按业务员分组','增加按销售公司分组','FOLLOW_UP','ADD'),
      ('销售额按医院分组','把医院换成经销商','FOLLOW_UP','REPLACE'),
      ('销售额按城市和业务员分组','不要按业务员分组','FOLLOW_UP','REMOVE'),
      ('最近一年订单笔数','取消时间限制','FOLLOW_UP','CLEAR'),
      ('列出医院名单','只看前5条','FOLLOW_UP',None),
      ('列出商品名单','现在查询销售总成本','NEW_TASK',None),
      ('上海销售额','医院名称字段来自哪个表？','NEW_TASK',None),
      ('查询订单笔数','返回之前的医院名单','RETURN_TO_TOPIC',None),
      ('含税销售总额','改成不含税销售总额','FOLLOW_UP','REPLACE'),
      ('按季度看订单笔数','继续看下一页','FOLLOW_UP',None),
    ]
    for old,text,relation,operation in turns:
        labels={'turn_relation':relation}
        if operation:labels['operation']=operation
        add(text,labels,['ENTITY:hospital','METRIC:order_count'],[old],
            source='USER_CRITICAL_MULTI_TURN_CONTRACT_AND_CONTRAST',family='TURN_OPERATION')
    contrasts=[
      ('只看前5条','DATASET_TRANSFORM',[]),
      ('订单笔数最高的5家医院','RANKING',[('订单笔数',['MEASURE','ORDER_BY'])]),
      ('医院名称字段来自哪个表？','LINEAGE_GRAPH',[]),
      ('列出TDC-3合作医院',None,[('医院',['TARGET_ENTITY','RELATION_TARGET'])]),
      ('按商品名称统计销售总数量','GROUPED_AGGREGATE',[('商品名称',['GROUP_BY']),('销售总数量',['MEASURE'])]),
      ('返回商品名称',None,[('商品名称',['PROJECTION_FIELD'])]),
      ('统计销售数量','SCALAR_AGGREGATE',[('销售数量',['MEASURE'])]),
      ('列出订单中的销售数量字段','DETAIL_ROWS',[('销售数量',['PROJECTION_FIELD'])]),
      ('按月查看销售总成本走势','TIME_SERIES',[('销售总成本',['MEASURE'])]),
      ('2026年1月的订单笔数是多少','SCALAR_AGGREGATE',[('订单笔数',['MEASURE'])]),
    ]
    for text,shape,mentions in contrasts:
        labels={}
        if shape:labels['query_shape']=shape
        if mentions:labels['mentions']=[mention(text,s,r) for s,r in mentions]
        add(text,labels,['METRIC:order_count','METRIC:sales_total_quantity','ATTRIBUTE:product.product_name','ATTRIBUTE:hospital.hospital_name'],
            ['当前已有医院名单'] if text=='只看前5条' else (),source='USER_CRITICAL_CONTRAST_AND_CATALOG',family='CONTRAST')
    if len(rows)!=100:raise ValueError(f'EXPECTED_100_CASES_GOT_{len(rows)}')
    validate_gold(rows,catalog)
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--snapshot',required=True,type=Path)
    parser.add_argument('--receipt',required=True,type=Path)
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args();raw=args.snapshot.read_bytes();snap=json.loads(raw)
    receipt=json.loads(args.receipt.read_text(encoding='utf-8'))
    if receipt['catalog_version']!=snap['catalog_version'] or receipt['scope']!=snap['scope']:
        raise SystemExit('CAPTURE_RECEIPT_MISMATCH')
    catalog=freeze(snap,hashlib.sha256(raw).hexdigest(),receipt['observed_at'])
    rows=build(catalog);args.output_dir.mkdir(parents=True,exist_ok=True)
    for name,value in [('frozen_catalog.json',catalog),('gold_axes.jsonl',rows)]:
        target=args.output_dir/name
        if target.exists():raise SystemExit('Refusing to overwrite frozen gold')
        target.write_text((''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in value) if name.endswith('jsonl')
            else json.dumps(value,ensure_ascii=False,indent=2)+'\n'),encoding='utf-8')
    print(json.dumps({'cases':len(rows),'facts':len(catalog['facts']),'catalog_ref':catalog['artifact_hash']}))
