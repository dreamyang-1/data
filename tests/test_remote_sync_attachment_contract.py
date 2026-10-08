"""Keep the imported composite attachment behavior without guessing datasets."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.domain.models import (
    AgentResponse, AtomicTask, ChatRequest, PrimaryIntent, TaskExecutionResult,
    TaskPlan, TrustedIdentity,
)
from app.services.orchestrator import DataAnalysisOrchestrator


@pytest.mark.asyncio
async def test_composite_export_without_dataset_ids_keeps_original_links():
    service = object.__new__(DataAnalysisOrchestrator)
    service.report_exporter = SimpleNamespace(export_many=AsyncMock())
    service.sessions = SimpleNamespace(get_recent_dataset_references=AsyncMock())
    chat = ChatRequest(application_id="app", conversation_id="root", message_id="m1",
                       question="查询医院数并计算覆盖率", semantic_model_id=81)
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="first", question="查询医院数"),
        AtomicTask(task_id="second", question="根据上述结果计算覆盖率，不再查询数据库",
                   depends_on=["first"]),
    ])
    response = AgentResponse(request_id=uuid4(), conversation_id="root", status="COMPLETED",
                             intent=PrimaryIntent.METRIC_QUERY, answer="查询及计算结果")
    links = ["[下载完整结果](https://files.example/original.xlsx)"]
    await service._attach_composite_report(
        response, chat=chat, identity=TrustedIdentity(tenant_id="t", user_id="u"),
        plan=plan, responses={task.task_id: response.model_copy() for task in plan.tasks},
        conversation_by_task={"first": "child1", "second": "child2"},
        markdown_link=True, fallback_links=links,
    )
    assert response.answer.endswith("附件：" + links[0])
    service.sessions.get_recent_dataset_references.assert_not_called()
    service.report_exporter.export_many.assert_not_called()


def test_computation_summary_is_not_wrapped_in_an_extra_task_heading():
    result = TaskExecutionResult(task_id="first", question="查询医院数", status="COMPLETED",
                                intent=PrimaryIntent.METRIC_QUERY, answer="医院数：10")
    computation = TaskExecutionResult(
        task_id="second", question="根据上述任务的查询结果计算覆盖率，不再查询数据库",
        status="COMPLETED", intent=PrimaryIntent.CHAT,
        answer="**覆盖率**：共2行，最高为测试经销商50.00%。",
    )
    answer, links = DataAnalysisOrchestrator._render_merged_task_answers([result, computation])
    assert answer.count(computation.answer) == 1
    assert "不再查询数据库" not in answer
    assert links == []


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['FAILED', 'SAFE_FALLBACK', 'NEEDS_CLARIFICATION', 'CANCELLED', 'SKIPPED'])
@pytest.mark.parametrize('selection', [None, []])
@pytest.mark.parametrize('exporter_enabled', [False, True])
async def test_no_completed_output_never_adds_download_notices(status, selection, exporter_enabled):
    service = object.__new__(DataAnalysisOrchestrator)
    exporter = SimpleNamespace(export_many=AsyncMock())
    service.report_exporter = exporter if exporter_enabled else None
    service.sessions = SimpleNamespace(get_recent_dataset_references=AsyncMock())
    chat = ChatRequest(application_id='app', conversation_id='root', message_id='m',
                       question='查询人员及其公司', semantic_model_id=120)
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=[AtomicTask(task_id='failed', question=chat.question),
        AtomicTask(task_id='derived', question='根据结果计算排名', depends_on=['failed'])])
    response = AgentResponse(request_id=uuid4(), conversation_id='root', status=status,
                             intent=PrimaryIntent.METRIC_QUERY, answer='具体失败原因或需要补充的内容')
    await service._attach_composite_report(response, chat=chat,
        identity=TrustedIdentity(tenant_id='t', user_id='u'), plan=plan,
        responses={'failed': response.model_copy(update={'dataset_id': 'stale-dataset'})},
        conversation_by_task={}, markdown_link=True, selected_task_ids=selection)
    assert response.answer == '具体失败原因或需要补充的内容'
    assert response.files == []
    service.sessions.get_recent_dataset_references.assert_not_called()
    exporter.export_many.assert_not_called()


@pytest.mark.asyncio
async def test_empty_final_selection_does_not_export_completed_intermediate():
    service = object.__new__(DataAnalysisOrchestrator)
    service.report_exporter = SimpleNamespace(export_many=AsyncMock())
    service.sessions = SimpleNamespace(get_recent_dataset_references=AsyncMock())
    chat = ChatRequest(application_id='app', conversation_id='root', message_id='m',
                       question='查询人员及其公司', semantic_model_id=120)
    plan = TaskPlan(planner='STRUCTURED_MODEL', tasks=[AtomicTask(task_id='source', question='中间查询'),
        AtomicTask(task_id='derived', question='根据结果计算排名', depends_on=['source'])])
    response = AgentResponse(request_id=uuid4(), conversation_id='root', status='SAFE_FALLBACK',
                             intent=PrimaryIntent.METRIC_QUERY, answer='无法完成最终计算')
    source = response.model_copy(update={'status': 'COMPLETED', 'dataset_id': 'source'})
    await service._attach_composite_report(response, chat=chat,
        identity=TrustedIdentity(tenant_id='t', user_id='u'), plan=plan,
        responses={'source': source}, conversation_by_task={}, selected_task_ids=[])
    assert response.answer == '无法完成最终计算'
    service.report_exporter.export_many.assert_not_called()
