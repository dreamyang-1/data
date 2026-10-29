import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import PrimaryIntent
from app.planning.compound_question import CompoundQuestionJudge, _JudgeResult
from app.planning.task_dag import SingleQuestionPlanner


def settings():
    return Settings(_env_file=None, env='test', intent_model_api_key=SecretStr('fixture-key'),
                    multi_question_model_enabled=True)


def test_judge_schema_retains_dependencies_without_intent_classification():
    schema=_JudgeResult.model_json_schema()
    assert 'intent' not in schema['properties']
    assert 'intent' not in schema['$defs']['_JudgeTask']['properties']
    assert 'depends_on' in schema['$defs']['_JudgeTask']['properties']


@pytest.mark.asyncio
@pytest.mark.parametrize('unexpected', [False, True])
async def test_retired_judge_intent_is_ignored_but_other_extra_fields_are_rejected(unexpected):
    captured=[]
    async def handler(request):
        captured.append(json.loads(request.content))
        payload={'split':True,'intent':'错误的旧意图','sub_questions':[
            {'question':'先查甲对象','intent':'旧意图','depends_on':[]},
            {'question':'再查关联乙对象','intent':'旧意图','depends_on':[0]}]}
        if unexpected:payload['unknown_scope_override']=True
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    judge=CompoundQuestionJudge(settings(),transport=httpx.MockTransport(handler))
    if unexpected:
        with pytest.raises(ValueError):await judge._model_judge('先查甲对象，另查其乙对象')
    else:
        result=await judge._model_judge('先查甲对象，另查其乙对象')
        assert result.entries[1].depends_on==(0,)
        assert not hasattr(result,'intent') and all(not hasattr(e,'intent') for e in result.entries)
    assert '不做意图判定' in captured[0]['messages'][0]['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('question,expected', [
    ('分析2025年10月到2025年12月销售额的同比增长情况', PrimaryIntent.TREND_ANALYSIS),
    ('查询各月销售额环比变化', PrimaryIntent.TREND_ANALYSIS),
    ('对比甲乙企业2025年各月销售额同比差异', PrimaryIntent.COMPARISON_ANALYSIS),
    ('查询11月销售额环比10月', PrimaryIntent.COMPARISON_ANALYSIS),
    ('根据上述任务结果分析各月销售额环比变化，不再查询数据库', PrimaryIntent.COMPARISON_ANALYSIS),
])
async def test_period_series_routing_preserves_object_comparisons_and_local_computation(question,expected):
    async def handler(request):
        payload={'intent':'COMPARISON_ANALYSIS','tasks':[{'question':question,'depends_on':[],
            'extraction':{'意图':'对比分析','实体':['销售订单'],'指标':[{'name':'销售额'}],
              '维度':['业务日期'],'排序':[{'field':'销售额','order':'desc'}],'限制':1}}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    planner=SingleQuestionPlanner(settings(),transport=httpx.MockTransport(handler))
    result=await planner._model_decompose(question)
    assert result.tasks[0].intent==expected
    if expected==PrimaryIntent.TREND_ANALYSIS:
        assert result.tasks[0].structured['意图']=='趋势分析'
        assert result.tasks[0].structured['排序']==[{'field':'业务日期','order':'asc'}]
        assert result.tasks[0].structured['限制'] is None  # choose extrema after reading the full sequence
    else:
        assert result.tasks[0].structured['排序']==[{'field':'销售额','order':'desc'}]
        assert result.tasks[0].structured['限制']==1


@pytest.mark.asyncio
async def test_local_wording_override_cannot_hide_missing_detail_fields():
    from app.planning.task_dag import TaskInputBindingError
    calls=[]
    question='查询金额最高的员工，再查该员工销售的商品名称'
    async def handler(request):
        calls.append(request)
        payload={'intent':'METRIC_QUERY','tasks':[
            {'question':'按员工统计销售金额并降序取第1名','depends_on':[],
             'extraction':{'意图':'排名分析','指标':[{'name':'销售金额'}],'维度':['员工'],
               '排序':[{'field':'销售金额','order':'desc'}],'限制':1,'是否查询数据库':'是'}},
            {'question':'根据上述任务结果查询该员工的商品名称，不再查询数据库','depends_on':[0],
             'extraction':{'意图':'明细查询','展示字段':[{'entity':'商品','field':'商品名称'}],
               '是否查询数据库':'否'}}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    planner=SingleQuestionPlanner(settings(),transport=httpx.MockTransport(handler))
    with pytest.raises(TaskInputBindingError):await planner.plan_question(question)
    assert len(calls)==2  # one bounded correction, no execution or silent fallback
