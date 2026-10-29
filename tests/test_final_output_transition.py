"""Final business results must leave the platform's thinking state."""
import json
from uuid import uuid4

import pytest

from app.domain.models import AgentResponse
from app.services.progress import emit_progress
from test_api import TestClient, build_test_app


@pytest.mark.parametrize("shape,status,intent,with_progress", [
    ("COMPOSITE", "COMPLETED", "METRIC_QUERY", True),
    ("COMPOSITE", "PARTIAL_SUCCESS", "METRIC_QUERY", True),
    ("SINGLE", "COMPLETED", "METRIC_QUERY", True),
    ("SINGLE", "NEEDS_CLARIFICATION", "METRIC_QUERY", True),
    ("SINGLE", "SAFE_FALLBACK", "METRIC_QUERY", True),
    ("COMPOSITE", "COMPLETED", "METRIC_QUERY", False),
    ("SINGLE", "COMPLETED", "CHAT", False),
])
def test_output_state_precedes_answer_and_preserves_table_and_link(shape, status, intent, with_progress):
    app = build_test_app(runtime_mode="V1", session_store_mode="memory", long_term_memory_mode="disabled")
    answer = "| 经销商 | 覆盖率 |\n| --- | --- |\n| 示例公司 | 43.80% |\n\n[下载完整结果](https://example.test/result.xlsx)"
    if status == "NEEDS_CLARIFICATION":
        answer = "缺少指标定义，请补充计算口径。"
    elif status == "SAFE_FALLBACK":
        answer = "查询服务暂不可用，请稍后重试。"

    class Workflow:
        async def ainvoke(self, state):
            if with_progress:
                for stage in ["INTENT_RECOGNITION", "TASK_PLANNING", "ASL_GENERATION", "DATA_RETRIEVAL", "RELIABILITY_CHECK", "INSIGHT_ANALYSIS"]:
                    await emit_progress(stage, "COMPLETED", "节点内容：" + stage)
            return {"response": AgentResponse(request_id=uuid4(), conversation_id=state["chat"].conversation_id,
                execution_shape=shape, status=status, intent=intent, answer=answer)}

    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", Workflow())
        response = client.post('/agent_chat/stream', json={"semantic_model_id": 81, "application_id": "app1",
            "conversation_id": "output-transition", "message_id": "m1", "question": "查询各经销商覆盖率"})
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    # Model the platform consumer: answer chunks only render once the node
    # state changes to output, not merely because a chunk labels itself output.
    active = None
    visible = []
    transitions = []
    for n, event in enumerate(events):
        if event['type'] == 'updata_state':
            active = event['data']
            if active == 'output':
                transitions.append(n)
        if event['type'] == 'message_chunk' and event.get('step') == 'output':
            if active == 'output':
                visible.append(event['content'])
    assert len(transitions) == 1
    assert ''.join(visible) == answer
    assert next(event for event in events if event['type'] == 'answer')['content'] == answer
    assert events[-1]['type'] == 'complete' and events[-1]['answer'] == answer
    assert all(event.get('step') == 'output' for event in events[transitions[0]+1:])
    assert not any(event['type'] == 'error' for event in events)
