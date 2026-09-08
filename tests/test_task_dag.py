from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import httpx
from pydantic import SecretStr

from app.config import Settings
from app.domain.models import (
    AgentResponse,
    AtomicTask,
    CanonicalAnalysisRequest,
    ChatRequest,
    ClarificationItem,
    EvidenceItem,
    HistoryMessage,
    PrimaryIntent,
    ReliabilityReport,
    TaskExecutionResult,
    TaskPlan,
    TrustedIdentity,
)
from app.planning import MultiQuestionPlanner, TaskPlanningError
from app.intent import RuleBasedIntentClassifier
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.adapters import build_mock_adapters
from minio_followup_store import DatasetReference, DatasetScope


@pytest.mark.asyncio
async def test_completed_dag_persists_branch_focus_in_root_conversation() -> None:
    sessions = InMemorySessionStore()
    service = object.__new__(DataAnalysisOrchestrator)
    service.sessions = sessions
    identity = TrustedIdentity(tenant_id="tenant", user_id="user")
    chat = ChatRequest(
        application_id="app",
        conversation_id="root-compound-departments",
        message_id="root-message",
        question="查询 TDC-3 产品的主要适用科室、次要适用科室",
        semantic_model_id=81,
    )
    tasks = [
        AtomicTask(task_id="task-1", question="查询 TDC-3 产品的主要适用科室"),
        AtomicTask(task_id="task-2", question="查询 TDC-3 产品的次要适用科室"),
    ]
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=tasks)
    conversations = {
        "task-1": "dag-child-primary",
        "task-2": "dag-child-secondary",
    }
    responses = {}
    for task, relation_type in zip(tasks, (1, 2), strict=True):
        child = CanonicalAnalysisRequest(
            semantic_model_id=81,
            conversation_id=conversations[task.task_id],
            tenant_id="tenant",
            user_id="user",
            application_id="app",
            original_question=task.question,
            primary_intent=PrimaryIntent.DETAIL_QUERY,
            entity="产品",
            fields=["商品名称", "适用科室"],
            semantic_entity_mentions=["TDC-3"],
            filters=[{
                "field": "适用科室类型",
                "operator": "EQ",
                "value": relation_type,
            }],
            missing_slots=[],
            asl_template={"subject": "product"},
        )
        await sessions.put_last_request(child)
        responses[task.task_id] = AgentResponse(
            request_id=uuid4(),
            conversation_id=child.conversation_id,
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="查询完成",
        )

    await service._persist_dag_root_context(
        chat=chat,
        identity=identity,
        plan=plan,
        responses=responses,
        conversation_by_task=conversations,
        root_message_id=chat.message_id,
    )

    focused = await sessions.get_last_request(
        "tenant", "user", "app", chat.conversation_id
    )
    branches = await sessions.get_recent_task_frames(
        "tenant", "user", "app", chat.conversation_id, limit=12
    )
    assert focused is not None
    assert focused.semantic_entity_mentions == ["TDC-3"]
    assert focused.filters[-1]["value"] == 2
    assert {frame.filters[-1]["value"] for frame in branches} == {1, 2}


@pytest.mark.asyncio
async def test_rule_planner_splits_numbered_independent_questions() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan(
        "1. 查询本月销售额；2. 分析最近半年销售趋势；3. 解释销售额口径"
    )
    assert plan is not None
    assert [task.task_id for task in plan.tasks] == ["task-1", "task-2", "task-3"]
    assert all(not task.depends_on for task in plan.tasks)
    assert len(planner.execution_layers(plan)) == 1


@pytest.mark.asyncio
async def test_rule_planner_keeps_dependent_post_calculations() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan(
        "查询2026年7月销售额；按区域拆分；找出最高和最低区域；再算最高比最低高多少"
    )

    assert plan is not None
    assert len(plan.tasks) == 4
    assert plan.tasks[2].depends_on == ["task-2"]
    assert plan.tasks[3].depends_on == ["task-3"]


@pytest.mark.asyncio
async def test_rule_planner_splits_mixed_query_definition_and_export() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan(
        "2026年7月销售额是多少？另外退款率口径是什么？再导出订单明细"
    )

    assert plan is not None
    assert len(plan.tasks) == 3
    assert "销售额是多少" in plan.tasks[0].question
    assert "退款率口径" in plan.tasks[1].question
    assert "导出订单明细" in plan.tasks[2].question


@pytest.mark.asyncio
async def test_single_continuous_analysis_is_not_split() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    assert await planner.plan("查询最近半年销售额并分析趋势") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    (
        "查询 TDC-3 产品的主要适用科室、次要适用科室",
        "查询 TDC‑3 产品的主要和次要适用科室",
        "查询 TDC-3 产品的次要适用科室、主要适用科室",
    ),
)
async def test_single_action_parallel_qualified_facets_are_split(question: str) -> None:
    planner = MultiQuestionPlanner(
        Settings(env="test", multi_question_model_enabled=False)
    )

    plan = await planner.plan(question)

    assert plan is not None
    assert len(plan.tasks) == 2
    assert {"主要适用科室", "次要适用科室"} == {
        "主要适用科室" if "主要适用科室" in task.question else "次要适用科室"
        for task in plan.tasks
    }
    assert all("TDC" in task.question and "3" in task.question for task in plan.tasks)
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    (
        "查询TDC-3产品的适用科室",
        "查询TDC-3产品的所有适用科室",
        "查询TDC-3的商品名称、规格型号",
        "按城市和品牌统计销售额",
    ),
)
async def test_plain_projection_or_all_scope_is_not_split(question: str) -> None:
    planner = MultiQuestionPlanner(
        Settings(env="test", multi_question_model_enabled=False)
    )

    assert await planner.plan(question) is None


def test_parallel_facet_validation_requires_every_branch_exactly_once() -> None:
    source = "查询 TDC-3 产品的主要适用科室、次要适用科室"
    invalid = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询TDC-3产品的主要适用科室"),
        AtomicTask(task_id="task-2", question="查询TDC-3产品的主要适用科室"),
    ])

    with pytest.raises(TaskPlanningError, match="并列业务分支"):
        MultiQuestionPlanner.validate(invalid, source_question=source)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "expected"),
    (
        (
            "查询销量前十的产品，以及下单次数最多的三个客户。",
            ["查询销量前十的产品", "查询下单次数最多的三个客户"],
        ),
        (
            "分别查询销售额最高的三家经销商和合作医院最多的三家经销商。",
            ["查询销售额最高的三家经销商", "查询合作医院最多的三家经销商"],
        ),
        (
            "查询销量最高的三个产品，同时查询销售额最高的三个产品。",
            ["查询销量最高的三个产品", "查询销售额最高的三个产品"],
        ),
    ),
)
async def test_parallel_rankings_with_shared_or_repeated_verb_are_split(
    question: str, expected: list[str]
) -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))

    plan = await planner.plan(question)

    assert plan is not None
    assert [task.question for task in plan.tasks] == expected
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_ranked_products_then_related_entities_is_a_dependent_plan() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))

    plan = await planner.plan(
        "找出销售额下降最大的五个产品，并列出涉及的经销商和医院。"
    )

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "找出销售额下降最大的五个产品",
        "列出上述产品涉及的经销商和医院",
    ]
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_named_report_facets_are_split_for_combined_report() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))

    plan = await planner.plan(
        "生成上海地区最近一年销售分析报告，包含趋势、排名、异常和结论。"
    )

    assert plan is not None
    assert plan.final_deliverable == "COMBINED_REPORT"
    assert [task.question for task in plan.tasks] == [
        "分析上海地区最近一年销售趋势",
        "分析上海地区最近一年销售排名",
        "分析上海地区最近一年销售异常",
    ]


@pytest.mark.asyncio
async def test_multi_facet_report_is_split_into_independent_evidence_queries() -> None:
    planner = MultiQuestionPlanner(
        Settings(env="test", multi_question_model_enabled=False)
    )
    plan = await planner.plan(
        "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势、"
        "医院覆盖、合作经销商数据，输出分析报告"
    )

    assert plan is not None
    assert plan.final_deliverable == "COMBINED_REPORT"
    assert [task.question for task in plan.tasks] == [
        "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势，使用数据源全部可用历史",
        "统计上海市紫杉醇释放冠脉球囊导管已合作医院数，使用数据源全部可用历史",
        "统计上海市紫杉醇释放冠脉球囊导管已合作经销商数，使用数据源全部可用历史",
    ]
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_multi_facet_report_preserves_explicit_lists_as_detail_queries() -> None:
    planner = MultiQuestionPlanner(
        Settings(env="test", multi_question_model_enabled=False)
    )

    plan = await planner.plan(
        "分析上海市某产品销售趋势、合作经销商名单，输出分析报告"
    )

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "分析上海市某产品销售趋势，使用数据源全部可用历史",
        "列出上海市某产品合作经销商名单明细，使用数据源全部可用历史",
    ]


@pytest.mark.asyncio
async def test_inline_query_and_difference_becomes_dependent_chain() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan("查询2026年6月和7月销售额并计算差额")
    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "查询2026年6月和7月销售额，分别按月返回", "计算差额",
    ]
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_relation_lookup_then_filter_becomes_dependent_chain() -> None:
    planner = MultiQuestionPlanner(
        Settings(env="test", multi_question_model_enabled=False)
    )
    plan = await planner.plan(
        "匹配无线蓝牙耳机所属商品分类，并根据商品分类筛选出供应商"
    )

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "匹配无线蓝牙耳机所属商品分类",
        "根据商品分类筛选出供应商",
    ]
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_multiple_inline_post_calculations_form_a_sequential_chain() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan("查询各区域销售额并找出最高和最低再计算差额")
    assert plan is not None
    assert [task.depends_on for task in plan.tasks] == [[], ["task-1"], ["task-2"]]


@pytest.mark.asyncio
async def test_contextual_metric_addition_and_unit_price_are_split() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan("再加订单量，算客单价谁高")
    assert plan is not None
    assert [task.question for task in plan.tasks] == ["再加订单量", "算客单价谁高"]
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_contextual_first_task_reuses_root_conversation() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    seen: list[tuple[str, str]] = []

    class RecordingOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            seen.append((chat.question, chat.conversation_id))
            return await super()._handle(chat, identity)

    service = RecordingOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=MultiQuestionPlanner(settings),
    )
    await service.handle(
        ChatRequest(semantic_model_id=81,
            conversation_id="business-context", message_id="m1",
            question="再加订单量，算客单价谁高", application_id="app",
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
    )
    assert seen[0] == ("再加订单量", "business-context")
    assert seen[1][1] == "business-context"


@pytest.mark.asyncio
async def test_dependent_new_entity_does_not_reuse_predecessor_dataset() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    seen: list[ChatRequest] = []

    class RecordingOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            seen.append(chat)
            if len(seen) == 1:
                return AgentResponse(
                    request_id=uuid4(), conversation_id=chat.conversation_id,
                    status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
                    answer="商品名称=无线蓝牙耳机，商品分类=数码产品",
                    dataset_id="dataset-products",
                    reliability=ReliabilityReport(
                        level="HIGH", score=1, gates={"query": True}
                    ),
                )
            return await super()._handle(chat, identity)

    service = RecordingOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=MultiQuestionPlanner(settings),
    )
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="匹配耳机所属商品分类"),
        AtomicTask(
            # This task changes entity but does not claim to reuse predecessor
            # values; exact dependency-result filtering is covered separately.
            task_id="task-2", question="另外查询供应商名单",
            depends_on=["task-1"],
        ),
    ])
    await service._handle_task_plan(
        ChatRequest(semantic_model_id=81,
            conversation_id="entity-transition", message_id="m1",
            question="ignored", application_id="app",
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
        plan,
    )

    assert seen[1].conversation_id != seen[0].conversation_id
    assert seen[1].dataset_id is None


@pytest.mark.asyncio
async def test_structured_model_plan_is_schema_validated() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"is_multi_question":true,"tasks":[
              {"question":"查询本月销售额","depends_on":[]},
              {"question":"解释退款率口径","depends_on":[]}
            ]}
            """}}]
        })

    settings = Settings(
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = MultiQuestionPlanner(
        settings, transport=httpx.MockTransport(handler)
    )
    plan = await planner.plan("查询本月销售额；另外解释退款率口径")
    assert plan is not None
    assert plan.planner == "STRUCTURED_MODEL"
    assert [task.question for task in plan.tasks] == [
        "查询本月销售额", "解释退款率口径"
    ]


@pytest.mark.asyncio
async def test_model_plan_cannot_invent_another_time_or_metric() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"is_multi_question":true,"tasks":[
              {"question":"查询上月销售额","depends_on":[]},
              {"question":"分析本月退款率","depends_on":[]}
            ]}
            """}}]
        })
    settings = Settings(
        env="test", intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = MultiQuestionPlanner(settings, transport=httpx.MockTransport(handler))
    # The structured plan is rejected and the deterministic fallback keeps the
    # original constraints instead of executing invented SQL.
    plan = await planner.plan("查询本月销售额；另外分析销售额趋势")
    assert plan is not None
    assert all("上月" not in task.question and "退款率" not in task.question for task in plan.tasks)


@pytest.mark.asyncio
async def test_model_plan_cannot_silently_drop_an_analysis_goal() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"is_multi_question":true,"tasks":[
              {"question":"查询本月销售额","depends_on":[]},
              {"question":"查询本月订单量","depends_on":[]}
            ]}
            """}}]
        })
    settings = Settings(
        env="test", intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = MultiQuestionPlanner(settings, transport=httpx.MockTransport(handler))

    plan = await planner.plan("查询本月销售额并分析趋势；另外查询本月订单量")

    assert plan is not None
    assert plan.planner == "DETERMINISTIC_RULE"
    assert any("趋势" in task.question for task in plan.tasks)


def test_plan_validation_rejects_dropped_exclusion_constraint() -> None:
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询本月销售额"),
        AtomicTask(task_id="task-2", question="查询本月订单量"),
    ])

    with pytest.raises(TaskPlanningError, match="排除或限定条件"):
        MultiQuestionPlanner.validate(
            plan,
            source_question="查询本月销售额但排除退款；另外查询本月订单量",
        )


@pytest.mark.asyncio
async def test_rule_plan_inherits_single_shared_metric_and_period():
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan("查询本月销售额；另外分析趋势")
    assert plan is not None
    assert plan.tasks[1].question.endswith("指标为销售额，时间范围为本月")


@pytest.mark.asyncio
async def test_rule_plan_does_not_overwrite_a_second_implicit_metric() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = await planner.plan("查询销售额；另外查询库存")
    assert plan is not None
    assert "指标为销售额" not in plan.tasks[1].question


def test_dag_rejects_forward_dependency() -> None:
    plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        tasks=[
            AtomicTask(task_id="task-1", question="查询销售额", depends_on=["task-2"]),
            AtomicTask(task_id="task-2", question="分析销售趋势"),
        ],
    )
    with pytest.raises(TaskPlanningError, match="排在其前面"):
        MultiQuestionPlanner.validate(plan)


class _Classifier:
    def classify(self, question, identity, conversation_id):
        raise AssertionError("the stub orchestrator overrides _handle")


class _StubOrchestrator(DataAnalysisOrchestrator):
    async def _handle(self, chat, identity):
        await __import__("asyncio").sleep(0)
        return AgentResponse(
            request_id=uuid4(),
            conversation_id=chat.conversation_id,
            status="COMPLETED",
            intent=PrimaryIntent.METRIC_QUERY,
            answer=f"完成：{chat.question}",
            reliability=ReliabilityReport(
                level="HIGH", score=1, gates={"query": True}
            ),
        )


@pytest.mark.asyncio
async def test_orchestrator_executes_and_aggregates_independent_tasks() -> None:
    settings = Settings(
        env="test",
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    service = _StubOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=MultiQuestionPlanner(settings),
    )
    response = await service.handle(
        ChatRequest(semantic_model_id=81,
            conversation_id="multi",
            message_id="m1",
            question="查询本月销售额；另外解释退款率口径",
            application_id="app",
            history=[HistoryMessage(
                role="user",
                content="历史问题",
                created_at=datetime.now(timezone.utc),
            )],
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
    )
    assert response.status == "COMPLETED"
    assert response.intent_source == "TASK_DAG"
    assert response.execution_shape == "COMPOSITE"
    assert response.task_intents == [
        PrimaryIntent.METRIC_QUERY,
        PrimaryIntent.METRIC_QUERY,
    ]
    assert response.task_plan is not None
    assert len(response.task_results) == 2
    assert all(item.status == "COMPLETED" for item in response.task_results)
    assert response.reliability is not None
    assert response.reliability.level == "HIGH"


@pytest.mark.asyncio
async def test_non_report_composite_response_keeps_every_dataset_id() -> None:
    class DatasetStubOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            response = await super()._handle(chat, identity)
            suffix = "primary" if "主要" in chat.question else "secondary"
            response.dataset_id = f"ds-{suffix}"
            return response

    settings = Settings(
        env="test",
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    service = DatasetStubOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=MultiQuestionPlanner(settings),
    )

    response = await service.handle(
        ChatRequest(semantic_model_id=81,
            conversation_id="multi-datasets",
            message_id="m1",
            question="查询 TDC-3 产品的主要适用科室、次要适用科室",
            application_id="app",
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
    )

    assert response.execution_shape == "COMPOSITE"
    assert response.dataset_ids == ["ds-primary", "ds-secondary"]
    assert [item.dataset_id for item in response.task_results] == [
        "ds-primary", "ds-secondary",
    ]


def test_composite_markdown_fallback_uses_sections_not_nested_tables() -> None:
    results = [
        TaskExecutionResult(
            task_id="task-1",
            question="查询主要适用科室",
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="共 1 条。\n\n| 商品 | 适用科室 |\n| --- | --- |\n| A | 急诊科 |",
        ),
        TaskExecutionResult(
            task_id="task-2",
            question="查询次要适用科室",
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="共 1 条。\n\n| 商品 | 适用科室 |\n| --- | --- |\n| A | 儿科 |",
        ),
    ]

    answer = DataAnalysisOrchestrator._task_result_summary_table(results)

    assert answer.count("### ") == 2
    assert "| 查询目标 | 结果内容 |" not in answer
    assert "\\|" not in answer
    assert "<br>" not in answer
    assert "| 商品 | 适用科室 |" in answer


def test_homogeneous_composite_datasets_merge_with_facet_column() -> None:
    plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        tasks=[
            AtomicTask(
                task_id="task-1",
                question="查询 TDC-3 产品的主要适用科室",
            ),
            AtomicTask(
                task_id="task-2",
                question="查询 TDC-3 产品的次要适用科室",
            ),
        ],
    )
    results = [
        TaskExecutionResult(
            task_id=task.task_id,
            question=task.question,
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="unused",
            dataset_id=f"ds-{index}",
        )
        for index, task in enumerate(plan.tasks, 1)
    ]
    datasets = {
        "task-1": (
            ["商品", "适用科室"],
            [{"商品": "一次性使用喉镜片", "适用科室": "急诊科"}],
        ),
        "task-2": (
            # Same semantic fields in a different SQL projection order must
            # still be treated as one compatible result schema.
            ["适用科室", "商品"],
            [{"商品": "一次性使用喉镜片", "适用科室": "儿科"}],
        ),
    }

    answer = DataAnalysisOrchestrator._render_homogeneous_task_datasets(
        "查询 TDC-3 产品的主要适用科室、次要适用科室",
        plan,
        results,
        datasets,
    )

    assert answer is not None
    assert "规格型号 = `TDC-3`" in answer
    assert "| 商品名称 | 适用类型 | 适用科室 |" in answer
    assert "| 一次性使用喉镜片 | 主要适用 | 急诊科 |" in answer
    assert "| 一次性使用喉镜片 | 次要适用 | 儿科 |" in answer
    assert "\\|" not in answer
    assert "<br>" not in answer


@pytest.mark.asyncio
async def test_composite_report_keeps_all_dataset_evidence_and_exports_sections() -> None:
    settings = Settings(
        env="test",
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    sessions = InMemorySessionStore(7200, 7200)

    class CompositeExporter:
        def __init__(self):
            self.sections = []

        def export_many(self, sections, **kwargs):
            self.sections = list(sections)
            now = datetime.now(timezone.utc).isoformat()
            return {
                "report_id": "report-composite",
                "format": kwargs["file_format"],
                "object_name": "data-analysis/reports/report-composite.xlsx",
                "download_url": "http://minio/report-composite.xlsx",
                "byte_size": 456,
                "object_expires_at": now,
                "report_reference": {
                    "report_id": "report-composite",
                    "object_name": "data-analysis/reports/report-composite.xlsx",
                    "expires_at": now,
                },
            }

        def delete_object(self, object_name):
            raise AssertionError(f"unexpected cleanup: {object_name}")

    exporter = CompositeExporter()

    class CompositeOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            index = (
                1 if "趋势" in chat.question
                else 2 if "已合作医院数" in chat.question
                else 3
            )
            dataset_id = f"dataset-{index}"
            now_dt = datetime.now(timezone.utc)
            reference = DatasetReference(
                dataset_id=dataset_id,
                bucket="bam",
                object_name=f"datasets/{dataset_id}.json",
                scope=DatasetScope(
                    identity.tenant_id,
                    identity.user_id,
                    chat.application_id,
                    chat.conversation_id,
                    chat.authorized_semantic_scope.fingerprint(),
                ),
                columns=("维度", "数值"),
                row_count=1,
                byte_size=10,
                snapshot_id=f"snapshot-{index}",
                data_as_of=now_dt.isoformat(),
                created_at=now_dt.isoformat(),
                expires_at=(now_dt + timedelta(hours=1)).isoformat(),
                source_type="DATABASE_QUERY",
                source_ref="test",
            )
            await sessions.put_dataset_reference(reference.to_dict(), recent_limit=10)
            return AgentResponse(
                request_id=uuid4(),
                conversation_id=chat.conversation_id,
                status="COMPLETED",
                intent=(
                    PrimaryIntent.TREND_ANALYSIS
                    if index == 1 else PrimaryIntent.DETAIL_QUERY
                ),
                answer=f"章节{index}完成",
                dataset_id=dataset_id,
                evidence=[EvidenceItem(
                    evidence_id=f"query-{index}",
                    kind="QUERY_RESULT",
                    source_ref="test",
                    payload={
                        "row_count": 1,
                        "truncated": False,
                        "result_fingerprint": f"snapshot-{index}",
                    },
                )],
                reliability=ReliabilityReport(
                    level="HIGH", score=1, gates={"query": True}
                ),
            )

    service = CompositeOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
        task_planner=MultiQuestionPlanner(settings),
        report_exporter=exporter,
    )
    response = await service.handle(
        ChatRequest(semantic_model_id=81,
            conversation_id="composite-report",
            message_id="m1",
            question=(
                "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势、"
                "医院覆盖、合作经销商数据，输出分析报告"
            ),
            application_id="app",
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
    )

    assert response.status == "COMPLETED"
    assert response.intent == PrimaryIntent.REPORT_GENERATION
    assert response.dataset_id is None
    assert response.dataset_ids == ["dataset-1", "dataset-2", "dataset-3"]
    manifest = next(
        item for item in response.evidence
        if item.kind == "REPORT_DATASET_MANIFEST"
    )
    assert manifest.payload["section_count"] == 3
    assert len(manifest.payload["sections"]) == 3
    assert len(exporter.sections) == 3
    assert response.files[0].dataset_ids == response.dataset_ids


@pytest.mark.asyncio
async def test_dependency_failure_skips_downstream_but_preserves_sibling() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    planner = MultiQuestionPlanner(settings)
    service = _StubOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=planner,
    )

    async def failing_handle(chat, identity):
        if "库存" in chat.question:
            raise RuntimeError("boom")
        return await _StubOrchestrator._handle(service, chat, identity)

    service._handle = failing_handle
    plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        tasks=[
            AtomicTask(task_id="task-1", question="查询库存异常"),
            AtomicTask(task_id="task-2", question="分析销售趋势"),
            AtomicTask(
                task_id="task-3",
                question="基于上述结果分析原因",
                depends_on=["task-1"],
            ),
        ],
    )
    chat = ChatRequest(semantic_model_id=81,
        conversation_id="multi", message_id="m2",
        question="ignored", application_id="app",
    )
    response = await service._handle_task_plan(
        chat, TrustedIdentity(tenant_id="t", user_id="u"), plan
    )
    statuses = {item.task_id: item.status for item in response.task_results}
    assert statuses == {"task-1": "FAILED", "task-2": "COMPLETED", "task-3": "SKIPPED"}
    assert response.status == "PARTIAL_SUCCESS"


@pytest.mark.asyncio
async def test_dependent_new_entity_query_inherits_bounded_upstream_dimension_values() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)

    class DependencyDatasetStore:
        def __init__(self):
            self.reference = None
            self.rows = (
                {
                    "product_name": "产品A",
                    "department.dept_code": "DEP001",
                    "department.dept_name": "超声科",
                },
                {
                    "product_name": "产品A",
                    "department.dept_code": "DEP002",
                    "department.dept_name": "麻醉科",
                },
            )

        def load_dataset(self, reference, *, current_scope):
            assert reference == self.reference
            assert current_scope == reference.scope
            return SimpleNamespace(reference=reference, rows=self.rows)

    store = DependencyDatasetStore()
    seen_children: list[ChatRequest] = []

    class DependencyOrchestrator(_StubOrchestrator):
        async def _handle(self, child, identity):
            seen_children.append(child)
            if "适用科室" in child.question and "经销商" not in child.question:
                now = datetime.now(timezone.utc)
                store.reference = DatasetReference(
                    dataset_id="dataset-departments",
                    bucket="bam",
                    object_name="datasets/departments.json",
                    scope=DatasetScope(
                        identity.tenant_id, identity.user_id,
                        child.application_id, child.conversation_id,
                        child.authorized_semantic_scope.fingerprint(),
                    ),
                    columns=(
                        "product_name",
                        "department.dept_code",
                        "department.dept_name",
                    ),
                    row_count=2,
                    byte_size=50,
                    snapshot_id="snapshot-departments",
                    data_as_of=now.isoformat(),
                    created_at=now.isoformat(),
                    expires_at=(now + timedelta(hours=1)).isoformat(),
                    source_type="DATABASE_QUERY",
                    source_ref="query-departments",
                )
                await sessions.put_dataset_reference(
                    store.reference.to_dict(), recent_limit=10
                )
                return AgentResponse(
                    request_id=uuid4(), conversation_id=child.conversation_id,
                    status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
                    answer="已返回产品及其适用科室。",
                    dataset_id=store.reference.dataset_id,
                    evidence=[EvidenceItem(
                        evidence_id="departments", kind="QUERY_RESULT",
                        source_ref="test", payload={"row_count": 2},
                    )],
                    reliability=ReliabilityReport(
                        level="HIGH", score=1, gates={"query": True}
                    ),
                )
            assert child.dependency_constraints
            constraint = child.dependency_constraints[0]
            assert constraint.source_column == "department.dept_code"
            assert constraint.values == ["DEP001", "DEP002"]
            return AgentResponse(
                request_id=uuid4(), conversation_id=child.conversation_id,
                status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
                answer="已按适用科室返回经销商。",
                evidence=[EvidenceItem(
                    evidence_id="dealers", kind="QUERY_RESULT",
                    source_ref="test", payload={"row_count": 1},
                )],
                reliability=ReliabilityReport(
                    level="HIGH", score=1, gates={"query": True}
                ),
            )

    service = DependencyOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
        task_planner=MultiQuestionPlanner(settings),
        dataset_store=store,
    )
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="匹配产品A适用科室"),
        AtomicTask(
            task_id="task-2",
            question="根据适用科室筛选出经销商",
            depends_on=["task-1"],
        ),
    ])

    response = await service._handle_task_plan(
        ChatRequest(
            conversation_id="dependency-filter", message_id="m1",
            question="ignored", application_id="app", semantic_model_id=81,
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
        plan,
    )

    assert response.status == "COMPLETED"
    assert "Internal DAG join requirement" in seen_children[0].question
    assert "department" in seen_children[0].question
    dependency_evidence = next(
        item for item in response.evidence if item.kind == "DEPENDENCY_CONSTRAINT"
    )
    assert dependency_evidence.payload["values"] == ["DEP001", "DEP002"]
    assert dependency_evidence.payload["scope_verified"] is True


@pytest.mark.asyncio
async def test_ambiguous_dependency_dimension_fails_closed_without_running_downstream() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)

    class AmbiguousStore:
        def __init__(self):
            self.reference = None
            self.rows = ({"region_name": "华东", "category_name": "耗材"},)

        def load_dataset(self, reference, *, current_scope):
            assert current_scope == reference.scope
            return SimpleNamespace(reference=reference, rows=self.rows)

    store = AmbiguousStore()
    calls: list[str] = []

    class GuardedOrchestrator(_StubOrchestrator):
        async def _handle(self, child, identity):
            calls.append(child.question)
            now = datetime.now(timezone.utc)
            store.reference = DatasetReference(
                dataset_id="dataset-ambiguous", bucket="bam",
                object_name="datasets/ambiguous.json",
                scope=DatasetScope(
                    identity.tenant_id, identity.user_id,
                    child.application_id, child.conversation_id,
                    child.authorized_semantic_scope.fingerprint(),
                ),
                columns=("region_name", "category_name"), row_count=1,
                byte_size=30, snapshot_id="s", data_as_of=now.isoformat(),
                created_at=now.isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
                source_type="DATABASE_QUERY", source_ref="q",
            )
            await sessions.put_dataset_reference(store.reference.to_dict(), recent_limit=10)
            return AgentResponse(
                request_id=uuid4(), conversation_id=child.conversation_id,
                status="COMPLETED", intent=PrimaryIntent.DETAIL_QUERY,
                answer="已返回结果。", dataset_id=store.reference.dataset_id,
                evidence=[EvidenceItem(
                    evidence_id="source", kind="QUERY_RESULT",
                    source_ref="test", payload={"row_count": 1},
                )],
                reliability=ReliabilityReport(
                    level="HIGH", score=1, gates={"query": True}
                ),
            )

    service = GuardedOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
        dataset_store=store,
    )
    response = await service._handle_task_plan(
        ChatRequest(semantic_model_id=81,
            conversation_id="dependency-ambiguous", message_id="m1",
            question="ignored", application_id="app",
        ),
        TrustedIdentity(tenant_id="t", user_id="u"),
        TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
            AtomicTask(task_id="task-1", question="查询区域与分类"),
            AtomicTask(
                task_id="task-2", question="根据上述结果筛选供应商",
                depends_on=["task-1"],
            ),
        ]),
    )

    assert calls == ["查询区域与分类"]
    assert response.status == "PARTIAL_SUCCESS"
    rejected = next(
        item for item in response.evidence
        if item.kind == "DEPENDENCY_CONSTRAINT_REJECTED"
    )
    assert rejected.payload["sql_executed"] is False


def test_identical_queries_are_merged_but_aliases_are_retained() -> None:
    planner = MultiQuestionPlanner(Settings(env="test", multi_question_model_enabled=False))
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="请查询 本月销售额"),
        AtomicTask(task_id="task-2", question="帮我查询本月销售额。"),
        AtomicTask(task_id="task-3", question="查询库存"),
    ])
    execution, aliases = planner.deduplicate(plan)
    assert [task.task_id for task in execution.tasks] == ["task-1", "task-3"]
    assert aliases == {"task-2": "task-1"}


@pytest.mark.asyncio
async def test_completed_checkpoint_is_reused_after_service_restart() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    planner = MultiQuestionPlanner(settings)
    calls: list[str] = []

    class CountingOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            calls.append(chat.question)
            return await super()._handle(chat, identity)

    service = CountingOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=planner,
    )
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="查询销售额"),
        AtomicTask(task_id="task-2", question="查询库存"),
    ])
    chat = ChatRequest(semantic_model_id=81, conversation_id="resume", message_id="resume-1", question="ignored", application_id="app")
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    fingerprint = __import__("hashlib").sha256(plan.model_dump_json().encode()).hexdigest()
    completed = await service._handle(
        ChatRequest(semantic_model_id=81, conversation_id="old", message_id="old", question="查询销售额", application_id="app"),
        identity,
    )
    calls.clear()
    await sessions.put_dag_checkpoint("t", "u", "app", "resume", "resume-1", {
        'authorized_scope': chat.authorized_semantic_scope.fingerprint(),
        "schema_version": "1.0", "plan_fingerprint": fingerprint,
        "completed": {"task-1": completed.model_dump(mode="json")},
        "conversations": {"task-1": "old"},
    })
    response = await service._handle_task_plan(chat, identity, plan)
    assert calls == ["查询库存"]
    assert response.status == "COMPLETED"


@pytest.mark.asyncio
async def test_single_dag_clarification_resumes_on_root_conversation_without_replanning() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    calls: list[str] = []

    class ClarifyingOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            calls.append(chat.question)
            if "销售额" in chat.question:
                return AgentResponse(
                    request_id=uuid4(), conversation_id=chat.conversation_id,
                    status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                    answer="请选择时间范围", clarification_questions=["请选择时间范围"],
                    missing_slots=["time_range"],
                )
            return await super()._handle(chat, identity)

    service = ClarifyingOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="root-resume", message_id="m1",
        question="查询销售额；另外查询库存", application_id="app",
        semantic_model_id=8,
    ), identity)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.dag_resume_token
    assert first.awaiting_task_ids == ["task-1"]
    calls_before_resume = list(calls)

    second = await service.handle(ChatRequest(
        conversation_id="root-resume", message_id="m2", question="本月",
        application_id="app", semantic_model_id=8,
        dag_resume_token=first.dag_resume_token,
    ), identity)
    assert second.status == "COMPLETED"
    assert calls == calls_before_resume + ["本月"]
    assert second.dag_resume_token is None


@pytest.mark.asyncio
async def test_multiple_dag_clarifications_require_explicit_task_answer_mapping() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    calls: list[str] = []

    class ClarifyingOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            calls.append(chat.question)
            if chat.question not in {"本月", "上周"}:
                return AgentResponse(
                    request_id=uuid4(), conversation_id=chat.conversation_id,
                    status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                    answer="请选择时间范围", clarification_questions=["请选择时间范围"],
                    missing_slots=["time_range"],
                )
            return await super()._handle(chat, identity)

    service = ClarifyingOrchestrator(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="root-multiple", message_id="m1",
        question="查询销售额；另外查询库存", application_id="app",
        semantic_model_id=8,
    ), identity)
    assert first.awaiting_task_ids == ["task-1", "task-2"]
    call_count = len(calls)

    ambiguous = await service.handle(ChatRequest(
        conversation_id="root-multiple", message_id="m2", question="本月和上周",
        application_id="app", semantic_model_id=8,
        dag_resume_token=first.dag_resume_token,
    ), identity)
    assert ambiguous.status == "NEEDS_CLARIFICATION"
    assert len(calls) == call_count

    completed = await service.handle(ChatRequest(
        conversation_id="root-multiple", message_id="m3", question="补充各子任务",
        application_id="app", semantic_model_id=8,
        dag_resume_token=first.dag_resume_token,
        task_answers={"task-1": "本月", "task-2": "上周"},
    ), identity)
    assert completed.status == "COMPLETED"
    assert calls[-2:] == ["本月", "上周"]


@pytest.mark.asyncio
async def test_report_branches_share_one_time_clarification_and_resume_together() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    calls: list[str] = []
    follow_up = "三个分析维度都按2025年10月17日至2025年12月30日"

    class SharedTimeReportOrchestrator(_StubOrchestrator):
        async def _handle(self, chat, identity):
            calls.append(chat.question)
            if chat.question == follow_up:
                return await super()._handle(chat, identity)
            intent = (
                PrimaryIntent.TREND_ANALYSIS
                if "趋势" in chat.question
                else PrimaryIntent.DETAIL_QUERY
            )
            return AgentResponse(
                request_id=uuid4(), conversation_id=chat.conversation_id,
                status="NEEDS_CLARIFICATION", intent=intent,
                answer="请选择时间范围。",
                clarification_questions=["要查询或分析哪个时间范围？"],
                clarification_items=[ClarificationItem(
                    slot="time_range",
                    title="时间范围",
                    question="要查询或分析哪个时间范围？",
                    options=["本月", "上月", "最近30天", "自定义起止日期"],
                )],
                missing_slots=["time_range"],
            )

    service = SharedTimeReportOrchestrator(
        settings=settings,
        classifier=_Classifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
        task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="shared-report-time",
        message_id="m1",
        question=(
            "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势、"
            "医院覆盖、合作经销商数据，输出分析报告"
        ),
        application_id="app",
        semantic_model_id=81,
    ), identity)

    assert first.status == "NEEDS_CLARIFICATION"
    assert first.intent == PrimaryIntent.REPORT_GENERATION
    assert first.awaiting_task_ids == ["task-1", "task-2", "task-3"]
    assert first.missing_slots == ["shared:time_range"]
    assert len(first.clarification_questions) == 1
    assert first.clarification_items[0].slot == "shared:time_range"
    assert first.answer.count("要查询或分析哪个时间范围") == 1
    assert first.dag_resume_token

    second = await service.handle(ChatRequest(
        conversation_id="shared-report-time",
        message_id="m2",
        question=follow_up,
        application_id="app",
        semantic_model_id=81,
    ), identity)

    assert second.status == "COMPLETED"
    assert second.intent == PrimaryIntent.REPORT_GENERATION
    assert second.awaiting_task_ids == []
    assert calls[-3:] == [follow_up, follow_up, follow_up]


@pytest.mark.asyncio
async def test_real_report_classifier_resumes_all_facets_with_one_period() -> None:
    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        multi_question_model_enabled=False,
        analysis_synthesis_enabled=False,
    )
    sessions = InMemorySessionStore(7200, 7200)
    service = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=sessions,
        task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="real-shared-report-time",
        message_id="m1",
        question=(
            "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势、"
            "医院覆盖、合作经销商数据，输出分析报告"
        ),
        application_id="app",
        semantic_model_id=81,
    ), identity)

    assert first.intent == PrimaryIntent.REPORT_GENERATION
    assert first.status in {"COMPLETED", "PARTIAL_SUCCESS"}
    assert all(result.status != "NEEDS_CLARIFICATION" for result in first.task_results)
    manifest = next(
        item for item in first.evidence if item.kind == "REPORT_DATASET_MANIFEST"
    )
    assert manifest.payload["section_count"] == 3
    root_token = __import__("hashlib").sha256(
        b"real-shared-report-time:m1"
    ).hexdigest()[:16]
    for index in range(2, 4):
        restored = await sessions.get_last_request(
            "t", "u", "app", f"dag-{root_token}-task-{index}"
        )
        assert restored is not None
        assert restored.time_range is not None
        assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in restored.assumptions
        assert "TRANSACTION_TIME_SCOPE=SALES_RECORD" in restored.assumptions
        assert {item["value"] for item in restored.filters} == {
            "上海市", "紫杉醇释放冠脉球囊导管",
        }


@pytest.mark.asyncio
async def test_dag_resume_fails_closed_when_semantic_scope_changes() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)

    class AlwaysClarify(_StubOrchestrator):
        async def _handle(self, chat, identity):
            return AgentResponse(
                request_id=uuid4(), conversation_id=chat.conversation_id,
                status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                answer="请补充", clarification_questions=["请补充"],
                missing_slots=["time_range"],
            )

    service = AlwaysClarify(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="scope-change", message_id="m1",
        question="查询销售额；另外查询库存", application_id="app",
        semantic_model_id=8,
    ), identity)
    changed = await service.handle(ChatRequest(
        conversation_id="scope-change", message_id="m2", question="本月",
        application_id="app", semantic_model_id=9,
        dag_resume_token=first.dag_resume_token,
    ), identity)
    assert changed.status == "SAFE_FALLBACK"
    assert "不适用于本次访问范围" in changed.answer


@pytest.mark.asyncio
async def test_dag_resume_rejects_an_explicit_wrong_token() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)

    class AlwaysClarify(_StubOrchestrator):
        async def _handle(self, chat, identity):
            return AgentResponse(
                request_id=uuid4(), conversation_id=chat.conversation_id,
                status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                answer="请补充", clarification_questions=["请补充"],
                missing_slots=["time_range"],
            )

    service = AlwaysClarify(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="wrong-token", message_id="m1",
        question="查询销售额；另外查询库存", application_id="app",
        semantic_model_id=8,
    ), identity)
    assert first.dag_resume_token

    rejected = await service.handle(ChatRequest(
        conversation_id="wrong-token", message_id="m2", question="本月",
        application_id="app", semantic_model_id=8,
        dag_resume_token="x" * 32,
    ), identity)
    assert rejected.status == "SAFE_FALLBACK"
    assert "令牌与当前受信会话状态不匹配" in rejected.answer


@pytest.mark.asyncio
async def test_concurrent_dag_resume_executes_child_only_once() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    answer_calls = 0

    class SlowResume(_StubOrchestrator):
        async def _handle(self, chat, identity):
            nonlocal answer_calls
            if chat.question == "本月":
                answer_calls += 1
                await asyncio.sleep(0.05)
                return await super()._handle(chat, identity)
            if "销售额" in chat.question:
                return AgentResponse(
                    request_id=uuid4(), conversation_id=chat.conversation_id,
                    status="NEEDS_CLARIFICATION", intent=PrimaryIntent.METRIC_QUERY,
                    answer="请选择时间", clarification_questions=["请选择时间"],
                    missing_slots=["time_range"],
                )
            return await super()._handle(chat, identity)

    service = SlowResume(
        settings=settings, classifier=_Classifier(), adapters=build_mock_adapters(),
        sessions=sessions, task_planner=MultiQuestionPlanner(settings),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    first = await service.handle(ChatRequest(
        conversation_id="concurrent-resume", message_id="m1",
        question="查询销售额；另外查询库存", application_id="app",
        semantic_model_id=8,
    ), identity)
    requests = [
        ChatRequest(
            conversation_id="concurrent-resume", message_id=message_id,
            question="本月", application_id="app", semantic_model_id=8,
            dag_resume_token=first.dag_resume_token,
        )
        for message_id in ("m2", "m3")
    ]
    results = await asyncio.gather(*(service.handle(item, identity) for item in requests))
    assert answer_calls == 1
    assert sorted(item.status for item in results) == ["COMPLETED", "SAFE_FALLBACK"]
