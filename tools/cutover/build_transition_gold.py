"""Twenty explicit multi-turn state labels; no model-generated ground truth."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from tools.cutover.transition_evaluator import validate_transitions


def build(catalog):
    amount='含税销售总额';quantity='销售总数量';orders='订单笔数';cost='销售总成本'
    cases=[
      ([amount+'和'+quantity],'再加订单笔数',{'metric_surfaces':[amount,quantity,orders],'canonical_metrics':['METRIC:sales_total_including_tax','METRIC:sales_total_quantity','METRIC:order_count'],'turn_relation':'FOLLOW_UP','target_task':'PREVIOUS','slot_operations':['ADD']},['METRIC:order_count'],['wrong_inheritance']),
      ([amount+'和'+quantity],'把指标换成订单笔数',{'metric_surfaces':[orders],'turn_relation':'FOLLOW_UP','target_task':'PREVIOUS','slot_operations':['REPLACE']},['METRIC:order_count'],['wrong_inheritance']),
      ([amount+'和'+orders],'不要订单笔数',{'metric_surfaces':[amount],'turn_relation':'FOLLOW_UP','target_task':'PREVIOUS','slot_operations':['REMOVE']},['METRIC:sales_total_including_tax','METRIC:order_count'],['wrong_inheritance']),
      (['上海最近一年'+amount,'不限地区'],'按季度',{'metric_surfaces':[amount],'region_values':[],'time_relation':'SAME_AS_PREVIOUS','time_grain':'QUARTER','target_task':'PREVIOUS'},['METRIC:sales_total_including_tax'],['wrong_inheritance']),
      (['上海最近一年'+amount],'那江苏呢？',{'metric_surfaces':[amount],'region_values':['江苏省'],'time_relation':'SAME_AS_PREVIOUS','turn_relation':'FOLLOW_UP','target_task':'PREVIOUS'},['METRIC:sales_total_including_tax'],['wrong_inheritance']),
      (['上海'+amount],'换成江苏',{'metric_surfaces':[amount],'region_values':['江苏省'],'slot_operations':['REPLACE'],'target_task':'PREVIOUS'},['METRIC:sales_total_including_tax'],['wrong_inheritance']),
      (['上海和北京'+amount],'地区再加江苏',{'metric_surfaces':[amount],'region_values':['上海市','北京市','江苏省'],'slot_operations':['ADD'],'target_task':'PREVIOUS'},['METRIC:sales_total_including_tax'],['wrong_inheritance']),
      (['上海和北京'+amount],'不要北京',{'metric_surfaces':[amount],'region_values':['上海市'],'slot_operations':['REMOVE'],'target_task':'PREVIOUS'},['METRIC:sales_total_including_tax'],['wrong_inheritance']),
      (['上海最近一年'+amount],'江苏有哪些医院？',{'metric_surfaces':[],'region_values':['江苏省'],'turn_relation':'NEW_TASK','target_task':'NEW'},['ENTITY:hospital'],['wrong_inheritance']),
      (['上海最近一年'+amount],'切换话题：查询订单笔数',{'metric_surfaces':[orders],'region_values':[],'turn_relation':'NEW_TASK','target_task':'NEW'},['METRIC:order_count'],['wrong_inheritance']),
      (['按医院统计'+amount],'增加按城市分组',{'metric_surfaces':[amount],'dimension_surfaces':['医院','城市'],'slot_operations':['ADD'],'target_task':'PREVIOUS'},['DIMENSION:hospital','DIMENSION:city'],['wrong_inheritance']),
      (['按医院统计'+amount],'把分组换成经销商',{'metric_surfaces':[amount],'dimension_surfaces':['经销商'],'slot_operations':['REPLACE'],'target_task':'PREVIOUS'},['DIMENSION:dealer'],['wrong_inheritance']),
      (['按城市和业务员统计'+amount],'不要按业务员分组',{'metric_surfaces':[amount],'dimension_surfaces':['城市'],'slot_operations':['REMOVE'],'target_task':'PREVIOUS'},['DIMENSION:city','DIMENSION:salesperson'],['wrong_inheritance']),
      (['2025年'+orders,'取消时间限制'],'再加销售总数量',{'metric_surfaces':[orders,quantity],'time_relation':'ABSENT','target_task':'PREVIOUS'},['METRIC:order_count','METRIC:sales_total_quantity'],['wrong_inheritance']),
      (['上海'+amount],'医院名称字段来自哪个表？',{'turn_relation':'NEW_TASK','target_task':'NEW','metric_clarification_required':False},['ATTRIBUTE:hospital.hospital_name'],['wrong_inheritance']),
      (['上海'+amount],'江苏有哪些医院？',{'turn_relation':'NEW_TASK','target_task':'NEW','metric_surfaces':[],'pending_action':'DETACHED'},['ENTITY:hospital'],['pending_hijack','wrong_inheritance']),
      ([amount,cost],'回到含税销售总额，再加订单笔数',{'turn_relation':'RETURN_TO_TOPIC','target_task':'HISTORY:0','metric_surfaces':[amount,orders]},['METRIC:sales_total_including_tax','METRIC:order_count'],['wrong_inheritance']),
      (['已有医院名单'],'只看前5条',{'dataset_route':'DISPLAY_LIMIT'},['ENTITY:hospital'],['unsafe_truncated_ranking']),
      (['已有按医院统计的含税销售总额'],'含税销售总额最高5名',{'dataset_route':'GLOBAL_RANK_REPLAN_OR_REJECT'},['METRIC:sales_total_including_tax','ENTITY:hospital'],['unsafe_truncated_ranking']),
      (['已有按医院统计的含税销售总额'],'含税销售总额最高5名',{'dataset_route':'LOCAL_RANK_COMPLETE_DATASET'},['METRIC:sales_total_including_tax','ENTITY:hospital'],['unsafe_truncated_ranking']),
    ]
    rows=[]
    for i,(history,text,labels,refs,safety) in enumerate(cases,1):
        # An operation is labeled for its affected slot, not every incidental
        # inferred slot. Full TaskPatch equivalence remains a separate axis.
        if 'slot_operations' in labels:
            axis='metric_operations' if i<=3 else 'dimension_operations' if i in (11,12,13) else 'region_operations'
            labels[axis]=labels.pop('slot_operations')
        refs=sorted(set(refs+labels.get('canonical_metrics',[])+labels.get('canonical_dimensions',[])))
        row={'case_id':f'S81-{i:03}','history':history,'current_utterance':text,
          'scope':catalog['scope'],'catalog_ref':catalog['artifact_hash'],'clock':'2026-09-09T09:00:00+08:00',
          'labels':labels,'safety_checks':safety,'catalog_evidence':refs,
          'business_evidence':['USER_PHASE0B_CRITICAL_MULTITURN_1_TO_15','USER_PHASE0C_SCOPE_CONTRACT','USER_CUTOVER_EVALUATOR_AND_ZERO_TOLERANCE_GATES'],
          'label_status':'REVIEWED_FOR_LISTED_AXES','review_method':'ENGINEER_REVIEW_OF_EXPLICIT_OPERATIONS_AND_FIXED_CATALOG_NAMES_BEFORE_OBSERVATION',
          'missing_labels':['complete_semantic_plan','source_SQL_result','scope_reuse_runtime','full_clarification_trace'],
          'initial_pending':i==16,'dataset':None}
        if i>=18:
            row['dataset']={'source_complete':i==20,'columns':['医院名称',amount],
              'rows':[{'医院名称':'验收机构甲',amount:2},{'医院名称':'验收机构乙',amount:9}],
              'data_origin':'SYNTHETIC_EXPLICIT_FIXTURE_NOT_SOURCE_BUSINESS_ROWS'}
        rows.append(row)
    validate_transitions(rows,catalog)
    return rows


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--catalog',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    rows=build(json.loads(a.catalog.read_text(encoding='utf-8')))
    with a.output.open('x',encoding='utf-8') as f:
        for row in rows:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps({'cases':len(rows),'label_status':'REVIEWED_FOR_LISTED_AXES','full_gold_complete':False}))
