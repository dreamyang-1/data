import json
from types import SimpleNamespace
import httpx
import pytest
from pydantic import SecretStr
from app.domain.models import PendingState
from test_complex_execution_correctness import context,service,IDENTITY


@pytest.mark.asyncio
@pytest.mark.parametrize('matching_scope,complete',[(True,False),(False,True),(True,True)])
async def test_pending_triage_only_expands_a_proven_complete_scoped_small_result(matching_scope,complete):
    agent=service();chat,request=context()
    agent.settings=agent.settings.model_copy(update={'intent_model_enabled':True,'intent_model_api_key':SecretStr('test')})
    chat.question='这些机构都按算术平均计算'
    request.original_question='计算机构的平均月度增长率'
    await agent.sessions.put_pending(PendingState(request=request),expected_version=0)
    table={'question':'查询机构','semantic_scope_fingerprint':chat.authorized_semantic_scope.fingerprint() if matching_scope else 'other-scope',
        'tasks':[{'名称':'查询机构','结果概要':{'row_count':1 if complete else 50,
            'sample_is_complete':complete,'sample':[{'机构编码':'SAMPLE-ONLY-001'}]}}]}
    await agent.sessions.put_task_table(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,chat.conversation_id,table)
    prompts=[]
    async def model(request):
        prompts.append(json.loads(request.content)['messages'][0]['content'])
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({
            'relation':'ANSWER','merged_question':'计算上述机构的平均月度增长率，按算术平均','reason':'用户确认了算法'})}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(model),base_url='https://offline.invalid') as client:
        agent._triage_http_client=client
        result=await agent.triage_v1_pending_reply(chat,IDENTITY)
    assert result[0]=='ANSWER'
    assert ('SAMPLE-ONLY-001' in prompts[0]) is (matching_scope and complete)


@pytest.mark.parametrize('total,truncated,long_value,expected',[(50,False,False,False),(1,True,False,False),
    (None,False,False,False),(1,False,True,False),(1,False,False,True)])
def test_small_preview_is_not_source_completeness_proof(total,truncated,long_value,expected):
    agent=service()
    dataset=SimpleNamespace(columns=['机构名称'],rows=[{'机构名称':'x'*90 if long_value else '机构A'}],
        row_count=1,total_row_count=total,truncated=truncated)
    summary=agent._summarize_query_result(dataset)
    assert summary.get('sample_is_complete',False) is expected


@pytest.mark.asyncio
async def test_new_task_summary_records_current_scope_for_pending_reuse():
    agent=service();chat,_=context()
    chat._task_table=[{'任务':'task-1','名称':'查询机构','执行状态':'已完成'}]
    chat._last_result_summary={'row_count':1,'sample_is_complete':True,'sample':[{'机构编码':'001'}]}
    await agent._persist_conversation_task_table(chat,IDENTITY)
    table=await agent.sessions.get_task_table(IDENTITY.tenant_id,IDENTITY.user_id,chat.application_id,chat.conversation_id)
    assert table['semantic_scope_fingerprint']==chat.authorized_semantic_scope.fingerprint()
    assert table['tasks'][0]['结果概要']['sample_is_complete'] is True
