import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.planning.task_dag import SingleQuestionPlanner


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id,owner,field,source,expected', [
    (81,'医院','业务市','查询上海地区各医院2025年订单笔数','城市'),
    (81,'医院','业务省','查询上海地区各医院2025年订单笔数','省份'),
    (81,'医院','业务市','按医院业务市统计2025年订单笔数','业务市'),
    (81,'医院','业务市','按医院业务城市统计2025年订单笔数','业务市'),
    (81,'医院','业务市','查询业务区域在上海的医院订单笔数','业务市'),
    (82,'医院','业务市','查询上海地区各医院2025年订单笔数','业务市'),
    (81,'经销商','业务市','查询上海地区各经销商2025年订单笔数','业务市'),
])
async def test_only_inferred_hospital_business_qualifier_is_normalized(
        monkeypatch,model_id,owner,field,source,expected):
    monkeypatch.setattr(semantic_description,'load_semantic_description',AsyncMock(return_value={
        'available':True,'source':f'semantic_model_{model_id}.md','content':'当前模型业务语义规范'}))
    async def model(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'tasks':[{
            'question':source,'depends_on':[],'extraction':{'意图':'统计查询','实体':[owner],
            '指标':[{'name':'订单笔数'}],'维度':[owner],
            '过滤条件':[{'entity':owner,'field':field,'op':'=','value':['上海']}],
            '时间粒度':{'time_range':'2025年'},'是否查询数据库':'是'}}]},ensure_ascii=False)}}]})
    planner=SingleQuestionPlanner(Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('fixture'),
        multi_question_model_enabled=True),transport=httpx.MockTransport(model))
    result=await planner.plan_question(source,semantic_model_id=model_id)
    task=result.tasks[0]
    assert task.structured['过滤条件']==[{'entity':owner,'field':expected,'op':'=','value':['上海']}]
    assert task.structured['维度']==[owner] and task.structured['时间粒度']['time_range']=='2025年'
    assert expected in ' '.join(task.parameters)
