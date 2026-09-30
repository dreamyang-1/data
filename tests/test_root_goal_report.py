import json
from types import SimpleNamespace

import pytest

from app.analysis.synthesis import QwenAnalysisSynthesizer
from app.domain.models import AtomicTask, TaskPlan, TaskExecutionResult
from app.presentation.root_report import collect_root_materials, render_root_report, result_table_title
from test_analysis_synthesis import settings, transport_for


def materials():
    return [
        {"task_id": "numerator", "question": "已合作医院数", "status": "COMPLETED",
         "presentation": {"table": "| 已合作医院数 |\n| --- |\n| 5 |"}},
        {"task_id": "denominator", "question": "区域医院总数", "status": "COMPLETED",
         "presentation": {"table": "| 区域医院总数 |\n| --- |\n| 10 |"}},
        {"task_id": "coverage", "question": "覆盖率", "status": "COMPLETED",
         "presentation": {"table": "| 覆盖率 |\n| --- |\n| 50% |", "chart": "<svg>existing-chart</svg>"}},
    ]


@pytest.mark.parametrize("ids", [["coverage"], ["numerator", "denominator"]])
def test_final_selects_user_deliverables_not_all_tasks_or_only_last_task(ids):
    answer, selected = render_root_report("用户明确需要的结果", materials(),
        {"overview": "整体简短回答", "result_task_ids": ids})
    assert selected == ids
    for item in materials():
        assert (item["presentation"]["table"] in answer) == (item["task_id"] in ids)
    assert answer.count("### 1、概况总结") == 1
    assert "任务1" not in answer


@pytest.mark.parametrize("final", [None, {}, {"result_task_ids": ["invented"]}, {"result_task_ids": ["coverage", "invented"]}])
def test_bad_or_missing_model_selection_preserves_results_without_gate(final):
    answer, selected = render_root_report("整体问题", materials(), final)
    assert selected == ["numerator", "denominator", "coverage"]
    assert "50%" in answer


def test_partial_results_and_preview_limits_survive_selection():
    items = materials()[:1] + [{"task_id": "failed", "question": "区域医院总数",
        "status": "FAILED", "summary": "服务暂不可用"}]
    items[0]["query_results"] = [{"truncated": True, "returned_row_count": 20, "row_count": 700}]
    answer, selected = render_root_report("覆盖率", items, {"result_task_ids": ["numerator"]})
    assert "服务暂不可用" in answer and "20条预览" in answer
    assert "不能作为全量统计" in answer and selected == ["numerator"]
    assert "0%" not in answer


def test_clarification_without_any_query_results_returns_only_actionable_explanation():
    items = [
        {
            "task_id": "metric",
            "question": "统计各经销商的合作时长",
            "status": "NEEDS_CLARIFICATION",
            "summary": (
                "目录中未找到名为‘合作时长’的指标，因此目前无法生成可执行查询；"
                "请补充合作时长的计算口径。"
            ),
            "missing_information": ["请补充合作时长的计算口径。"],
            "depends_on": [],
        },
        {
            "task_id": "filter",
            "question": "筛选合作时长大于3个月的经销商",
            "status": "SKIPPED",
            "summary": "依赖任务尚未完成，本任务未执行。",
            "depends_on": ["metric"],
        },
        {
            "task_id": "independent",
            "question": "查询独立数据项",
            "status": "SAFE_FALLBACK",
            "summary": "数据服务暂时不可用。",
            "depends_on": [],
        },
    ]

    answer, selected = render_root_report("列出合作时长大于3个月的经销商名单", items, None)

    assert selected == []
    assert "目录中未找到名为‘合作时长’的指标" in answer
    assert "请补充合作时长的计算口径" in answer
    assert "依赖任务尚未完成" not in answer
    assert "数据服务暂时不可用" in answer
    assert "概况总结" not in answer
    assert "关键发现" not in answer
    assert "业务提示" not in answer
    assert "本次尚未获得可用于回答问题的数据" not in answer


def test_cached_and_empty_branches_are_not_omitted_from_model_material():
    plan = TaskPlan(planner="STRUCTURED_MODEL", analyze_summary="先查两项指标。", tasks=[
        AtomicTask(task_id="a", question="查询甲项", extraction={"指标": [{"name": "甲项"}]}),
        AtomicTask(task_id="b", question="查询乙项", depends_on=["a"]),
    ])
    results = [TaskExecutionResult(task_id="a", question="查询甲项", status="COMPLETED", answer="没有记录"),
               TaskExecutionResult(task_id="b", question="查询乙项", status="NEEDS_CLARIFICATION", answer="无法判断时间")]
    items = collect_root_materials(plan, results, {}, {})
    assert [item["status"] for item in items] == ["COMPLETED", "NEEDS_CLARIFICATION"]
    assert items[0]["structured_parameters"] == plan.tasks[0].extraction
    assert items[1]["depends_on"] == ["a"]
    answer, _ = render_root_report("整体问题", items, None)
    assert "没有记录" in answer and "无法判断时间" in answer


@pytest.mark.asyncio
async def test_one_call_returns_root_insight_and_separate_final_plan_with_full_context():
    calls = []
    context = {"original_question": "它们呢", "completed_question": "查询上海各经销商覆盖率",
               "planning": {"analyze_summary": "查询分子分母后计算覆盖率。", "tasks": [
                   {"task_id": "a", "depends_on": [], "extraction": {"指标": ["医院数"]}},
                   {"task_id": "b", "depends_on": ["a"], "expected_output": "覆盖率"}]}}
    output = {"claims": [{"statement": "围绕整体目标的分析依据。"}],
              "final_answer": {"overview": "简短回答", "findings": ["关键发现"],
                               "tips": ["预览限制"], "result_task_ids": ["coverage"],
                               "result_titles": {"coverage": "上海各经销商覆盖率"}}}
    text, parsed = await QwenAnalysisSynthesizer(settings(), transport_for(output, calls)).synthesize_combined(
        context["completed_question"], materials(), planning_context=context)
    assert len(calls) == 1 and text == "围绕整体目标的分析依据。"
    assert parsed.final_answer == output["final_answer"]
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert payload["completed_question"] == context["completed_question"]
    assert payload["planning_context"] == context
    assert payload["untrusted_user_question"] == "它们呢"
    assert "先逐项交代" not in calls[0]["messages"][0]["content"]


def test_malformed_optional_final_plan_does_not_discard_insight():
    text = json.dumps({"claims": ["可用分析"], "final_answer": {"overview": 123, "tips": "not-list", "result_task_ids": [1, "a"]}})
    result = QwenAnalysisSynthesizer._parse_report(text, set())
    assert result.claims[0].statement == "可用分析"
    assert result.final_answer == {"result_task_ids": ["a"]}


@pytest.mark.parametrize("final", [None, {}, {"result_task_ids": ["unknown"]}])
def test_missing_final_metadata_shows_computed_deliverable_not_inputs(final):
    items = materials()
    items[2].update(depends_on=["numerator", "denominator"],
                    summary="已计算各经销商覆盖率。", facts={"computation": {"row_count": 2}})
    items[0]["warnings"] = ["中间查询只有20行预览"]
    items[0]["query_results"] = [{"truncated": True, "returned_row_count": 20}]
    items[2]["presentation"]["notes"] = ["完整计算结果已生成，以下为预览。"]
    answer, selected = render_root_report("统计各经销商覆盖率", items, final)
    assert selected == ["coverage"]
    assert "已计算各经销商覆盖率。" in answer
    assert items[0]["presentation"]["table"] not in answer
    assert items[1]["presentation"]["table"] not in answer
    assert "中间查询只有20行预览" not in answer
    assert "完整计算结果已生成" in answer


def test_explicit_requested_inputs_and_computed_output_are_preserved():
    items = materials()
    items[2]["depends_on"] = ["numerator", "denominator"]
    _, selected = render_root_report("同时查询医院数和覆盖率", items,
        {"result_task_ids": ["numerator", "denominator", "coverage"]})
    assert selected == ["numerator", "denominator", "coverage"]


def test_failed_computation_does_not_hide_available_inputs_or_invent_ratio():
    items = materials()
    items[2].update(status="FAILED", depends_on=["numerator", "denominator"], summary="分母口径不一致，未计算")
    answer, selected = render_root_report("覆盖率", items, None)
    assert selected == ["numerator", "denominator"]
    assert "未计算" in answer and "50%" not in answer


@pytest.mark.asyncio
async def test_download_exports_selected_derived_dataset_not_source_tables():
    from dataclasses import replace
    from uuid import uuid4
    from app.config import Settings
    from app.domain.models import AgentResponse, ChatRequest, TrustedIdentity
    from app.services.orchestrator import DataAnalysisOrchestrator
    from app.services.report_export import DatasetReportExporter
    from app.stores import InMemorySessionStore
    from minio_followup_store import DatasetScope
    from test_report_export import reference, Minio, Store
    service = object.__new__(DataAnalysisOrchestrator)
    service.settings = Settings(_env_file=None, env="test")
    service.sessions = InMemorySessionStore()
    service.report_exporter = DatasetReportExporter(Minio(), Store(), bucket="bam")
    chat = ChatRequest(semantic_model_id=81, application_id="app", conversation_id="root", message_id="m", question="覆盖率")
    refs = [replace(reference(), dataset_id=name, scope=DatasetScope("tenant", "user", "app", name, chat.authorized_semantic_scope.fingerprint())) for name in ["source", "derived"]]
    for ref in refs:
        await service.sessions.put_dataset_reference(ref.to_dict(), recent_limit=10)
    responses = {ref.dataset_id: AgentResponse(request_id=uuid4(), conversation_id=ref.scope.conversation_id,
        status="COMPLETED", intent="METRIC_QUERY", answer="结果", dataset_id=ref.dataset_id) for ref in refs}
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[AtomicTask(task_id="source", question="取数"),
        AtomicTask(task_id="derived", question="覆盖率", depends_on=["source"])])
    response = responses["derived"].model_copy(deep=True)
    await service._attach_composite_report(response, chat=chat, identity=TrustedIdentity(tenant_id="tenant", user_id="user"),
        plan=plan, responses=responses, conversation_by_task={"source":"source", "derived":"derived"},
        markdown_link=True, selected_task_ids=["derived"])
    assert response.files[0].dataset_ids == ["derived"]
    assert "附件：" in response.answer


@pytest.mark.asyncio
async def test_computed_result_exposes_its_exact_artifact_for_final_export():
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from uuid import uuid4
    from app.config import Settings
    from app.domain.models import AgentResponse, ChatRequest, TrustedIdentity
    from app.services.orchestrator import DataAnalysisOrchestrator
    from minio_followup_store import LoadedDataset
    from test_report_export import reference
    source = replace(reference(), columns=("经销商", "医院数"))
    denominator = replace(reference(), dataset_id="denominator", row_count=1, columns=("总数",))
    derived = replace(reference(), dataset_id="derived")
    class Store:
        def load_dataset(self, ref, **kwargs):
            return LoadedDataset(ref, ({"总数":10},) if ref.dataset_id == "denominator" else ({"经销商":"甲", "医院数":5},{"经销商":"乙", "医院数":2}))
        def save_dataset(self, **kwargs):
            assert kwargs["rows"][0]["覆盖率"] == "50.00%"
            return derived
    service = object.__new__(DataAnalysisOrchestrator)
    service.settings = Settings(_env_file=None, env="test")
    service.dataset_store = Store()
    service.sessions = SimpleNamespace(put_dataset_reference=AsyncMock())
    service._latest_task_dataset_reference = AsyncMock(side_effect=[source.to_dict(), denominator.to_dict()])
    service._pure_computation_chat_fallback = AsyncMock(return_value=AgentResponse(request_id=uuid4(),conversation_id="test",status="COMPLETED",intent="CHAT",answer="fallback"))
    chat = ChatRequest(semantic_model_id=81, application_id="app", conversation_id="test", message_id="m", question="覆盖率")
    result = await service._respond_pure_computation(chat, TrustedIdentity(tenant_id="tenant",user_id="user"),
        AtomicTask(task_id="final", question="计算覆盖率", depends_on=["source","denominator"]), {})
    assert result.dataset_id == "derived"
    assert "50.00%" in chat._dag_deferred_insight["presentation"]["table"]


@pytest.mark.parametrize("title", ["上海各经销商的已合作医院数及区域医院覆盖率", "上海各经销商的区域医院覆盖率"])
def test_same_merged_data_can_answer_different_user_goals_without_header_generated_titles(title):
    items = materials()[1:]
    items[1]["question"] = "计算区域医院覆盖率"
    items[1]["presentation"] = {
        "table": "| 经销商 | 已合作医院数 | 区域医院覆盖率 |\n| --- | --- | --- |\n| 甲 | 5 | 50% |",
    }
    answer, _ = render_root_report(title, items, {"result_titles": {"coverage": title}})
    assert f"**{title}**" in answer
    assert "**计算区域医院覆盖率**" not in answer
    assert items[1]["presentation"]["table"] in answer


@pytest.mark.parametrize("titles", [None, [], {"coverage": 1}, {"coverage": " "}, {"unknown": "不相关标题"}])
def test_bad_title_metadata_does_not_block_or_reuse_incomplete_calculation_question(titles):
    item = materials()[2]
    item["facts"] = {"computation": {"row_count": 1}}
    assert result_table_title(item, titles) == "综合计算结果"
    answer, _ = render_root_report("整体问题", [materials()[1], item], {"result_titles": titles})
    assert "50%" in answer


def test_independent_task_titles_keep_original_scope_when_model_metadata_missing():
    item = materials()[1]
    item["question"] = "上海地区医院总数"
    assert result_table_title(item, None) == "上海地区医院总数"


def test_optional_title_parsing_does_not_discard_insight():
    parsed = QwenAnalysisSynthesizer._parse_report(json.dumps({"claims": ["可用分析"],
        "final_answer": {"result_titles": {"a": "完整标题\n测试", "b": 1}}}), set())
    assert parsed.final_answer["result_titles"] == {"a": "完整标题 测试"}
    assert parsed.claims[0].statement == "可用分析"


def test_requested_count_and_ratio_can_use_one_combined_result_with_complete_title():
    items = materials()
    items[2]["depends_on"] = ["numerator", "denominator"]
    items[2]["presentation"]["table"] = "| 经销商 | 已合作医院数 | 区域医院覆盖率 |\n| --- | --- | --- |\n| 甲 | 5 | 50% |"
    titles = {"coverage": "上海各经销商的已合作医院数及区域医院覆盖率"}
    answer, selected = render_root_report("查询上海各经销商已合作医院数和覆盖率", items,
        {"result_task_ids": ["coverage"], "result_titles": titles})
    assert selected == ["coverage"]
    assert items[2]["presentation"]["table"] in answer
    assert items[0]["presentation"]["table"] not in answer
    assert items[1]["presentation"]["table"] not in answer
