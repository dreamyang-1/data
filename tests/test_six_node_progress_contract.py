"""Current product contract has six main nodes; ASL stays inside execution."""
import json
import re
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse
from app.services.progress import emit_progress
from test_api import TestClient, build_test_app


@pytest.mark.parametrize('shape,kind', [('SINGLE','query'),('COMPOSITE','query'),
                                       ('SINGLE','reuse'),('COMPOSITE','partial')])
def test_main_nodes_are_six_and_planning_precedes_asl_sql_and_output(shape, kind):
    app = build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    class Workflow:
        async def ainvoke(self,state):
            await emit_progress('INTENT_RECOGNITION','COMPLETED','已识别问题')
            await emit_progress('TASK_PLANNING','COMPLETED','已规划全部业务目标')
            await emit_progress('ASL_GENERATION','COMPLETED', '绑定后的ASL' if kind!='reuse' else '复用完整结果，不生成新ASL或SQL')
            if kind!='reuse':
                await emit_progress('SQL_EXECUTION','COMPLETED','执行只读SQL')
            await emit_progress('DATA_RETRIEVAL','COMPLETED','结果已就绪')
            await emit_progress('RELIABILITY_CHECK','COMPLETED','已校验结果')
            await emit_progress('INSIGHT_ANALYSIS','COMPLETED','已有结果分析')
            return {'response':AgentResponse(request_id=uuid4(),conversation_id=state['chat'].conversation_id,
                status='PARTIAL_SUCCESS' if kind=='partial' else 'COMPLETED',
                execution_shape=shape,intent='METRIC_QUERY',answer='业务结果')}
    with TestClient(app) as client:
        object.__setattr__(app.state.container,'workflow',Workflow())
        response=client.post('/agent_chat/stream',json={'semantic_model_id':81,'application_id':'app',
            'conversation_id':'six-nodes','message_id':'message','question':'查询业务结果'})
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    headings=[heading.strip() for heading in re.findall(r'#### ◉ ([^\r\n]+)',text)]
    assert headings == ['意图识别','任务拆分与规划','调度执行','结果校验','数据洞察分析','最终输出']
    assert text.index('已规划全部业务目标') < text.index('调度执行')
    assert text.index('调度执行') < text.index('绑定后的ASL' if kind!='reuse' else '复用完整结果')
    assert next(i for i,e in enumerate(events) if e.get('meta',{}).get('stage')=='TASK_PLANNING') < next(
        i for i,e in enumerate(events) if e.get('meta',{}).get('stage')=='ASL_GENERATION')


def test_idempotent_cached_stream_keeps_nodes_without_reexecuting_business_work(monkeypatch):
    app=build_test_app(runtime_mode='V1',session_store_mode='memory',long_term_memory_mode='disabled')
    calls=[]
    async def run(chat,identity):
        calls.append(chat.message_id)
        for stage in ['ASL_GENERATION','DATA_RETRIEVAL','RELIABILITY_CHECK','INSIGHT_ANALYSIS']:
            await emit_progress(stage,'COMPLETED','合成验收结果')
        return AgentResponse(request_id=uuid4(),conversation_id=chat.conversation_id,
            status='COMPLETED',intent='METRIC_QUERY',answer='合成业务结果')
    with TestClient(app) as client:
        monkeypatch.setattr(app.state.container.orchestrator,'_handle',run)
        payload={'semantic_model_id':81,'application_id':'app','conversation_id':'cache-nodes',
                 'message_id':'same-message','question':'查询本月销售额'}
        assert client.post('/agent_chat/stream',json=payload).status_code==200
        response=client.post('/agent_chat/stream',json=payload)
    events=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    text=''.join(event.get('content','') for event in events if event.get('type')=='message_chunk')
    headings=[h.strip() for h in re.findall(r'#### ◉ ([^\r\n]+)',text)]
    assert headings==['意图识别','任务拆分与规划','调度执行','结果校验','数据洞察分析','最终输出']
    assert calls==['same-message']
    assert '未执行' in text and '合成业务结果' in text
