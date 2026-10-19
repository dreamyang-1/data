"""Unexecutable planner thresholds are system errors, not missing user facts."""
from copy import deepcopy
from test_structured_binding import fixture,run


def test_detail_metric_threshold_is_reported_as_planning_failure():
    extraction,knowledge,plan=fixture()
    extraction.update(指标=[],展示字段=[{'entity':'医院','field':'医院名称'}],
        过滤条件=[{'field':'区域全部医院总数','op':'>','value':[12]}])
    plan.update(metrics=[],display_fields=[{'index':0,'key':'hospital.name'}],
        filters=[{'index':0,'key':'count_hospital'}])
    ast,_=run(extraction,knowledge,plan)
    failure=next(item for item in ast['ambiguity'] if item.get('ambiguity_id')=='PLANNER_METRIC_THRESHOLD_RESULT_OPERATION')
    assert failure['type']=='context' and failure['affected_slots']==['task_plan']
    assert failure['candidates']==[] and '请补充' not in failure['question']
    assert not ast['filters'] and not ast['having']


def test_supported_aggregate_having_keeps_its_existing_behavior():
    extraction,knowledge,plan=fixture()
    knowledge=deepcopy(knowledge)
    knowledge['metrics'][0].metadata['calculation_rule']={'calc_formula':'count_hospital=COUNT(hospital.name)'}
    extraction['过滤条件']=[{'field':'区域全部医院总数','op':'>','value':[12]}]
    plan['filters']=[{'index':0,'key':'count_hospital'}]
    ast,_=run(extraction,knowledge,plan)
    assert ast['having']==['COUNT(hospital.name) > 12'] and not ast['ambiguity']


def test_detail_relationship_uses_its_declared_dated_transaction_as_subject():
    from types import SimpleNamespace as Obj
    extraction,knowledge,plan=fixture()
    order={'entity_code':'fact','entity_name':'交易','attributes':[
        {'attr_name':'日期','field_mapping':'fact.recorded_on','data_type':'DATE'}]}
    knowledge['entities'].append(Obj(metadata=order))
    knowledge['entities'][0].metadata['entity_name']='医院'
    knowledge['relations']=[Obj(metadata={'join_key':{'source_field':'fact.hospital_id','target_field':'hospital.id'}})]
    knowledge['_vector_authorized_fields'].append('fact.recorded_on')
    extraction.update(意图='明细查询',实体=['交易','医院'],指标=[],维度=[],
        展示字段=[{'entity':'医院','field':'医院名称'}],过滤条件=[],时间粒度={'unit':'年','time_range':'2025年'})
    plan.update(subject='hospital',metrics=[],filters=[],
        display_fields=[{'index':0,'key':'hospital.name'}],time={'anchor':'fact.recorded_on'})
    ast,repairs=run(extraction,knowledge,plan)
    assert not ast['ambiguity']
    assert ast['subject']=={'entity':'fact'}
    assert ast['time_context']['anchor']=='fact.recorded_on'
    assert any(item['type']=='DETAIL_SUBJECT_FROM_DECLARED_TIME_ANCHOR' for item in repairs)
    extraction['时间粒度']={'unit':None,'time_range':None}
    ast,_=run(extraction,knowledge,plan)
    assert ast['subject']=={'entity':'hospital'}
