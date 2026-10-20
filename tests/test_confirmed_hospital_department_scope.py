import json
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.domain import semantic_description
from app.planning.task_dag import SingleQuestionPlanner


@pytest.mark.asyncio
@pytest.mark.parametrize('model_id,field,source,expected', [
    (81, '业务科室', '上海地区哪个医院消化内科2025年9月的订单数量最多？该医院销售了哪些产品？', True),
    (81, 'sales_order.business_department', '上海地区哪个医院消化内科2025年9月的订单数量最多？该医院销售了哪些产品？', True),
    (81, '业务科室', '按医院订单业务科室统计2025年9月订单笔数', False),
    (81, '科室名称', '统计医院下属科室数量', False),
    (82, '业务科室', '查询医院消化内科2025年9月订单最多的医院', False),
])
async def test_confirmed_product_department_meaning_reaches_original_asl_reference(
        monkeypatch, model_id, field, source, expected):
    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source':f'semantic_model_{model_id}.md','available':True,'content':'当前模型业务语义规范'}))
    department={'entity':'销售订单','field':field,'op':'=','value':['消化内科']}
    region={'entity':'医院','field':'城市','op':'=','value':['上海']}

    async def model(request):
        tasks=[]
        for index in range(2):
            tasks.append({'question':'查询2025年9月消化内科订单最多的医院' if index==0 else '根据上述结果查询该医院的产品',
                'depends_on':[] if index==0 else [0], 'extraction':{'意图':'统计查询' if index==0 else '明细查询',
                '实体':['医院'],'指标':[{'name':'订单笔数'}] if index==0 else [],'维度':['医院'] if index==0 else [],
                '过滤条件':[department,region], '限制':1 if index==0 else None,
                '时间粒度':{'time_range':'2025年9月'},'是否查询数据库':'是'}})
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'tasks':tasks},ensure_ascii=False)}}]})

    planner=SingleQuestionPlanner(Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('fixture'),
        multi_question_model_enabled=True),transport=httpx.MockTransport(model))
    result=await planner.plan_question(source,semantic_model_id=model_id)
    assert len(result.tasks)==2 and result.tasks[1].depends_on==(0,)
    for task in result.tasks:
        condition=task.structured['过滤条件'][0]
        assert condition['field']==('科室编码' if expected else field)
        assert condition['entity']==('商品科室关联' if expected else '销售订单')
        assert condition['value']==['消化内科']
        assert task.structured['过滤条件'][1]==region
        assert task.structured['时间粒度']['time_range']=='2025年9月'
        assert ('商品适用科室' in task.question)==expected
        assert '消化内科' in ' '.join(task.parameters)
    assert result.tasks[0].structured['维度']==['医院'] and result.tasks[0].structured['限制']==1
