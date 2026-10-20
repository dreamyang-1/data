from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.planning.compound_question import CompoundJudgment, CompoundJudgmentEntry, CompoundTaskPlanner
from app.planning.task_dag import DecomposedTask, QuestionDecomposition, SingleQuestionPlanner


@pytest.mark.asyncio
@pytest.mark.parametrize('region', ['上海', '江苏'])
async def test_independent_monthly_and_regional_compound_metric_keep_all_question_groups(monkeypatch, region):
    first='统计2025年月度订单笔数'
    second=f'查询{region}地区各经销商区域医院覆盖率'
    source=first+'，'+second
    filter_={'entity':'医院','field':'城市' if region=='上海' else '省份','op':'=','value':[region]}
    base={'实体':['医院'],'指标':[{'name':'区域全部医院总数'}],'维度':[],
        '过滤条件':[filter_],'时间粒度':{'time_range':'最近12个月'},'是否查询数据库':'是'}
    decompositions=[
        QuestionDecomposition(tasks=(DecomposedTask(question=first,structured={
            '指标':[{'name':'订单笔数'}],'时间粒度':{'time_range':'2025年'}}),)),
        QuestionDecomposition(grounding_default_times=('最近12个月',),shared_region_denominator=True,tasks=(
            DecomposedTask(question=f'查询最近12个月{region}各经销商已合作医院数',structured={
                **base,'指标':[{'name':'已合作医院数'}],'维度':['经销商']}),
            DecomposedTask(question=f'查询最近12个月{region}区域全部医院总数',structured=base),
            DecomposedTask(question=f'根据上述结果计算{region}各经销商区域医院覆盖率，不再查询数据库',
                depends_on=(0,1),structured={'指标':[{'name':'区域医院覆盖率'}],'是否查询数据库':'否'}))),
    ]
    settings=Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('fixture'),multi_question_model_enabled=True)
    planner=SingleQuestionPlanner(settings)
    monkeypatch.setattr(planner,'_model_decompose',AsyncMock(side_effect=decompositions))
    judgment=CompoundJudgment(is_compound=True,entries=(CompoundJudgmentEntry(question=first),CompoundJudgmentEntry(question=second)))
    result=await CompoundTaskPlanner(settings,planner).plan(judgment,source_question=source,semantic_model_id=81)
    assert len(result.tasks)==4
    assert [group.task_count for group in result.question_groups]==[1,3]
    assert result.tasks[-1].depends_on==['task-2','task-3']
    denominator=result.tasks[2]
    assert denominator.extraction['过滤条件']==[filter_]
    assert denominator.extraction['时间粒度']['time_range']=='全部历史'
    assert region in denominator.question and '"field"' not in denominator.question
