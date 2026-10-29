import json

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.planning.compound_question import CompoundQuestionJudge


@pytest.mark.asyncio
async def test_independent_question_cannot_import_another_clause_local_region():
    payload={'split':True,'sub_questions':[
        {'question':'查询上海医院数','intent':'指标查询'},
        {'question':'统计2025年上海订单笔数','intent':'指标查询'}]}
    async def handler(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    judge=CompoundQuestionJudge(Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('test'),
        multi_question_model_enabled=True),transport=httpx.MockTransport(handler))
    result=await judge.judge('查询上海医院数；统计2025年订单笔数',semantic_model_id=81)
    assert result.is_compound
    assert '上海' not in result.entries[1].question
    assert '2025' not in result.entries[0].question


@pytest.mark.asyncio
async def test_common_header_conditions_remain_legal_for_both_questions():
    payload={'split':True,'sub_questions':[
        {'question':'查询2025年上海医院数','intent':'指标查询'},
        {'question':'统计2025年上海订单笔数','intent':'指标查询'}]}
    async def handler(request):
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    judge=CompoundQuestionJudge(Settings(_env_file=None,env='test',intent_model_api_key=SecretStr('test'),
        multi_question_model_enabled=True),transport=httpx.MockTransport(handler))
    result=await judge.judge('2025年上海：查询医院数；统计订单笔数',semantic_model_id=81)
    assert result.is_compound and all('上海' in entry.question for entry in result.entries)
