"""First completion call owns analytical routing, not a gate or regex."""
import json
import pytest

from test_v2_raw_turn_recognition import planner,request,IDENTITY
from test_v2_pending_recognition import catalog
from test_v2_lightweight_current_turn import split_instruction_and_schema,data_message


@pytest.mark.asyncio
@pytest.mark.parametrize('routes',[[],['trend'],['anomaly'],['attribution'],['forecast'],['trend','attribution','forecast']])
async def test_completion_routes_survive_handoff_with_names_only_even_when_gate_confirms_new_task(catalog,routes):
    question='完整的业务问题'
    def supplied(context):
        return dict(status='ACCEPTED',relation='NEW_TASK',target_task_id=None,
            state_version=context['task_context']['state_version'],task_version=None,pending_id=None)
    engine,transport=planner(catalog,[(question,{'context_proposal':supplied,
        'completed_question':question,'analysis_routes':routes},None)])
    engine.defer_new_task_binding=True
    engine.analysis_tool_catalog_enabled=True
    result=await engine.run(request(question=question,message_id='catalog-routing'),IDENTITY,
        allow_standalone_new_task_passthrough=True,gate_confirmed_new_task=True)
    assert result.analysis_routes==routes
    assert len(transport.calls)==1
    instruction,schema,_=split_instruction_and_schema(transport.calls[0])
    assert 'analysis_routes' in schema['required']
    data=json.loads(data_message(transport.calls[0])['content'])
    assert len(data['analysis_chains'])==6
    assert all(set(tool)=={'id','name'} for chain in data['analysis_chains'] for tool in chain['tools'])
    assert 'required_data' not in json.dumps(data['analysis_chains'])
    assert not result.parse.mentions
    assert result.next_state.context.authorized_scope.business_domain_ids==(205,)
