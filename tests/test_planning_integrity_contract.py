"""Regression evidence for question grounding and executable task handoffs."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import PrimaryIntent
from app.planning.task_dag import SingleQuestionPlanner, TaskPlanningError


CATALOG = ('| 指标 | 同义词 | 单位 |\n| --- | --- | --- |\n'
           '| 合作时长 | 合作月数、合作周期 | 月 |\n'
           '| 订单笔数 | 订单数 | 笔 |\n'
           '| 含税销售总额 | 含税销售额 | 元 |\n'
           '| 实体 | 同义词 |\n| --- | --- |\n| 经销商 | 经销企业 |\n| 医院 | 医疗机构 |')


def planner_for(*responses):
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content))
        payload = responses[min(len(calls)-1, len(responses)-1)]
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    return SingleQuestionPlanner(Settings(_env_file=None, env='test', multi_question_model_enabled=True,
        intent_model_api_key=SecretStr('fixture-key')), transport=httpx.MockTransport(handler)), calls


def query_extraction(year='2025年'):
    return {'意图':'统计查询','实体':['经销商'],'指标':[{'name':'订单笔数'}],
        '维度':['经销商'],'过滤条件':[], '时间粒度':{'unit':'年','time_range':year},'是否查询数据库':'是'}


def test_remote_detail_metric_capability_keeps_registered_formula_and_unknown_attributes():
    raw={'意图':'明细查询','指标':[{'name':'订单均价'}, {'name':'订单备注'}],
        '维度':['订单编号'], '展示字段':[], '排序':[{'field':'订单均价','order':'asc'}], '限制':1}
    result=SingleQuestionPlanner._sanitize_extraction(raw,intent=PrimaryIntent.DETAIL_QUERY,
        metric_catalog=(frozenset({'平均每单销售金额'}),{'订单均价':'平均每单销售金额'}))
    assert result['指标']==[{'name':'平均每单销售金额'}]
    assert result['维度']==['订单编号']
    assert result['展示字段']==[{'field':'订单备注'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('change_text',[False,True])
async def test_single_plan_cannot_change_explicit_year(change_text):
    question='统计2025年各经销商订单笔数'
    wrong={'intent':'METRIC_QUERY','tasks':[{'question':question.replace('2025','2024') if change_text else question,
        'extraction':query_extraction('2024年')}]}
    right={'intent':'METRIC_QUERY','tasks':[{'question':question,'extraction':query_extraction()}]}
    planner,calls=planner_for(wrong,right)
    result=await planner.plan_question(question,semantic_context=CATALOG)
    assert len(calls)==2
    assert result.tasks[0].structured['时间粒度']['time_range']=='2025年'


@pytest.mark.asyncio
async def test_repeated_grounding_error_is_blocked_before_query():
    payload={'intent':'METRIC_QUERY','tasks':[{'question':'统计2024年各经销商订单笔数',
        'extraction':query_extraction('2024年')}]}
    planner,calls=planner_for(payload)
    with pytest.raises(TaskPlanningError):
        await planner.plan_question('统计2025年各经销商订单笔数',semantic_context=CATALOG)
    assert len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['经销商名称','经销商类别'])
async def test_local_projection_cannot_invent_same_owner_field(field):
    payload={'intent':'DETAIL_QUERY','tasks':[
        {'question':'查询经销商名称','extraction':{'意图':'明细查询','实体':['经销商'],
            '展示字段':[{'entity':'经销商','field':'经销商名称'}],'是否查询数据库':'是'}},
        {'question':f'根据上述查询结果返回{field}，不再查询数据库','depends_on':[0],
         'extraction':{'意图':'明细查询','展示字段':[{'entity':'经销商','field':field}],'是否查询数据库':'否'}}]}
    planner,_=planner_for(payload)
    if field=='经销商类别':
        with pytest.raises(TaskPlanningError):
            await planner.plan_question('查询经销商名称，再查询上述经销商的类别',semantic_context=CATALOG)
    else:
        result=await planner.plan_question('查询经销商名称，再返回上述经销商名称',semantic_context=CATALOG)
        assert len(result.tasks)==2


def test_shared_literal_city_is_not_a_foreign_condition():
    from app.planning.compound_question import CompoundJudgmentEntry,_validate_question_conditions
    source='两项分别回答：第一项统计2025年上海各经销商订单笔数；第二项列出第一项中订单笔数最多的经销商在2025年上海合作的医院名单。'
    entries=(CompoundJudgmentEntry(question='统计2025年上海各经销商订单笔数'),
        CompoundJudgmentEntry(question='列出2025年上海订单笔数最多的经销商在2025年上海合作的医院名单',depends_on=(0,)))
    _validate_question_conditions(entries,source)


def test_location_owner_in_a_relative_relationship_clause_is_the_returned_party():
    from app.planning.task_dag import _ground_location_owners
    extraction={'实体':['销售订单','医院','经销商'],
        '过滤条件':[{'entity':'销售订单','field':'省份名称','op':'=','value':['上海']}]}
    _ground_location_owners(extraction,'根据上述经销商结果，查询2025年上海合作的医院名单')
    assert extraction['过滤条件'][0]['entity']=='医院'


def test_fallback_preserves_explicit_question_reference():
    from app.planning.compound_question import rule_split_plan
    source='两项分别回答：第一项统计2025年上海各经销商订单笔数；第二项列出第一项中订单笔数最多的经销商在2025年上海合作的医院名单。'
    plan=rule_split_plan(source,max_tasks=5)
    assert plan.tasks[1].depends_on==[plan.tasks[0].task_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('aggregate',[False,True])
async def test_ranking_preserves_row_or_object_grain(aggregate):
    question=('2025年上海医院中，哪家医院所有订单的含税金额加起来最高？' if aggregate
        else '查询2025年上海含税金额最高的那一条销售订单')
    extraction={'意图':'明细查询','实体':['医院','销售订单'],'指标':[], '维度':[],
        '展示字段':[{'entity':'医院','field':'医院名称'},{'entity':'销售订单','field':'含税金额'}],
        '排序':[{'field':'含税金额','order':'desc'}],'限制':1,
        '时间粒度':{'unit':'年','time_range':'2025年'},'是否查询数据库':'是'}
    planner,_=planner_for({'intent':'DETAIL_QUERY','tasks':[{'question':question,'extraction':extraction}]})
    if aggregate:
        with pytest.raises(TaskPlanningError):
            await planner.plan_question(question,semantic_context=CATALOG)
    else:
        assert len((await planner.plan_question(question,semantic_context=CATALOG)).tasks)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('op,value,expected',[('>','12个月',('gt',12)),('>=','12个月',('gte',12)),
    ('<','1年',('lt',12)),('<=',12,('lte',12))])
async def test_registered_metric_list_compiles_bound_filter(op,value,expected):
    words={'>':'大于','>=':'不少于','<':'小于','<=':'不超过'}
    question=f'查询合作时长{words[op]}{value}的经销商名单'
    extraction={'意图':'明细查询','实体':['经销商'],'指标':[],'维度':[],
        '展示字段':[{'entity':'经销商','field':'经销商名称'}],
        '过滤条件':[{'entity':'经销商','field':'合作时长','op':op,'value':[value]}],
        '时间粒度':{'unit':None,'time_range':None},'限制':1000,'是否查询数据库':'是'}
    planner,_=planner_for({'intent':'DETAIL_QUERY','tasks':[{'question':question,'extraction':extraction}]})
    result=await planner.plan_question(question,semantic_context=CATALOG)
    assert len(result.tasks)==2
    query,compute=result.tasks
    assert query.structured['指标']==[{'name':'合作时长'}]
    assert query.structured['维度']==['经销商']
    assert not query.structured['过滤条件'] and query.structured['限制'] is None
    assert query.structured['时间粒度']['time_range'] is None
    assert compute.depends_on==(0,) and compute.structured['是否查询数据库']=='否'
    operation=compute.structured['结果操作']['operation']
    assert operation=={'type':'filter','field':'合作时长','operator':expected[0],'value':expected[1]}


@pytest.mark.asyncio
async def test_invalid_result_operation_uses_remaining_correction_attempt():
    question='统计2025年各经销商订单笔数'
    invalid={'intent':'METRIC_QUERY','tasks':[{'question':question,'extraction':query_extraction(),
        'postprocess':{'operation':{'type':'derive','operator':'-','left_field':'订单笔数',
            'right_field':'不存在的上月订单笔数','result_field':'差额'}}}]}
    correct={'intent':'METRIC_QUERY','tasks':[{'question':question,'extraction':query_extraction()}]}
    planner,calls=planner_for(invalid,correct)
    result=await planner.plan_question(question,semantic_context=CATALOG)
    assert len(calls)==2 and len(result.tasks)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit',[False,True])
async def test_local_greater_than_does_not_degrade_to_equality(explicit):
    from test_dataset_lineage_contract import setup,save,IDENTITY
    from app.domain.models import ChatRequest
    agent=setup()
    rows=[{'经销商名称':f'SYNTH-{i}','合作时长':n} for i,n in enumerate([11,12,13,None])]
    ref=save(agent,rows)
    question='根据上述任务的查询结果筛选合作时长大于12个月的经销商名单，不再查询数据库'
    chat=ChatRequest(application_id='lineage-app',conversation_id='lineage-conversation',
        message_id='threshold',question=question,semantic_model_id=81)
    operation={'type':'filter','field':'合作时长','operator':'gt','value':12} if explicit else None
    result=await agent._respond_dataset_computation(chat,IDENTITY,SimpleNamespace(question=question),[(ref,rows)],operation=operation)
    if not explicit:
        # No declared unit contract: stop rather than invent an equality filter.
        assert result.status=='FAILED' and result.dataset_id is None
        return
    assert result.status=='COMPLETED'
    refs=await agent.sessions.get_recent_dataset_references(IDENTITY.tenant_id,IDENTITY.user_id,
        chat.application_id,chat.conversation_id,limit=20)
    from app.services.dataset_followup import restore_reference
    saved=next(restore_reference(item) for item in refs if item['dataset_id']==result.dataset_id)
    assert list(agent.dataset_store.load_dataset(saved,current_scope=saved.scope).rows)==[rows[2]]


def catalog_snapshot():
    return {'scope':{'semantic_model_id':81,'business_domain_ids':[205],'scope_mode':'EXPLICIT_DOMAINS'},
        'catalog_version':'synthetic-current-v1','documents':[{'semantic_model':{'id':81},
        'business_domain':{'id':205,'name':'医药销售域'},
        'entities':[{'entity_name':'经销商','attributes':[{'attr_name':'经销商名称'}]}],
        'dimensions':[{'dim_name':'经销商'}],
        'metrics':[{'metric_name':'合作时长','synonyms':'合作月数','unit':'月',
            'business_definition':{'description':'从首次成交日起计算'},
            'calculation_rule':{'calc_formula':'registered_duration_formula'}}]}]}


@pytest.mark.asyncio
async def test_planning_uses_current_scoped_catalog_without_description_download(monkeypatch):
    from app.planning.catalog_context import render_catalog_context
    from unittest.mock import AsyncMock
    unavailable=AsyncMock(side_effect=AssertionError('No optional description download needed'))
    monkeypatch.setattr('app.domain.semantic_description.load_semantic_description',unavailable)
    context=render_catalog_context(catalog_snapshot(),81,(205,))
    question='查询各经销商合作时长'
    planner,calls=planner_for({'intent':'METRIC_QUERY','tasks':[{'question':question,
        'extraction':{'意图':'统计查询','实体':['经销商'],'指标':[{'name':'合作时长'}],'维度':['经销商']}}]})
    result=await planner.plan_question(question,semantic_context=context,semantic_model_id=81)
    assert result.metric_catalog[0]==frozenset({'合作时长'})
    assert result.metric_units=={'合作时长':'月'}
    assert 'synthetic-current-v1' in calls[0]['messages'][0]['content']
    assert not unavailable.called


@pytest.mark.parametrize('mutation',['document_model','document_domain','receipt'])
def test_planning_catalog_cannot_expand_scope(mutation):
    from app.planning.catalog_context import render_catalog_context
    snapshot=catalog_snapshot()
    if mutation=='document_model':snapshot['documents'][0]['semantic_model']['id']=82
    elif mutation=='document_domain':snapshot['documents'][0]['business_domain']['id']=206
    else:snapshot['scope']['business_domain_ids']=[206]
    with pytest.raises(ValueError,match='SCOPE_MISMATCH'):
        render_catalog_context(snapshot,81,(205,))


def test_prior_samples_are_never_declared_complete_without_proof():
    from app.planning.compat import render_prior_task_table_context
    table={'question':'合作名单','tasks':[{'名称':'查询经销商','结果概要':{
        'row_count':100,'sample':[{'经销商名称':'合成对象'}],'sample_is_complete':False}}]}
    text=render_prior_task_table_context(table)
    assert '以下仅为采样，不能作为完整对象名单' in text
    assert '把采样里的参数值逐个写进' not in text
    assert '由完整数据集及依赖绑定取得对象范围' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('single_query_postprocess',[False,True])
async def test_monthly_extrema_does_not_invent_previous_month_columns(single_query_postprocess):
    question='2025年上海月度订单笔数环比降幅最大的月份是哪一个？'
    query={'意图':'趋势分析','实体':['销售订单'],'指标':[{'name':'订单笔数'}],
        '维度':['业务日期'],'时间粒度':{'unit':'月','time_range':'2025年'},'是否查询数据库':'是'}
    operation={'type':'pipeline','operations':[
        {'type':'derive','operator':'-','left_field':'订单笔数','right_field':'上月订单笔数','result_field':'环比差'},
        {'type':'limit','n':1}]}
    payload={'intent':'TREND_ANALYSIS','tasks':[
        {'question':'统计2025年上海各月订单笔数','extraction':query},
        {'question':'根据上述任务计算环比降幅最大月份，不再查询数据库','depends_on':[0],
         'extraction':{'意图':'指标计算','是否查询数据库':'否'},
         'postprocess':{'source_task_index':0,'operation':operation}}]}
    if single_query_postprocess:
        payload['tasks']=payload['tasks'][:1]
        payload['tasks'][0]['postprocess']={'operation':operation}
    planner,_=planner_for(payload)
    result=await planner.plan_question(question,semantic_context=CATALOG)
    assert len(result.tasks)==1 and result.tasks[0].intent==PrimaryIntent.TREND_ANALYSIS
    assert result.tasks[0].question==question
    assert result.tasks[0].structured['排序']==[{'field':'业务日期','order':'asc'}]
    assert result.tasks[0].structured['限制'] is None


@pytest.mark.parametrize('months',[12,13])
def test_duration_unit_normalization_never_authorizes_a_different_threshold(months):
    from app.planning.compound_question import _validate_grounding
    plan=SimpleNamespace(tasks=[SimpleNamespace(question=f'查询合作月数超过{months}个月的经销企业')],final_deliverable=None)
    if months==12:
        _validate_grounding(plan,'列出合作月数超过一年的经销企业')
    else:
        with pytest.raises(TaskPlanningError):
            _validate_grounding(plan,'列出合作月数超过一年的经销企业')


def test_prompt_examples_are_valid_json_and_do_not_copy_another_question_filters():
    from app.planning.task_dag import extraction_parser_prompt
    text=extraction_parser_prompt()
    examples=[]
    for section in text.split('输入用户问题：')[1:]:
        question,body=section.split('输出：',1)
        value,_=json.JSONDecoder().raw_decode(body.lstrip())
        examples.append((question,value))
    assert len(examples)>=6
    question,value=next((question,value) for question,value in examples if '费森' in question)
    literal_values=[str(item) for condition in value['过滤条件'] for item in condition['value']]
    assert all(item in question for item in literal_values)
    assert '浙江' in literal_values and '费森' in literal_values


@pytest.mark.parametrize('unit,expected',[('元',1_000_000),('万元',100)])
def test_currency_threshold_chinese_scale_uses_the_declared_catalog_unit(unit,expected):
    from app.planning.metric_thresholds import numeric_threshold
    assert numeric_threshold('100万',unit)==expected


@pytest.mark.asyncio
async def test_local_operation_rejects_a_fictitious_input_column_before_execution():
    question='统计2025年各经销商订单笔数，再计算同比变化'
    payload={'intent':'METRIC_QUERY','tasks':[
        {'question':'统计2025年各经销商订单笔数','extraction':query_extraction()},
        {'question':'根据上述结果计算同比变化，不再查询数据库','depends_on':[0],
         'extraction':{'是否查询数据库':'否'},'postprocess':{'source_task_index':0,
            'operation':{'type':'derive','operator':'subtract','left_field':'订单笔数','right_field':'不存在的上一年订单笔数','result_field':'同比变化'}}}]}
    planner,calls=planner_for(payload)
    with pytest.raises(TaskPlanningError):
        await planner.plan_question(question,semantic_context=CATALOG)
    assert len(calls)==2


def test_fallback_can_reference_first_question_from_third_question():
    from app.planning.compound_question import rule_split_plan
    source='第一项统计2025年各经销商订单笔数；第二项统计2024年各医院订单笔数；第三项列出第一项中最多经销商的合作医院名单'
    plan=rule_split_plan(source,max_tasks=5)
    assert plan.tasks[2].depends_on==['task-1']
    assert '2024年' not in plan.tasks[2].question


@pytest.mark.asyncio
async def test_invalid_model_dependency_is_not_silently_removed():
    question='统计2025年各经销商订单笔数'
    planner,calls=planner_for({'intent':'METRIC_QUERY','tasks':[{'question':question,
        'depends_on':[99],'extraction':query_extraction()}]})
    with pytest.raises(TaskPlanningError):
        await planner.plan_question(question,semantic_context=CATALOG)
    assert len(calls)==2


@pytest.mark.parametrize('mode',['环比','同比'])
def test_monthly_extrema_query_includes_first_month_comparison_base(mode):
    from datetime import date
    from app.analysis.chain_tools.temporal_window import align_structured_time_window
    result=align_structured_time_window(f'2025年月度订单笔数{mode}降幅最大的月份是哪一个',
        {'unit':'月','time_range':'2025年'},intent='TREND_ANALYSIS',today=date(2026,10,19))
    assert result['time_range']==('2024-12-01至2025-12-31' if mode=='环比' else '2024-01-01至2025-12-31')
    assert align_structured_time_window('2025年月度订单笔数趋势',{'unit':'月','time_range':'2025年'},
        intent='TREND_ANALYSIS',today=date(2026,10,19)) is None


@pytest.mark.parametrize('mode',['环比','同比'])
def test_period_extrema_compare_rates_and_exclude_baseline_from_candidates(mode):
    from datetime import date
    from app.analysis.chain_tools.period_extrema import calculate_monthly_extremum
    labels=['2024-12','2025-01','2025-02','2025-03'] if mode=='环比' else ['2024-01','2024-02','2024-03','2025-01','2025-02','2025-03']
    values=[1000,500,100,90] if mode=='环比' else [1000,100,10,500,20,9]
    result=calculate_monthly_extremum(f'2025年月度订单笔数{mode}降幅最大的月份是哪一个',labels,values,
        (date(2025,1,1),date(2025,3,31)))
    assert result['complete'] and result['compared_period_count']==3
    assert result['selected'][0]['to']=='2025-02'
    assert result['selected'][0]['change_rate']==pytest.approx(-.8)


def test_period_extrema_exposes_missing_baseline_and_zero_base():
    from datetime import date
    from app.analysis.chain_tools.period_extrema import calculate_monthly_extremum
    result=calculate_monthly_extremum('2025年月度订单笔数环比降幅最大的月份是哪一个',
        ['2025-01','2025-02','2025-03'],[0,100,90],(date(2025,1,1),date(2025,3,31)))
    assert not result['complete'] and result['missing_period_count']==1
    assert result['undefined_base_periods']==['2025-02']


def test_trend_engine_answers_rate_extremum_without_replacing_normal_trend():
    from app.analysis import AnalysisEngine
    from app.domain.models import PlannerExtraction,KnowledgeContext
    from test_analysis_engine import request
    query=request(PrimaryIntent.TREND_ANALYSIS,'订单笔数')
    query.original_question='2025年月度订单笔数环比降幅最大的月份是哪一个'
    query._planner_extraction=PlannerExtraction(structured={'时间粒度':{'unit':'月','time_range':'2025年1月至2025年3月'}})
    output=AnalysisEngine().analyze(query,['月份','订单笔数'],[{'月份':month,'订单笔数':number}
        for month,number in zip(['2024-12','2025-01','2025-02','2025-03'],[1000,500,100,90])],KnowledgeContext(query=''))
    assert output.method=='monthly_period_extremum'
    assert output.facts['period_extremum']['selected'][0]['to']=='2025-02'
    assert '环比-80.00%' in output.answer
