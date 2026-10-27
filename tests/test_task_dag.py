from __future__ import annotations

import asyncio
import json
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
from app.planning import (
    CompoundQuestionRouter,
    TaskPlanningError,
    deduplicate_plan,
    execution_layers,
    query_fingerprint,
    validate_plan,
    user_metric_phrase,
)
from app.intent import RuleBasedIntentClassifier
from app.services.orchestrator import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.adapters import build_mock_adapters
from minio_followup_store import DatasetReference, DatasetScope


async def _run_plan(router: CompoundQuestionRouter, question: str) -> TaskPlan | None:
    return (await router.route_and_plan(question)).plan


# 参数提取模型调用的默认 mock 响应：合法最小提取，不触发纠错重试
_DEFAULT_EXTRACT_PAYLOAD = (
    '{"intent":"METRIC_QUERY","extraction":{"意图":"统计查询","业务域":[],'
    '"实体":[],"指标":[],"维度":[],"展示字段":[],"过滤条件":[],'
    '"时间粒度":null,"排序":[],"限制":null,"输出要求":null,"是否去重":null}}'
)


@pytest.mark.parametrize(("raw", "expected"), [
    ("是", "是"),
    ("否（基于前序任务结果计算）", "否"),
    (False, "否"),
    ("不确定", None),
])
def test_extraction_normalizes_database_query_route(raw, expected):
    from app.planning.task_dag import SingleQuestionPlanner, extraction_parser_prompt

    result = SingleQuestionPlanner._sanitize_extraction(
        {"是否查询数据库": raw}, intent=PrimaryIntent.METRIC_QUERY
    )

    assert result["是否查询数据库"] == expected
    assert "是否查询数据库" in extraction_parser_prompt()


def test_extraction_drops_default_output_table_and_keeps_limit():
    """输出要求只保留用户明确说出的格式；“默认输出表格”按未声明处理。

    结构化参数中的行数限制必须原样保留，供 ASL 与最终输出消费。
    """
    from app.planning.task_dag import SingleQuestionPlanner

    dropped = SingleQuestionPlanner._sanitize_extraction(
        {"输出要求": "默认输出表格", "限制": 1000}, intent=PrimaryIntent.METRIC_QUERY
    )
    assert dropped["输出要求"] is None
    assert dropped["限制"] == 1000

    kept = SingleQuestionPlanner._sanitize_extraction(
        {"输出要求": "折线图展示", "限制": 1000}, intent=PrimaryIntent.METRIC_QUERY
    )
    assert kept["输出要求"] == "折线图展示"
    assert kept["限制"] == 1000


def _split_handler(
    judge_payload: str,
    extract_payload: "str | dict[str, str] | None" = None,
    captured: dict | None = None,
):
    """构造同时覆盖拆分判断与参数提取两个模型调用点的 mock handler。

    拆分判断器的 system 提示词含“问题拆分判断器”，其余请求按参数提取处理；
    extract_payload 传 dict 时按 user 消息里的关键词给对应提取响应（每个
    子问题独立提取的场景）；captured 只记录提取请求（注入格式与重试反馈的
    验证对象）。
    """
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        system = payload["messages"][0]["content"]
        if "问题拆分判断器" in system:
            content = judge_payload
        else:
            if captured is not None and '你是任务规划器' not in system:
                captured.setdefault("system", system)
                captured.setdefault("user", payload["messages"][1]["content"])
            if isinstance(extract_payload, dict):
                user = payload["messages"][1]["content"]
                content = _DEFAULT_EXTRACT_PAYLOAD
                for keyword, mapped in extract_payload.items():
                    if keyword in user:
                        content = mapped
                        break
            else:
                content = extract_payload if extract_payload is not None else _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return handler


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
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, 
        "1. 查询本月销售额；2. 分析最近半年销售趋势；3. 解释销售额口径"
    )
    assert plan is not None
    assert [task.task_id for task in plan.tasks] == ["task-1", "task-2", "task-3"]
    assert all(not task.depends_on for task in plan.tasks)
    assert len(execution_layers(plan)) == 1


@pytest.mark.asyncio
async def test_rule_planner_keeps_dependent_post_calculations() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, 
        "查询2026年7月销售额；按区域拆分；找出最高和最低区域；再算最高比最低高多少"
    )

    assert plan is not None
    assert len(plan.tasks) == 4
    assert plan.tasks[2].depends_on == ["task-2"]
    assert plan.tasks[3].depends_on == ["task-3"]


@pytest.mark.asyncio
async def test_rule_planner_splits_mixed_query_definition_and_export() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, 
        "2026年7月销售额是多少？另外退款率口径是什么？再导出订单明细"
    )

    assert plan is not None
    assert len(plan.tasks) == 3
    assert "销售额是多少" in plan.tasks[0].question
    assert "退款率口径" in plan.tasks[1].question
    assert "导出订单明细" in plan.tasks[2].question


@pytest.mark.asyncio
async def test_single_continuous_analysis_is_not_split() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    assert await _run_plan(planner, "查询最近半年销售额并分析趋势") is None


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
    planner = CompoundQuestionRouter(
        Settings(env="test", multi_question_model_enabled=False)
    )

    plan = await _run_plan(planner, question)

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
    planner = CompoundQuestionRouter(
        Settings(env="test", multi_question_model_enabled=False)
    )

    assert await _run_plan(planner, question) is None


def test_parallel_facet_validation_requires_every_branch_exactly_once() -> None:
    source = "查询 TDC-3 产品的主要适用科室、次要适用科室"
    invalid = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询TDC-3产品的主要适用科室"),
        AtomicTask(task_id="task-2", question="查询TDC-3产品的主要适用科室"),
    ])

    with pytest.raises(TaskPlanningError, match="并列业务分支"):
        validate_plan(invalid, source_question=source)


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
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))

    plan = await _run_plan(planner, question)

    assert plan is not None
    assert [task.question for task in plan.tasks] == expected
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_ranked_products_then_related_entities_is_a_dependent_plan() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))

    plan = await _run_plan(planner, 
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
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))

    plan = await _run_plan(planner, 
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
    planner = CompoundQuestionRouter(
        Settings(env="test", multi_question_model_enabled=False)
    )
    plan = await _run_plan(planner, 
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
    planner = CompoundQuestionRouter(
        Settings(env="test", multi_question_model_enabled=False)
    )

    plan = await _run_plan(planner, 
        "分析上海市某产品销售趋势、合作经销商名单，输出分析报告"
    )

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "分析上海市某产品销售趋势，使用数据源全部可用历史",
        "列出上海市某产品合作经销商名单明细，使用数据源全部可用历史",
    ]


@pytest.mark.asyncio
async def test_inline_query_and_difference_becomes_dependent_chain() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, "查询2026年6月和7月销售额并计算差额")
    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "查询2026年6月和7月销售额，分别按月返回", "计算差额",
    ]
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_relation_lookup_then_filter_becomes_dependent_chain() -> None:
    planner = CompoundQuestionRouter(
        Settings(env="test", multi_question_model_enabled=False)
    )
    plan = await _run_plan(planner, 
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
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, "查询各区域销售额并找出最高和最低再计算差额")
    assert plan is not None
    assert [task.depends_on for task in plan.tasks] == [[], ["task-1"], ["task-2"]]


@pytest.mark.asyncio
async def test_contextual_metric_addition_and_unit_price_are_split() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, "再加订单量，算客单价谁高")
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
        task_planner=CompoundQuestionRouter(settings),
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
        task_planner=CompoundQuestionRouter(settings),
    )
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="匹配耳机所属商品分类"),
        AtomicTask(
            # This task changes entity but does not claim to reuse predecessor
            # values; exact dependency-result filtering is covered separately.
            task_id="task-2", question="另外查询供应商名单",
            depends_on=["task-1"],
            extraction={"依赖绑定": []},
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
    calls = []
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        system_prompt = body["messages"][0]["content"]
        if "问题拆分判断器" in system_prompt:
            assert "业务任务边界" in system_prompt
            assert "问题内部的任务拆解" in system_prompt
            assert "多个指标、多个展示字段或多个分组维度" in system_prompt
            assert '"split"' in system_prompt
            content = """
            {"split":true,"sub_questions":[
              {"question":"查询本月销售额","intent":"指标查询","depends_on":[]},
              {"question":"解释退款率口径","intent":"解释口径","depends_on":[]}
            ]}
            """
        else:
            content = _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    settings = Settings(
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(
        settings, transport=httpx.MockTransport(handler)
    )
    plan = await _run_plan(planner, "查询本月销售额；另外解释退款率口径")
    assert plan is not None
    assert plan.planner == "STRUCTURED_MODEL"
    assert len(calls) == 3  # one question judgment, then one decomposition per goal
    assert [task.question for task in plan.tasks] == [
        "查询本月销售额", "解释退款率口径"
    ]


@pytest.mark.asyncio
async def test_structured_model_plan_preserves_business_split_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system_prompt = body["messages"][0]["content"]
        if "问题拆分判断器" in system_prompt:
            assert "split_reason_code" in system_prompt
            assert "expected_output" in system_prompt
            assert "shared_conditions" in system_prompt
            content = """
            {"split":true,
             "split_reason_code":"DISTINCT_DELIVERABLES",
             "shared_conditions":["上海市"],
             "sub_questions":[
               {"question":"查询上海市经销商名单","intent":"明细查询","depends_on":[],"expected_output":"经销商名单"},
               {"question":"查询上海市医院名单","intent":"明细查询","depends_on":[],"expected_output":"医院名单"}
             ]}
            """
        else:
            content = _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "查询上海市经销商名单。查询上海市医院名单。")

    assert plan is not None
    assert plan.split_reason_code == "DISTINCT_DELIVERABLES"
    assert plan.shared_conditions == ["上海市"]
    assert [task.expected_output for task in plan.tasks] == ["经销商名单", "医院名单"]


@pytest.mark.asyncio
async def test_structured_model_analyze_summary_is_preserved() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system_prompt = body["messages"][0]["content"]
        if "问题拆分判断器" in system_prompt:
            assert "analyze_summary" in system_prompt
            content = """
            {"split":true,
             "analyze_summary":"拆分为两项子任务，任务2依赖任务1的科室名单。",
             "sub_questions":[
               {"question":"查询TDC-3产品的适用科室","intent":"明细查询","depends_on":[]},
               {"question":"从适用科室中筛选合作医院","intent":"明细查询","depends_on":[0]}
             ]}
            """
        else:
            content = _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "先查询TDC-3产品的适用科室，再从这些科室中筛选合作医院")

    assert plan is not None
    assert plan.analyze_summary == "拆分为两项子任务，任务2依赖任务1的科室名单。"


@pytest.mark.asyncio
async def test_structured_model_dependency_decision_is_preserved() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)['messages'][0]['content']
        content = """
            {"split":true,"sub_questions":[
              {"question":"查询TDC-3产品的适用科室","intent":"明细查询","depends_on":[]},
              {"question":"从TDC-3产品的适用科室中筛选合作医院","intent":"明细查询","depends_on":[0]}
            ]}
            """ if '问题拆分判断器' in system else _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={'choices':[{'message':{'content':content}}]})

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "先查询TDC-3产品的适用科室，再从这些科室中筛选合作医院")

    assert plan is not None
    assert plan.planner == "STRUCTURED_MODEL"
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_invalid_judge_output_falls_back_to_rule_split() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"split":false,"sub_questions":[
              {"question":"查询本月销售额","depends_on":[]},
              {"question":"查询本月订单量","depends_on":[]}
            ]}
            """}}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "查询本月销售额；另外查询本月订单量")

    assert plan is not None
    assert plan.planner == "DETERMINISTIC_RULE"
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_model_judges_period_separated_independent_questions() -> None:
    question = (
        "查询空心纤维血液透析器产品合作的经销商名单。"
        "查询外周插管中心静脉导管合作的医院名单。"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        system = json.loads(request.content)['messages'][0]['content']
        content = """
            {"split":true,"sub_questions":[
              {"question":"查询空心纤维血液透析器产品合作的经销商名单","intent":"明细查询","depends_on":[]},
              {"question":"查询外周插管中心静脉导管合作的医院名单","intent":"明细查询","depends_on":[]}
            ]}
            """ if '问题拆分判断器' in system else _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={'choices':[{'message':{'content':content}}]})

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, question)

    assert plan is not None
    assert plan.planner == "STRUCTURED_MODEL"
    assert [task.question for task in plan.tasks] == [
        "查询空心纤维血液透析器产品合作的经销商名单",
        "查询外周插管中心静脉导管合作的医院名单",
    ]
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_valid_model_single_task_decision_is_authoritative() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": '{"split":false,"sub_questions":[]}'
            }}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    assert await _run_plan(planner, "查询最近半年销售额；然后分析趋势") is None


@pytest.mark.asyncio
async def test_model_single_decision_does_not_remove_proven_dependency_plan() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {
                "content": '{"split":false,"sub_questions":[]}'
            }}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, 
        "找出销售额下降最大的五个产品，并列出涉及的经销商和医院。"
    )

    assert plan is not None
    assert plan.planner == "DETERMINISTIC_RULE"
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_disabled_model_never_calls_multi_question_transport() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled model transport must not be called")

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_enabled=False,
        intent_model_api_key=SecretStr("configured-but-disabled"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "查询本月销售额；另外查询本月订单量")

    assert plan is not None
    assert plan.planner == "DETERMINISTIC_RULE"


@pytest.mark.asyncio
async def test_period_separated_queries_have_deterministic_failure_fallback() -> None:
    planner = CompoundQuestionRouter(Settings(
        _env_file=None,
        env="test",
        multi_question_model_enabled=False,
    ))
    question = (
        "查询空心纤维血液透析器产品合作的经销商名单。"
        "查询外周插管中心静脉导管合作的医院名单。"
    )

    plan = await _run_plan(planner, question)

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "查询空心纤维血液透析器产品合作的经销商名单",
        "查询外周插管中心静脉导管合作的医院名单",
    ]
    assert all(not task.depends_on for task in plan.tasks)


@pytest.mark.asyncio
async def test_single_continuous_query_with_trailing_period_is_not_split() -> None:
    planner = CompoundQuestionRouter(Settings(
        _env_file=None,
        env="test",
        multi_question_model_enabled=False,
    ))

    assert await _run_plan(planner, "查询最近半年销售额并分析趋势。") is None


@pytest.mark.asyncio
async def test_explicit_result_reference_has_deterministic_dependency_fallback() -> None:
    planner = CompoundQuestionRouter(Settings(
        _env_file=None,
        env="test",
        multi_question_model_enabled=False,
    ))

    plan = await _run_plan(planner, 
        "先查询TDC-3产品的适用科室，再从这些科室中筛选合作医院"
    )

    assert plan is not None
    assert [task.question for task in plan.tasks] == [
        "先查询TDC-3产品的适用科室",
        "从这些科室中筛选合作医院，业务对象为TDC-3",
    ]
    assert plan.tasks[0].depends_on == []
    assert plan.tasks[1].depends_on == ["task-1"]


@pytest.mark.asyncio
async def test_model_plan_cannot_invent_another_time_or_metric() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"split":true,"sub_questions":[
              {"question":"查询上月销售额","depends_on":[]},
              {"question":"分析本月退款率","depends_on":[]}
            ]}
            """}}]
        })
    settings = Settings(
        env="test", intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))
    # The structured plan is rejected and the deterministic fallback keeps the
    # original constraints instead of executing invented SQL.
    plan = await _run_plan(planner, "查询本月销售额；另外分析销售额趋势")
    assert plan is not None
    assert all("上月" not in task.question and "退款率" not in task.question for task in plan.tasks)


@pytest.mark.asyncio
async def test_model_plan_cannot_silently_drop_an_analysis_goal() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": """
            {"split":true,"sub_questions":[
              {"question":"查询本月销售额","depends_on":[]},
              {"question":"查询本月订单量","depends_on":[]}
            ]}
            """}}]
        })
    settings = Settings(
        env="test", intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    plan = await _run_plan(planner, "查询本月销售额并分析趋势；另外查询本月订单量")

    assert plan is not None
    assert plan.planner == "DETERMINISTIC_RULE"
    assert any("趋势" in task.question for task in plan.tasks)


def test_plan_validation_rejects_dropped_exclusion_constraint() -> None:
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询本月销售额"),
        AtomicTask(task_id="task-2", question="查询本月订单量"),
    ])

    with pytest.raises(TaskPlanningError, match="排除或限定条件"):
        validate_plan(
            plan,
            source_question="查询本月销售额但排除退款；另外查询本月订单量",
        )


def test_plan_validation_allows_distinct_identifiers_in_distinct_tasks() -> None:
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询TDC-3产品合作的经销商"),
        AtomicTask(task_id="task-2", question="查询ABC-4产品合作的医院"),
    ])

    validate_plan(
        plan,
        source_question="查询TDC-3产品合作的经销商。查询ABC-4产品合作的医院。",
    )


def test_plan_validation_rejects_invented_or_dropped_business_identifier() -> None:
    invented = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询TDC-3产品合作的经销商"),
        AtomicTask(task_id="task-2", question="查询XYZ-3产品合作的医院"),
    ])
    dropped = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="查询TDC-3产品合作的经销商"),
        AtomicTask(task_id="task-2", question="查询合作医院"),
    ])
    source = "查询TDC-3产品合作的经销商。查询ABC-3产品合作的医院。"

    with pytest.raises(TaskPlanningError, match="不存在的业务型号或编码"):
        validate_plan(invented, source_question=source)
    with pytest.raises(TaskPlanningError, match="遗漏原问题中的业务型号或编码"):
        validate_plan(dropped, source_question=source)


def test_plan_validation_accepts_normalized_date_spellings() -> None:
    # 口语“25年9月”与规范“2025年9月”“2025年09月”语义等价，不算编造数字。
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="统计2025年9月新华医院各销售员的销售额，按金额降序取最高"),
        AtomicTask(task_id="task-2", question="查询2025年09月该销售员销售的产品明细",
                   depends_on=["task-1"]),
    ])
    validate_plan(
        plan, source_question="25年9月新华医院销售额最高的销售员是谁？销售了哪些产品？",
    )


def test_plan_validation_tolerates_derived_numbers_instead_of_rejecting() -> None:
    # 2026-10-21 用户裁决：拆分中的数字改写（去年同期→2024）不再拦截整个
    # 计划，只留诊断日志；拆错了就继续执行，由下游按结构化参数校验。
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(task_id="task-1", question="统计2026年9月新华医院各销售员的销售额"),
        AtomicTask(task_id="task-2", question="查询2026年9月该销售员销售的产品明细",
                   depends_on=["task-1"]),
    ])
    validate_plan(
        plan, source_question="25年9月新华医院销售额最高的销售员是谁？销售了哪些产品？",
    )


def test_result_operation_coerces_safe_count_spellings() -> None:
    # 2026-10-21 用户裁决：行数限制写成 '前2名'/2.0 等可安全归一化的
    # 字面量时直接换算为整数，不因字面类型差异拒绝整个结果操作。
    from app.planning.calculation_bindings import (
        normalize_result_operation, validate_result_operation)

    op = normalize_result_operation({'type': 'limit', 'count': '前2名'})
    validate_result_operation(op)
    assert op['count'] == 2
    op = normalize_result_operation(
        {'type': 'sort_limit', 'field': '含税销售总额', 'limit': 2.0, 'descending': True})
    validate_result_operation(op)
    assert op['count'] == 2
    with pytest.raises(ValueError, match='结果限制'):
        validate_result_operation(normalize_result_operation({'type': 'limit', 'count': '全部'}))


@pytest.mark.asyncio
async def test_computation_binding_failure_degrades_instead_of_blocking(monkeypatch) -> None:
    # 2026-10-21 用户裁决：重试耗尽的计算绑定不再拦停整个计划。
    # 无效的结果操作被丢弃后按原样继续执行（拆错了就拆错了），
    # 派生列空缺由执行与洞察环节兜底。
    from app.planning import SingleQuestionPlanner
    from app.planning.task_dag import DecomposedTask, QuestionDecomposition

    # 2026-10-22 宽松化后缺字段 sort/sort_limit 已被容忍（丢弃/降级），
    # 降级机制改用真实的未知操作类型触发，保持“重试耗尽不拦停”覆盖。
    bad = {'是否查询数据库': '否',
           '结果操作': {'operation': {'type': 'self_sql', 'sql': 'SELECT 1'}}}
    decomposition = QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY, tasks=(
        DecomposedTask(question='查询2025年各经销商销售排名',
                       structured={'意图': '统计查询'}),
        DecomposedTask(question='提取第一名和第二名经销商名称，不再查询数据库',
                       depends_on=(0,), structured=bad),
    ))
    settings = Settings(
        _env_file=None, env='test',
        intent_model_api_key=SecretStr('test-key'),
        multi_question_model_enabled=True,
    )
    planner = SingleQuestionPlanner(settings)

    async def decompose(*args, **kwargs):
        return decomposition

    monkeypatch.setattr(planner, '_model_decompose', decompose)
    result = await planner.plan_question(
        '先给出2025年各经销商销售排名，再对比第一名与第二名去年同期的差距。'
    )
    assert [task.question for task in result.tasks] == [
        '查询2025年各经销商销售排名',
        '提取第一名和第二名经销商名称，不再查询数据库',
    ]
    assert result.tasks[1].structured.get('结果操作') is None


def _yoy_decomposition(compute_question: str, time_range: str = '2025年'):
    from app.planning.task_dag import DecomposedTask, QuestionDecomposition

    return QuestionDecomposition(intent=PrimaryIntent.COMPARISON_ANALYSIS, tasks=(
        DecomposedTask(
            question='查询2025年各经销商的销售额',
            structured={'意图': '统计查询', '指标': [{'name': '销售额'}], '维度': ['经销商'],
                        '时间粒度': {'unit': 'year', 'time_range': time_range, 'anchor': None}}),
        DecomposedTask(question=compute_question, depends_on=(0,)),
    ))


@pytest.mark.parametrize(("compute_question", "expected_label"), [
    ('基于2025年和2024年各经销商的销售额计算同比增长率，不再查询数据库', '2024年'),
    ('计算2025年销售额相对去年同期的同比增长率，不再查询数据库', '2024年'),
    ('对比2025年各经销商销售额与上一年销售额计算增长率，不再查询数据库', '2024年'),
    ('基于2025年11月和去年同期各经销商的销售额计算同比增长率，不再查询数据库', '2024年11月'),
])
def test_collapse_synthesizes_base_period_retrieval_for_yoy_computation(
        compute_question, expected_label) -> None:
    # 折叠"基于2025年和2024年计算同比增长率"类计算任务时，必须为现有取数
    # 未覆盖的基期合成独立取数任务：基期查询即使返回空也如实呈现"基期无
    # 数据"，而不是基期从未被查询。
    from app.planning.task_dag import _collapse_analysis_computation_tasks

    result = _collapse_analysis_computation_tasks(
        _yoy_decomposition(compute_question))

    assert [task.question for task in result.tasks] == [
        '查询2025年各经销商的销售额',
        f'查询{expected_label}各经销商的销售额',
    ]
    baseline = result.tasks[1]
    assert baseline.structured['时间粒度']['time_range'] == expected_label
    assert baseline.structured['指标'] == [{'name': '销售额'}]
    assert baseline.structured['维度'] == ['经销商']
    assert f'{expected_label}（时间范围）' in baseline.parameters
    assert baseline.depends_on == ()
    assert baseline.structured is not result.tasks[0].structured
    # 当前期间取数任务保持原样
    assert result.tasks[0].structured['时间粒度']['time_range'] == '2025年'
    assert result.intent == PrimaryIntent.COMPARISON_ANALYSIS


@pytest.mark.parametrize(("compute_question", "kept_time_range"), [
    ('计算2025年各经销商销售额的环比增长率并排名，不再查询数据库', '2025年'),
    ('基于2025年和2024年各经销商的销售额计算同比增长率，不再查询数据库', '2024年至2025年'),
])
def test_collapse_single_period_computation_keeps_existing_behavior(
        compute_question, kept_time_range) -> None:
    # 单期间计算任务（无对比诉求或期间已被取数覆盖）的折叠行为保持不变；
    # 计算任务未声明可解析基期时不猜测，维持原单取数折叠结果。
    from app.planning.task_dag import _collapse_analysis_computation_tasks

    result = _collapse_analysis_computation_tasks(
        _yoy_decomposition(compute_question, time_range=kept_time_range))

    assert [task.question for task in result.tasks] == ['查询2025年各经销商的销售额']
    assert result.tasks[0].structured['时间粒度']['time_range'] == kept_time_range


def test_collapse_unresolvable_baseline_phrase_does_not_invent_period() -> None:
    # 相对基期锚定不了任何绝对期间时不产出基期任务（不猜年份）。
    from app.planning.task_dag import (
        DecomposedTask, QuestionDecomposition, _collapse_analysis_computation_tasks)

    decomposition = QuestionDecomposition(intent=PrimaryIntent.COMPARISON_ANALYSIS, tasks=(
        DecomposedTask(
            question='查询各经销商的销售额',
            structured={'意图': '统计查询', '指标': [{'name': '销售额'}], '维度': ['经销商']}),
        DecomposedTask(question='计算各经销商销售额的同比变化，不再查询数据库', depends_on=(0,)),
    ))
    result = _collapse_analysis_computation_tasks(decomposition)

    assert [task.question for task in result.tasks] == ['查询各经销商的销售额']


@pytest.mark.asyncio
async def test_plan_question_keeps_base_period_retrieval_after_collapse(monkeypatch) -> None:
    # 端到端复现：规划器把同比问题拆成"取数+计算同比增长率"两个任务时，
    # 折叠丢弃计算任务后必须保留2024基期取数任务，基期不能从未被查询。
    from app.planning import SingleQuestionPlanner

    decomposition = _yoy_decomposition(
        '基于2025年和2024年各经销商的销售额计算同比增长率，不再查询数据库')
    settings = Settings(
        _env_file=None, env='test',
        intent_model_api_key=SecretStr('test-key'),
        multi_question_model_enabled=True,
    )
    planner = SingleQuestionPlanner(settings)

    async def decompose(*args, **kwargs):
        return decomposition

    monkeypatch.setattr(planner, '_model_decompose', decompose)
    result = await planner.plan_question('2025年各经销商的销售额同比增长率是多少？')

    assert len(result.tasks) == 2
    assert result.tasks[0].structured['时间粒度']['time_range'] == '2025年'
    assert result.tasks[1].structured['时间粒度']['time_range'] == '2024年'
    assert result.tasks[1].depends_on == ()
    assert result.intent == PrimaryIntent.COMPARISON_ANALYSIS


@pytest.mark.asyncio
async def test_rule_plan_inherits_single_shared_metric_and_period():
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, "查询本月销售额；另外分析趋势")
    assert plan is not None
    assert plan.tasks[1].question.endswith("指标为销售额，时间范围为本月")


@pytest.mark.asyncio
async def test_rule_plan_does_not_overwrite_a_second_implicit_metric() -> None:
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = await _run_plan(planner, "查询销售额；另外查询库存")
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
        validate_plan(plan)


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
        task_planner=CompoundQuestionRouter(settings),
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
        task_planner=CompoundQuestionRouter(settings),
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


def test_merged_composite_answers_render_single_reply_and_extract_links() -> None:
    results = [
        TaskExecutionResult(
            task_id="task-1",
            question="查询上海地区各经销商的已合作医院数",
            status="COMPLETED",
            intent=PrimaryIntent.METRIC_QUERY,
            answer=(
                "| 经销商名称 | 已合作医院数 |\n| --- | --- |\n"
                "| 国药集团上海公司 | 49 |\n\n"
                "附件：[下载完整查询结果（XLSX）](http://files/ds-1.xlsx)"
            ),
            dataset_id="ds-1",
        ),
        TaskExecutionResult(
            task_id="task-2",
            question="查询上海地区的区域全部医院总数",
            status="COMPLETED",
            intent=PrimaryIntent.METRIC_QUERY,
            answer=(
                "| 区域全部医院总数 |\n| --- |\n| 121 |\n\n"
                "附件：[下载完整查询结果（XLSX）](http://files/ds-2.xlsx)"
            ),
            dataset_id="ds-2",
        ),
    ]

    answer, links = DataAnalysisOrchestrator._render_merged_task_answers(results)

    assert answer is not None and links is not None
    # 单值结果转成「问题：值」一行，明细表保留在加粗问题小标题下
    assert "**查询上海地区的区域全部医院总数**：121" in answer
    assert "**查询上海地区各经销商的已合作医院数**" in answer
    assert "| 经销商名称 | 已合作医院数 |" in answer
    # 不再按任务分节，附件行全部摘出由调用方统一处理
    assert "### ◉ 任务" not in answer
    assert "附件：" not in answer
    assert links == [
        "[下载完整查询结果（XLSX）](http://files/ds-1.xlsx)",
        "[下载完整查询结果（XLSX）](http://files/ds-2.xlsx)",
    ]


def test_merged_composite_answers_fall_back_when_child_answer_missing() -> None:
    results = [
        TaskExecutionResult(
            task_id="task-1",
            question="查询主要适用科室",
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="| 商品 | 适用科室 |\n| --- | --- |\n| A | 急诊科 |",
        ),
        TaskExecutionResult(
            task_id="task-2",
            question="查询次要适用科室",
            status="COMPLETED",
            intent=PrimaryIntent.DETAIL_QUERY,
            answer="",
        ),
    ]

    assert DataAnalysisOrchestrator._render_merged_task_answers(results) == (None, None)


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
        task_planner=CompoundQuestionRouter(settings),
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
    planner = CompoundQuestionRouter(settings)
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
        task_planner=CompoundQuestionRouter(settings),
        dataset_store=store,
    )
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="匹配产品A适用科室"),
        AtomicTask(
            task_id="task-2",
            question="根据适用科室筛选出经销商",
            depends_on=["task-1"],
            extraction={"依赖绑定": [{"source_task_id": "task-1", "source_column": "department.dept_code"}]},
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
    assert seen_children[0].question == "匹配产品A适用科室"
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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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
    planner = CompoundQuestionRouter(Settings(env="test", multi_question_model_enabled=False))
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="请查询 本月销售额"),
        AtomicTask(task_id="task-2", question="帮我查询本月销售额。"),
        AtomicTask(task_id="task-3", question="查询库存"),
    ])
    execution, aliases = deduplicate_plan(plan)
    assert [task.task_id for task in execution.tasks] == ["task-1", "task-3"]
    assert aliases == {"task-2": "task-1"}


@pytest.mark.asyncio
async def test_completed_checkpoint_is_reused_after_service_restart() -> None:
    settings = Settings(env="test", multi_question_model_enabled=False)
    sessions = InMemorySessionStore(7200, 7200)
    planner = CompoundQuestionRouter(settings)
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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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
    saved = await sessions.get_dag_pending("t", "u", "app", "root-resume")
    assert saved["analysis_root_context"]["completed_question"] == "查询销售额；另外查询库存"
    assert len(saved["analysis_root_context"]["planning"]["tasks"]) == 2
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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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
        task_planner=CompoundQuestionRouter(settings),
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
        task_planner=CompoundQuestionRouter(settings),
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
        assert restored.time_range is None
        assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" not in restored.assumptions
        assert "TRANSACTION_TIME_SCOPE=SALES_RECORD" in restored.assumptions
        assert {item["value"] for item in restored.filters} == {
            "上海市", "紫杉醇释放冠脉球囊导管",
        }


def test_sales_record_time_is_only_trusted_for_proven_report_facets() -> None:
    """负例：受信销售时间不得外溢到主数据/非组合报告/无销售事实分面。"""
    internal = DataAnalysisOrchestrator._report_task_internal_assumptions

    sales_report = TaskPlan(
        planner="DETERMINISTIC_RULE",
        final_deliverable="COMBINED_REPORT",
        tasks=[
            AtomicTask(task_id="task-1", question="分析产品销售额趋势"),
            AtomicTask(task_id="task-2", question="查询产品适用科室名单"),
            AtomicTask(task_id="task-3", question="统计产品合作医院覆盖数据"),
        ],
    )
    # Positive control: the proven relationship facet keeps the trusted scope.
    assert internal(sales_report, sales_report.tasks[2]) == (
        "TRANSACTION_TIME_SCOPE=SALES_RECORD",
    )
    # Master-data facets (applicable departments) never inherit sales time.
    assert internal(sales_report, sales_report.tasks[1]) == ()

    # A report without any sales-fact facet gets no trusted sales time at all.
    master_report = TaskPlan(
        planner="DETERMINISTIC_RULE",
        final_deliverable="COMBINED_REPORT",
        tasks=[
            AtomicTask(task_id="task-1", question="查询产品适用科室名单"),
            AtomicTask(task_id="task-2", question="统计产品合作医院数量"),
        ],
    )
    assert internal(master_report, master_report.tasks[1]) == ()

    # Non-combined-report plans never receive the assumption.
    parallel_plan = TaskPlan(
        planner="DETERMINISTIC_RULE",
        final_deliverable=None,
        tasks=[
            AtomicTask(task_id="task-1", question="分析产品销售额趋势"),
            AtomicTask(task_id="task-2", question="统计产品合作医院覆盖数据"),
        ],
    )
    assert internal(parallel_plan, parallel_plan.tasks[1]) == ()


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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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
        sessions=sessions, task_planner=CompoundQuestionRouter(settings),
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


@pytest.mark.asyncio
async def test_model_plan_injects_semantic_context_into_system_prompt() -> None:
    captured: dict[str, str] = {}

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(
        _split_handler(
            '{"split":false,"sub_questions":[]}',
            '{"intent":"DETAIL_QUERY","extraction":{"意图":"统计查询",'
            '"业务域":["医药销售域"],"实体":["交易订单"],'
            '"指标":[{"name":"含税销售总额"}],"维度":["时间"],'
            '"展示字段":[],"过滤条件":[],'
            '"时间粒度":{"unit":"年","time_range":"今年"},'
            '"排序":[],"限制":null,"输出要求":"默认输出表格"}}',
            captured,
        )
    ))

    spec = "# 业务语义规范\n指标表：含税销售总额、不含税销售总额"
    route = await planner.route_and_plan("查一下今年销售额", semantic_context=spec)
    outcome = route.single

    assert "平台业务语义规范" in captured["system"]
    assert "含税销售总额" in captured["system"]
    # 国药结构化提取规范整段注入提取提示词。
    assert "结构化提取规范" in captured["system"]
    assert "语义解析器" in captured["system"]
    assert outcome is not None
    assert outcome.single_intent == PrimaryIntent.METRIC_QUERY
    structured = outcome.single_structured
    assert structured is not None
    assert structured["意图"] == "指标查询"
    assert structured["时间粒度"] == {"unit": "年", "time_range": "今年"}
    # parameters由结构化提取JSON派生，兼容mentions通道。
    assert list(outcome.single_parameters) == [
        "交易订单（实体）", "含税销售总额（指标）", "时间（维度）", "今年（时间范围）",
    ]
    extraction = outcome.single_extraction
    assert extraction is not None
    assert extraction.structured == structured


@pytest.mark.asyncio
async def test_model_plan_prefers_remote_description_and_user_keeps_plain_question(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        system = payload["messages"][0]["content"]
        if "问题拆分判断器" in system:
            content = '{"split":false,"sub_questions":[]}'
        else:
            # 提取请求的首轮才是注入格式验证对象；空提取会触发带纠错说明的重试
            captured.setdefault("system", system)
            captured.setdefault("user", payload["messages"][1]["content"])
            content = _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    from app.domain import semantic_description as sd

    async def fake_load(settings, semantic_model_id):
        return {
            "source": "semantic_model_81.md",
            "available": True,
            "content": "# 业务语义规范描述\n指标：医院总数",
        }

    monkeypatch.setattr(sd, "load_semantic_description", fake_load)

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    question = "查一下各区域医院总数"
    await planner.route_and_plan(
        question,
        semantic_context="# 业务语义规范\n提示词粘贴的旧规范段",
        semantic_model_id=81,
    )

    # 远程描述文件进 system，覆盖提示词粘贴段；user 不再重复注入规范。
    assert "医院总数" in captured["system"]
    assert "摘自语义模型描述文件" in captured["system"]
    assert "提示词粘贴的旧规范段" not in captured["system"]
    assert captured["user"] == question


@pytest.mark.asyncio
async def test_model_plan_without_semantic_context_keeps_plain_prompt() -> None:
    captured: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        system = payload["messages"][0]["content"]
        if "问题拆分判断器" in system:
            content = '{"split":false,"sub_questions":[]}'
        else:
            captured["system"] = system
            content = _DEFAULT_EXTRACT_PAYLOAD
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}]
        })

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))

    await planner.route_and_plan("查询最近半年销售额；然后分析趋势")

    # STALE_TEST: live platform descriptions replaced embedded business catalogs.
    # Missing context retains the parser contract without inventing a catalog.
    assert "摘自智能体配置" not in captured["system"]
    assert "结构化提取规范" in captured["system"]
    assert "医药销售域" not in captured["system"]


@pytest.mark.asyncio
async def test_single_task_intent_falls_back_to_chinese_extraction_intent() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(
        _split_handler(
            '{"split":false,"sub_questions":[]}',
            '{"intent":null,"extraction":{"意图":"明细查询",'
            '"业务域":["医药销售域"],"实体":["商品"],'
            '"指标":[],"维度":[],'
            '"展示字段":[{"entity":"科室","field":"科室名称"}],'
            '"过滤条件":[{"field":"商品品牌","op":"=",'
            '"value":["百特"]}],'
            '"时间粒度":{"unit":null,"time_range":null},'
            '"排序":[],"限制":null,"输出要求":"默认输出表格"}}',
        )
    ))

    route = await planner.route_and_plan("请提供百特Prismaflex M60 set使用科室。")
    outcome = route.single

    assert route.plan is None
    assert outcome is not None
    # intent缺失时按中文意图映射兜底。
    assert outcome.single_intent == PrimaryIntent.DETAIL_QUERY
    assert outcome.single_structured is not None
    assert outcome.single_structured["意图"] == "明细查询"
    assert outcome.single_structured["展示字段"] == [
        {"entity": "科室", "field": "科室名称"}
    ]
    assert list(outcome.single_parameters) == [
        "商品（实体）", "百特（商品品牌）",
    ]


@pytest.mark.asyncio
async def test_multi_task_extraction_is_kept_per_task_and_no_data_intent_drops_it() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(
        _split_handler(
            """
            {"split":true,"sub_questions":[
              {"question":"查询百特产品的合作医院名单","depends_on":[]},
              {"question":"解释区域医院覆盖率的口径","depends_on":[]}
            ]}
            """,
            {
                "百特产品": '{"intent":"DETAIL_QUERY","extraction":{"意图":"明细查询",'
                          '"业务域":["医药销售域"],"实体":["医院"],"指标":[],"维度":[],'
                          '"展示字段":[],'
                          '"过滤条件":[{"field":"商品名称","op":"=","value":["百特产品"]}],'
                          '"时间粒度":{"unit":null,"time_range":null},'
                          '"排序":[],"限制":null,"输出要求":"默认输出表格"}}',
                "覆盖率": '{"intent":"METRIC_DEFINITION","extraction":{"意图":"解释口径",'
                         '"业务域":[],"实体":[],"指标":[{"name":"区域医院覆盖率"}],'
                         '"维度":[],"展示字段":[],"过滤条件":[],'
                         '"时间粒度":null,"排序":[],"限制":null,"输出要求":"默认输出表格"}}',
            },
        )
    ))

    plan = await _run_plan(planner, "查询百特产品的合作医院名单。解释区域医院覆盖率的口径。")

    assert plan is not None
    assert plan.tasks[0].extraction is not None
    assert plan.tasks[0].extraction["意图"] == "明细查询"
    assert plan.tasks[0].parameters == ["医院（实体）", "百特产品（商品名称）"]
    # 口径解释任务保留完整提取，只有闲聊类意图才强制为空。
    assert plan.tasks[1].extraction is not None
    assert plan.tasks[1].parameters == ["区域医院覆盖率（指标）"]


def test_extract_semantic_spec_section_cuts_role_header() -> None:
    prompt = (
        "> 定位：业务分析伙伴\n# 角色设定\n不替代报表系统。\n"
        "# 业务语义规范\n## 一、业务域总览\n指标表内容\n"
    )
    from app.planning import extract_semantic_spec_section
    section = extract_semantic_spec_section(prompt)
    assert section.startswith("# 业务语义规范")
    assert "指标表内容" in section
    assert "角色设定" not in section


def test_extract_semantic_spec_section_returns_empty_without_marker() -> None:
    from app.planning import extract_semantic_spec_section
    assert extract_semantic_spec_section("> 定位：业务分析伙伴") == ""
    assert extract_semantic_spec_section("") == ""
    assert extract_semantic_spec_section(None) == ""


@pytest.mark.asyncio
async def test_multi_task_insight_is_combined_once_at_root() -> None:
    """多任务拆分后各子任务不出单独解读，父级合并成一次整体分析。"""
    from app.services.progress import progress_scope

    class CombinedSynthesizer:
        def __init__(self) -> None:
            self.combined_questions: list[str] = []
            self.combined_tasks: list[list[dict]] = []

        async def synthesize(self, *args, **kwargs):
            raise AssertionError("多任务拆分不应再调用单任务解读")

        async def synthesize_combined(self, question, tasks, *, agent_prompt="", planning_context=None, **kwargs):
            self.combined_questions.append(question)
            self.combined_tasks.append(list(tasks))
            assert planning_context["completed_question"] == question
            assert len(planning_context["planning"]["tasks"]) == 2
            from app.analysis.synthesis import SynthesisOutput, SynthesisClaim
            return "两个任务的整体分析结论。", SynthesisOutput(
                claims=[SynthesisClaim(statement="两个任务的整体分析结论。")],
                final_answer={"overview": "针对用户目标的简短回答。", "result_task_ids": ["task-1", "task-2"]},
            )

    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        multi_question_model_enabled=False,
    )
    synthesizer = CombinedSynthesizer()
    service = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=CompoundQuestionRouter(settings),
        analysis_synthesizer=synthesizer,
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    events = []
    with progress_scope(events.append):
        response = await service.handle(ChatRequest(
            conversation_id="dag-combined-insight",
            message_id="m1",
            question="查询 TDC-3 产品的主要适用科室、次要适用科室",
            application_id="app",
            semantic_model_id=81,
        ), identity)

    assert response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
    assert synthesizer.combined_questions == [
        "查询 TDC-3 产品的主要适用科室、次要适用科室"
    ]
    assert [len(tasks) for tasks in synthesizer.combined_tasks] == [2]
    assert response.answer.startswith(("分析结果如下：", "查询结果如下："))
    assert "### 1、概况总结" not in response.answer
    assert "两个任务的整体分析结论。" not in response.answer
    assert "针对用户目标的简短回答。" in response.answer
    child_insights = [
        event for event in events
        if event["stage"] == "INSIGHT_ANALYSIS" and event.get("is_child_task")
    ]
    assert child_insights == []
    root_completed = [
        event for event in events
        if event["stage"] == "INSIGHT_ANALYSIS"
        and event["status"] == "COMPLETED"
        and not event.get("is_child_task")
    ]
    assert len(root_completed) == 1
    assert "整体分析结论" in root_completed[0]["message"]


@pytest.mark.asyncio
async def test_combined_insight_failure_keeps_per_task_results() -> None:
    """整体分析模型不可用时退回各任务结果，不阻塞回复。"""
    from app.services.progress import progress_scope

    class BrokenCombinedSynthesizer:
        async def synthesize(self, *args, **kwargs):
            raise AssertionError("多任务拆分不应再调用单任务解读")

        async def synthesize_combined(self, question, tasks, *, agent_prompt="", planning_context=None, **kwargs):
            raise RuntimeError("model unavailable")

    settings = Settings(
        env="test",
        adapter_mode="mock",
        intent_model_enabled=False,
        multi_question_model_enabled=False,
    )
    service = DataAnalysisOrchestrator(
        settings=settings,
        classifier=RuleBasedIntentClassifier(),
        adapters=build_mock_adapters(),
        sessions=InMemorySessionStore(7200, 7200),
        task_planner=CompoundQuestionRouter(settings),
        analysis_synthesizer=BrokenCombinedSynthesizer(),
    )
    identity = TrustedIdentity(tenant_id="t", user_id="u")
    events = []
    with progress_scope(events.append):
        response = await service.handle(ChatRequest(
            conversation_id="dag-combined-insight-fallback",
            message_id="m1",
            question="查询 TDC-3 产品的主要适用科室、次要适用科室",
            application_id="app",
            semantic_model_id=81,
        ), identity)

    assert response.status in {"COMPLETED", "PARTIAL_SUCCESS"}
    assert "整体分析结论" not in response.answer
    degraded = [
        event for event in events
        if event["stage"] == "INSIGHT_ANALYSIS" and event["status"] == "DEGRADED"
    ]
    assert len(degraded) == 1
    assert "已保留本次可用查询结果" in degraded[0]["message"]


@pytest.mark.asyncio
async def test_user_metric_phrase_extracts_metric_tail() -> None:
    from app.planning.compound_question import user_metric_phrase

    assert user_metric_phrase("查看上海各经销商科室业务匹配度") == "科室业务匹配度"
    assert user_metric_phrase("统计各经销商的合作时长") == "合作时长"
    assert user_metric_phrase("查询今年江苏省的产品动销率") == "产品动销率"
    assert user_metric_phrase("你好") == ""


@pytest.mark.asyncio
async def test_substituted_calculation_plan_is_forced_to_metric_definition() -> None:
    """计算任务的指标在原话里毫无踪影时，强制转成口径解释并保留原文指标名。"""

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(
        _split_handler(
            """
            {"split":true,
             "analyze_summary":"拆分为三项子任务，问题3依赖前两项。",
             "sub_questions":[
               {"question":"查询上海市各经销商的已合作医院数","depends_on":[]},
               {"question":"查询上海市的区域全部医院总数","depends_on":[]},
               {"question":"根据上述问题的查询结果计算各经销商的区域医院覆盖率作为科室业务匹配度，不再查询数据库",
                "depends_on":[0,1]}
             ]}
            """,
            {
                "已合作医院数": '{"intent":"METRIC_QUERY","extraction":{"意图":"统计查询",'
                             '"指标":[{"name":"已合作医院数"}],"维度":["经销商"],'
                             '"实体":["销售订单","经销商"],'
                             '"过滤条件":[{"field":"省份","op":"=","value":["上海"]}]}}',
                "区域全部医院总数": '{"intent":"METRIC_QUERY","extraction":{"意图":"统计查询",'
                                '"指标":[{"name":"区域全部医院总数"}],'
                                '"过滤条件":[{"field":"省份","op":"=","value":["上海"]}]}}',
                "不再查询数据库": '{"intent":"COMPARISON_ANALYSIS","extraction":{"意图":"指标计算",'
                              '"指标":[{"name":"区域医院覆盖率"}]}}',
                "科室业务匹配度": '{"intent":"METRIC_DEFINITION","extraction":{"意图":"解释口径",'
                              '"指标":[{"name":"科室业务匹配度"}],"实体":null,"维度":null}}',
            },
        )
    ))

    route = await planner.route_and_plan("查看上海各经销商科室业务匹配度")
    outcome = route.single

    assert route.plan is None
    assert outcome is not None
    assert outcome.single_intent == PrimaryIntent.METRIC_DEFINITION
    assert outcome.single_parameters == ("科室业务匹配度（指标）",)


@pytest.mark.asyncio
async def test_legitimate_composite_metric_plan_survives_guard() -> None:
    """One business goal can legitimately contain two queries and a calculation."""

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    async def handler(request):
        system = json.loads(request.content)['messages'][0]['content']
        payload = {'split':False,'sub_questions':[]} if '问题拆分判断器' in system else {
            'intent':'METRIC_QUERY','tasks':[
                {'question':'查询上海市各经销商的已合作医院数','depends_on':[],
                 'extraction':{'意图':'统计查询','指标':[{'name':'已合作医院数'}],'维度':['经销商']}},
                {'question':'查询上海市的区域全部医院总数','depends_on':[],
                 'extraction':{'意图':'统计查询','指标':[{'name':'区域全部医院总数'}]}},
                {'question':'根据上述任务的查询结果计算区域医院覆盖率，不再查询数据库','depends_on':[0,1],
                 'extraction':{'意图':'指标计算','指标':[{'name':'区域医院覆盖率'}]}}]}
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(payload,ensure_ascii=False)}}]})
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(handler))
    catalog = ('| 指标 | 同义词 | 依赖指标 |\n| --- | --- | --- |\n'
        '| 已合作医院数 | 无 | 无 |\n| 区域全部医院总数 | 无 | 无 |\n'
        '| 区域医院覆盖率 | 无 | 已合作医院数、区域全部医院总数 |')
    route = await planner.route_and_plan("统计上海市各经销商的区域医院覆盖率", semantic_context=catalog)

    assert route.plan is not None
    assert len(route.plan.tasks) == 3
    assert route.single is None
    assert not route.judgment.is_compound
    assert route.plan.tasks[2].depends_on == ['task-1','task-2']


@pytest.mark.asyncio
async def test_single_task_metric_definition_keeps_user_metric_name() -> None:
    """口径解释单任务必须把用户原文指标名带进参数通道，不能丢给追问门禁。"""

    settings = Settings(
        _env_file=None,
        env="test",
        intent_model_api_key=SecretStr("test-key"),
        multi_question_model_enabled=True,
    )
    planner = CompoundQuestionRouter(settings, transport=httpx.MockTransport(
        _split_handler(
            '{"split":false,"sub_questions":[]}',
            '{"intent":"METRIC_DEFINITION","extraction":{"意图":null,'
            '"指标":[{"name":"科室业务匹配度"}],"实体":null,"维度":null}}',
        )
    ))

    route = await planner.route_and_plan("科室业务匹配度这个指标的口径是什么")
    outcome = route.single

    assert route.plan is None
    assert outcome is not None
    assert outcome.single_intent == PrimaryIntent.METRIC_DEFINITION
    assert outcome.single_parameters == ("科室业务匹配度（指标）",)
    # 口径解释任务保留完整提取，意图键与任务意图行保持同一标签。
    assert outcome.single_structured is not None
    assert outcome.single_structured["指标"] == [{"name": "科室业务匹配度"}]
    assert outcome.single_structured["意图"] == "解释口径"
    extraction = outcome.single_extraction
    assert extraction is not None
    assert extraction.intent == PrimaryIntent.METRIC_DEFINITION
    assert extraction.parameters == ["科室业务匹配度（指标）"]


@pytest.mark.asyncio
async def test_classify_metadata_overlay_keeps_user_metric_name() -> None:
    """分类叠加时口径解释的原文指标名要落到 request.metrics，供语义服务解析。"""
    from app.domain.models import PlannerExtraction as PlannerExtractionModel

    service = object.__new__(DataAnalysisOrchestrator)
    base = CanonicalAnalysisRequest(
        semantic_model_id=81,
        conversation_id="c-metric-overlay",
        tenant_id="t",
        user_id="u",
        application_id="app",
        original_question="科室业务匹配度这个指标的口径是什么",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        missing_slots=[],
    )

    class _StubClassifier:
        def classify(self, *args, **kwargs):
            return base

    service.classifier = _StubClassifier()
    result = await service._classify(
        "科室业务匹配度这个指标的口径是什么",
        TrustedIdentity(tenant_id="t", user_id="u"),
        "c-metric-overlay",
        planner_extraction=PlannerExtractionModel(
            intent=PrimaryIntent.METRIC_DEFINITION,
            parameters=["科室业务匹配度（指标）"],
        ),
    )

    assert result.primary_intent == PrimaryIntent.METRIC_DEFINITION
    assert [metric.input for metric in result.metrics] == ["科室业务匹配度"]


def test_task_name_aligned_with_equality_filter():
    # 规划器偶发把带具体区域限定的任务名写成“各省份”式泛化（可执行
    # question 与结构化过滤仍保留具体地区，SQL 口径正确），任务卡展示名
    # 与抽取自相矛盾会误导用户。兜底只修正展示名，不改结构化参数。
    from app.planning.task_dag import (
        DecomposedTask,
        QuestionDecomposition,
        _align_task_names_with_scope,
    )

    decomposition = QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY, tasks=(
        DecomposedTask(
            question='统计上海地区各经销商的有效订单去重医院数',
            name='统计最近12个月各经销商的有效订单去重医院数',
            structured={'意图': '统计查询', '过滤条件': [
                {'entity': '省份', 'field': '省份名称', 'op': '=', 'value': '上海'}]}),
        DecomposedTask(
            question='统计上海地区的区域全部医院总数',
            name='统计各省份的区域全部医院总数',
            structured={'意图': '统计查询', '过滤条件': [
                {'entity': '省份', 'field': '省份名称', 'op': '=', 'value': '上海'}]}),
        DecomposedTask(
            # 实测规划链路里等值过滤值常落成单元素列表
            question='统计上海地区的区域全部医院总数',
            name='统计各省份的区域全部医院总数',
            structured={'意图': '统计查询', '过滤条件': [
                {'entity': '医院', 'field': '省份', 'op': '=', 'value': ['上海']}]}),
        DecomposedTask(
            question='查询2025年各省份的区域全部医院总数',
            name='查询2025年各省份的区域全部医院总数',
            structured={'意图': '统计查询', '过滤条件': []}),
    ))
    aligned = _align_task_names_with_scope(decomposition)
    # 任务1的泛化词是“各经销商”，等值过滤维度是省份；名称里缺少实际执行的
    # “上海”限定，按结构化条件补上，避免任务描述看不出真实查询范围
    assert aligned.tasks[0].name == '统计最近12个月各经销商的有效订单去重医院数（省份为上海）'
    # 任务2的“各省份”与 省份名称=上海 矛盾，按过滤值修正展示名
    assert aligned.tasks[1].name == '统计上海的区域全部医院总数'
    # 任务3同形态但值为单元素列表，同样修正
    assert aligned.tasks[2].name == '统计上海的区域全部医院总数'
    # 无等值过滤的泛化描述是合法的按维度分组表述，不改写
    assert aligned.tasks[3].name == '查询2025年各省份的区域全部医院总数'
    # 结构化参数与可执行 question 不受影响
    assert aligned.tasks[1].question == '统计上海地区的区域全部医院总数'
    assert aligned.tasks[1].structured['过滤条件'][0]['value'] == '上海'
    assert aligned.tasks[2].structured['过滤条件'][0]['value'] == ['上海']


@pytest.mark.asyncio
async def test_root_intent_replaces_model_intent_in_planning(monkeypatch):
    """根意图下沉：任务拆分与规划不再做意图判断。

    意图识别节点的根意图直接作为问题意图与全部任务意图；
    提取模型输出的意图（含每任务中文意图键）不再采信，
    中文意图键与根意图保持一致。未传根意图时保持既有行为。
    """
    from unittest.mock import AsyncMock

    from app.domain import semantic_description
    from app.planning.task_dag import SingleQuestionPlanner

    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source': 'semantic_model_81.md', 'available': True, 'content': ''}))
    plan_payload = json.dumps(
        {'tasks': [{'question': '查询各经销商的订单数量'}]}, ensure_ascii=False)
    extraction_payload = json.dumps({
        'intent': 'TREND_ANALYSIS',
        'extraction': {'意图': '趋势分析', '指标': [], '维度': [], '展示字段': [],
                       '过滤条件': [], '时间粒度': None, '排序': [], '限制': None,
                       '输出要求': None, '是否去重': None, '是否查询数据库': '是'},
    }, ensure_ascii=False)

    async def model(request):
        payload = json.loads(request.content.decode())
        content = plan_payload if '你是任务规划器' in payload['messages'][0]['content'] else extraction_payload
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    settings = Settings(
        _env_file=None, env='test',
        intent_model_api_key=SecretStr('test-key'),
        multi_question_model_enabled=True,
    )
    planner = SingleQuestionPlanner(settings, transport=httpx.MockTransport(model))

    # 未传根意图：保持既有行为，意图取提取模型输出
    legacy = await planner.plan_question('查询各经销商的订单数量', semantic_model_id=81)
    assert legacy.intent is PrimaryIntent.TREND_ANALYSIS
    assert legacy.tasks[0].intent is PrimaryIntent.TREND_ANALYSIS
    assert legacy.tasks[0].structured['意图'] == '趋势分析'

    # 传入根意图：意图判断被跳过，模型意图输出被忽略
    root = await planner.plan_question(
        '查询各经销商的订单数量', semantic_model_id=81,
        root_intent=PrimaryIntent.METRIC_QUERY)
    assert root.intent is PrimaryIntent.METRIC_QUERY
    assert root.tasks[0].intent is PrimaryIntent.METRIC_QUERY
    assert root.tasks[0].structured['意图'] == '指标查询'


@pytest.mark.asyncio
async def test_root_intent_also_governs_computation_tasks(monkeypatch):
    """计算路由使用查库标记，意图必须沿用识别节点。"""
    from unittest.mock import AsyncMock

    from app.domain import semantic_description
    from app.planning.task_dag import SingleQuestionPlanner

    monkeypatch.setattr(semantic_description, 'load_semantic_description', AsyncMock(return_value={
        'source': 'semantic_model_81.md', 'available': True,
        'content': '| 指标 | 同义词 | 业务口径 | 单位 | 指标层级 | 依赖指标 |\n'
                   '| --- | --- | --- | --- | --- | --- |\n'
                   '| 订单数量 | 订单数 | 订单号去重数 | 笔 | 原子指标 | 无 |\n'}))
    plan_payload = json.dumps({'tasks': [
        {'question': '查询各经销商的订单数量', 'metric_targets': ['订单数量']},
        {'question': '计算订单数量的平均值，不再查询数据库', 'depends_on': [0],
         'metric_targets': ['订单数量'],
         'postprocess': {'operation': {'type': 'aggregate', 'group_by': [],
             'aggregations': [{'field': '订单数量', 'function': 'avg', 'alias': '平均值'}]}}},
    ]}, ensure_ascii=False)
    extraction_payload = json.dumps({'tasks': [
        {'task_index': 0, 'extraction': {'意图': '明细查询', '指标': [], '维度': [],
            '展示字段': [], '过滤条件': [], '时间粒度': None, '排序': [], '限制': None,
            '输出要求': None, '是否去重': None, '是否查询数据库': '是'}},
        {'task_index': 1, 'extraction': {'意图': '明细查询', '指标': [], '维度': [],
            '展示字段': [], '过滤条件': [], '时间粒度': None, '排序': [], '限制': None,
            '输出要求': None, '是否去重': None, '是否查询数据库': '否',
            '结果操作': {'source_task_index': 0, 'operation': {'type': 'aggregate', 'group_by': [],
                'aggregations': [{'field': '订单数量', 'function': 'avg', 'alias': '平均值'}]}}}},
    ]}, ensure_ascii=False)

    async def model(request):
        payload = json.loads(request.content.decode())
        content = plan_payload if '你是任务规划器' in payload['messages'][0]['content'] else extraction_payload
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    settings = Settings(
        _env_file=None, env='test',
        intent_model_api_key=SecretStr('test-key'),
        multi_question_model_enabled=True,
    )
    planner = SingleQuestionPlanner(settings, transport=httpx.MockTransport(model))
    root = await planner.plan_question(
        '查询各经销商的订单数量并计算平均值', semantic_model_id=81,
        root_intent=PrimaryIntent.DETAIL_QUERY)
    assert root.intent is PrimaryIntent.DETAIL_QUERY
    assert root.tasks[0].intent is PrimaryIntent.DETAIL_QUERY
    assert root.tasks[0].structured['意图'] == '明细查询'
    assert root.tasks[1].intent is PrimaryIntent.DETAIL_QUERY
    assert root.tasks[1].structured['意图'] == '明细查询'
    assert root.tasks[1].structured['是否查询数据库'] == '否'
    assert root.tasks[1].structured['结果操作']['operation']['type'] == 'aggregate'
