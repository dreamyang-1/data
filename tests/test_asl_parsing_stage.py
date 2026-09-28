"""ASL parsing is a separate public stage; SQL keeps its existing protocol."""
import json
from uuid import uuid4

from app.api import _CompositeChildProgressOrderer, _thinking_section, _thinking_title
from app.domain.models import AgentResponse, PrimaryIntent
from app.services.progress import emit_progress
from tests.test_api import TestClient, build_test_app


def child(stage, status, index, **extra):
    return dict(stage=stage, status=status, message=f"{stage}-{index}",
                task_id=f"task-{index}", task_index=index, task_count=2,
                is_child_task=True, **extra)


def test_all_child_asl_precedes_any_sql_even_when_first_task_finishes_early():
    orderer = _CompositeChildProgressOrderer()
    output = orderer.push(child("ASL_GENERATION", "COMPLETED", 0))
    assert orderer.push(child("SEMANTIC_QUERY_PLANNING", "COMPLETED", 0)) == []
    assert orderer.push(child("DATA_RETRIEVAL", "COMPLETED", 0)) == []
    assert orderer.push(child("RELIABILITY_CHECK", "COMPLETED", 0)) == []
    output += orderer.push(child("ASL_GENERATION", "COMPLETED", 1))
    output += orderer.push(child("SQL_EXECUTION", "COMPLETED", 1))
    output += orderer.push(child("DATA_RETRIEVAL", "COMPLETED", 1))
    output += orderer.flush()
    sections = [_thinking_section(event["stage"]) for event in output]
    assert sections == sorted(sections, key={"parsing": 0, "execution": 1, "validation": 2}.get)
    assert "任务1" in output[0]["message"]
    assert "任务2" in output[1]["message"]
    assert sum(event["stage"] == "ASL_GENERATION" for event in output) == 2


def test_clarifying_child_does_not_invent_sql_execution_or_block_sibling():
    orderer = _CompositeChildProgressOrderer()
    output = orderer.push(child("ASL_GENERATION", "NEEDS_INPUT", 1))
    output += orderer.push(child("DATA_RETRIEVAL", "SKIPPED", 1, task_terminal=True))
    output += orderer.push(child("ASL_GENERATION", "COMPLETED", 0))
    output += orderer.push(child("SQL_EXECUTION", "COMPLETED", 0))
    output += orderer.push(child("DATA_RETRIEVAL", "COMPLETED", 0))
    output += orderer.push(child("RELIABILITY_CHECK", "COMPLETED", 0))
    output += orderer.push(child("INSIGHT_ANALYSIS", "COMPLETED", 0))
    output += orderer.flush()
    assert not any(e["task_index"] == 1 and _thinking_section(e["stage"]) == "execution" for e in output)
    assert output[-1]["stage"] == "INSIGHT_ANALYSIS"


def test_asl_clarification_stream_stays_in_parsing_without_sql_heading():
    app = build_test_app()

    class Workflow:
        async def ainvoke(self, state):
            await emit_progress("INTENT_RECOGNITION", "COMPLETED", "用户问题已补全")
            await emit_progress("TASK_PLANNING", "COMPLETED", "拆分判断完成。当前问题无需拆分。")
            await emit_progress("ASL_GENERATION", "RUNNING", "正在绑定参数")
            await emit_progress("ASL_GENERATION", "NEEDS_INPUT", "商品品类尚不能唯一绑定，请选择标准值。")
            return {"response": AgentResponse(request_id=uuid4(), conversation_id=state["chat"].conversation_id,
                    status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                    answer="请确认商品品类。")}

    with TestClient(app) as client:
        object.__setattr__(app.state.container, "workflow", Workflow())
        response = client.post("/agent_chat/stream", headers={"X-Tenant-Id": "t1", "X-User-Id": "u1"}, json={"semantic_model_id": 81,
                "application_id": "app", "conversation_id": "parsing-clarification",
                "message_id": "m1", "question": "查询销售额"})
    events = [json.loads(block.removeprefix("data: ")) for block in response.text.strip().split("\n\n")]
    assert response.status_code == 200, response.text
    text = "".join(event.get("content", "") for event in events if event.get("type") == "message_chunk")
    assert text.index("任务拆分与规划") < text.index("解析校验") < text.index("商品品类尚不能唯一绑定")
    assert "#### ◉ 调度执行" not in text
    assert _thinking_title(_thinking_section("SEMANTIC_QUERY_PLANNING")) == "#### ◉ 调度执行"
