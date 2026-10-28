"""Offline acceptance of the six chains and target/baseline separation."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.analysis import AnalysisEngine
from app.analysis.chain_tools.temporal_window import resolve_target_window
from app.analysis.requirements import build_requirements
from app.domain.models import ChatRequest, KnowledgeContext, PlannerExtraction, PrimaryIntent, TrustedIdentity
from app.intent import RuleBasedIntentClassifier
from app.planning.analysis_task_planner import build_analysis_task_plan
from app.services import DataAnalysisOrchestrator
from app.services.progress import progress_scope

TODAY = date(2026, 10, 21)
METRIC = "含税销售总额"
IDENTITY = TrustedIdentity(tenant_id="t", user_id="u", roles=[])


def structured(window="2025年10月至12月", unit="月", dimensions=None):
    return {"意图": "趋势分析", "指标": [{"name": METRIC}], "维度": dimensions or [],
        "实体": ["销售订单"], "过滤条件": [], "时间粒度": {"unit": unit, "time_range": window, "anchor": None},
        "排序": [], "是否查询数据库": "是"}


def plan(question="2025年10月到12月的含税销售总额趋势", intent=PrimaryIntent.TREND_ANALYSIS, extraction=None):
    return build_analysis_task_plan(question, PlannerExtraction(intent=intent, parameters=[], structured=extraction or structured()), today=TODAY)


def request_for(task_plan, question):
    task = task_plan.tasks[-1]
    binding = task.extraction["分析绑定"]
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    chat = ChatRequest(conversation_id="local", message_id="m", application_id="app", question=question, semantic_model_id=81)
    request = orchestrator._analysis_task_request(chat, IDENTITY, task, binding)
    return request


@pytest.mark.parametrize("question,intent", [
    ("2025年各月销售额趋势", PrimaryIntent.TREND_ANALYSIS),
    ("2025年各月销售额同比趋势", PrimaryIntent.TREND_ANALYSIS),
    ("对比两个产品销售额", PrimaryIntent.COMPARISON_ANALYSIS),
    ("各产品销售额占比", PrimaryIntent.COMPOSITION_ANALYSIS),
    ("分析销售额异常", PrimaryIntent.ANOMALY_ANALYSIS),
    ("分析销售额下降原因", PrimaryIntent.ROOT_CAUSE_ANALYSIS),
    ("分析销售额下降原因和历史趋势", PrimaryIntent.ROOT_CAUSE_ANALYSIS),
    ("预测未来三个月销售额", PrimaryIntent.FORECAST_ANALYSIS),
])
def test_six_intent_boundaries(question, intent):
    actual = RuleBasedIntentClassifier().classify(question, IDENTITY, "local")
    assert actual.primary_intent == intent


def test_monthly_pop_uses_september_but_every_target_fact_and_chart_excludes_it():
    q = "2025年10月到12月的含税销售总额趋势"
    p = plan(q)
    assert p.tasks[0].extraction["时间粒度"]["time_range"] == "2025-09-01至2025-12-31"
    request = request_for(p, q)
    assert request.time_range.start == date(2025, 10, 1)
    rows = [{"月份": month, METRIC: value} for month, value in [
        ("2025-09", 100000), ("2025-10", 100), ("2025-11", 150), ("2025-12", 200)]]
    result = AnalysisEngine().analyze(request, ["月份", METRIC], rows, KnowledgeContext(query=q))
    facts = result.facts
    assert facts["absolute_change"] == 100
    assert facts["minimum"] == 100 and facts["maximum"] == 200
    assert facts["display_window"]["total"] == 450
    assert facts["pattern"] == "持续上升"
    assert facts["change_points"] == []
    assert facts["period_over_period"]["changes"][0]["from"] == "2025-09"
    assert facts["period_over_period"]["changes"][0]["to"] == "2025-10"
    assert facts["continuous_runs"][0]["start_label"] == "2025-10"
    # STALE_TEST: 默认执行同比，缺基期时保留不可计算状态。
    assert facts["year_over_year"]["status"] == "NOT_COMPUTABLE"
    for chart in facts["chart_specs"]:
        assert "2025-09" not in {str(row.get(chart["x_field"])) for row in chart["data"]}


def test_yoy_has_two_bounded_queries_and_only_target_outputs():
    q = "2025年下半年各月订单笔数同比趋势"
    s = structured("2025年下半年")
    p = plan(q, extraction=s)
    assert len(p.tasks) == 3
    # STALE_TEST: explicit YoY selects no MoM baseline.
    assert p.tasks[0].extraction["时间粒度"]["time_range"] == "2025-07-01至2025-12-31"
    assert p.tasks[1].extraction["时间粒度"]["time_range"] == "2024-07-01至2024-12-31"
    assert p.tasks[-1].depends_on == ["task-1", "task-2"]
    request = request_for(p, q)
    rows = [{"月份": f"{year}-{month:02}", METRIC: 100 if year == 2024 else 120}
            for year in (2025, 2024) for month in range(7, 13)]
    out = AnalysisEngine().analyze(request, ["月份", METRIC], rows, KnowledgeContext(query=q))
    assert out.facts["point_count"] == 6
    # STALE_TEST: explicit YoY does not execute overall or MoM tools.
    assert "absolute_change" not in out.facts
    assert "period_over_period" not in out.facts
    yoy = out.facts["year_over_year"]
    assert yoy["computable_count"] == 6
    assert all(item["change_rate"] == pytest.approx(.2) for item in yoy["changes"])
    assert all(item["label"].startswith("2025") for item in yoy["changes"])
    assert not any("2024-07" in warning for warning in out.warnings)


def test_rolling_year_preserves_day_bounds_and_marks_both_partial_months():
    q = "分析近1年的销售总数量的月度变化趋势"
    p = plan(q, extraction=structured("近1年"))
    assert p.tasks[0].extraction["时间粒度"]["time_range"] == "2025-10-21至2026-10-21"
    req = request_for(p, q)
    months = [(2025, 10), (2025, 11), (2025, 12), *[(2026, m) for m in range(1, 11)]]
    out = AnalysisEngine().analyze(req, ["月份", METRIC],
        [{"月份": f"{year}-{month:02}", METRIC: 100} for year, month in months], KnowledgeContext(query=q))
    assert out.facts["point_count"] == 13
    assert out.facts["edge_bucket_integrity"]["first_bucket_partial"]
    assert out.facts["edge_bucket_integrity"]["last_bucket_partial"]
    changes = out.facts["period_over_period"]["changes"]
    assert not any(item["from"] == "2025-10" or item["to"] == "2026-10" for item in changes)
    assert out.facts["trend_diagnostics"]["basis_labels"] == [f"2025-{m:02}" for m in (11, 12)] + [f"2026-{m:02}" for m in range(1, 10)]
    assert not any(item["from"] == "2025-10" or item["to"] == "2026-10" for item in out.facts["period_changes"])


@pytest.mark.parametrize("unit,window,word", [
    ("日", "2025-10-01至2025-10-03", "分日"),
    ("季度", "2025年第一季度至2025年第四季度", "分季度"),
    ("年", "2023年至2025年", "分年"),
])
def test_grain_is_not_hardcoded_month(unit, window, word):
    p = plan("分析销售趋势", extraction=structured(window, unit))
    assert word in p.tasks[0].question
    assert "分月" not in p.tasks[0].question


def test_filters_anchor_and_dimensions_survive_time_rewrite():
    s = structured(dimensions=["产品"])
    s["时间粒度"]["anchor"] = "发货日期"
    s["过滤条件"] = [{"entity": "经销商", "field": "名称", "op": "=", "value": ["甲公司"]}]
    p = plan(extraction=s)
    text = p.tasks[0].question
    assert all(value in text for value in ("产品", "发货日期", "经销商", "甲公司"))
    assert s["时间粒度"]["time_range"] == "2025年10月至12月"


def test_analysis_request_preserves_dimension_bindings():
    q = "分析2025年第四季度各渠道业态含税销售总额占比"
    p = plan(q, PrimaryIntent.COMPOSITION_ANALYSIS, structured(dimensions=["渠道业态"]))
    request = request_for(p, q)
    assert request.dimensions == ["渠道业态"]
    assert [metric.canonical_name for metric in request.metrics] == [METRIC]


@pytest.mark.parametrize("intent,question", [
    (PrimaryIntent.TREND_ANALYSIS, "分析销售趋势"),
    (PrimaryIntent.COMPARISON_ANALYSIS, "两个产品销售额对比"),
    (PrimaryIntent.COMPOSITION_ANALYSIS, "各产品销售额占比"),
    (PrimaryIntent.ANOMALY_ANALYSIS, "检测销售额异常"),
    (PrimaryIntent.ROOT_CAUSE_ANALYSIS, "分析销售额下降原因"),
    (PrimaryIntent.FORECAST_ANALYSIS, "预测未来2个月销售额"),
])
def test_each_chain_has_its_own_methods_and_dependencies(intent, question):
    p = plan(question, intent)
    task = p.tasks[-1]
    assert task.primary_intent == intent
    req = task.extraction["分析绑定"]["requirements"]
    assert req["intent"] == intent.value and req["methods"]
    assert req["input_shape"] and req["metric_names"] == [METRIC]
    assert task.depends_on == [item.task_id for item in p.tasks[:-1]]
    if intent != PrimaryIntent.TREND_ANALYSIS:
        assert "period_over_period_change" not in req["methods"]
        assert "year_over_year_change" not in req["methods"]


def test_locked_intent_is_not_reclassified_from_question_text():
    p = plan("分析销售额下降原因", PrimaryIntent.TREND_ANALYSIS,
             structured(dimensions=["产品"]))
    assert p.tasks[-1].primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert p.tasks[-1].extraction["分析绑定"]["analysis_type"] == "trend"


@pytest.mark.parametrize("text,expected", [
    ("近1年", (date(2025, 10, 21), TODAY)),
    ("最近12个完整自然月", (date(2025, 10, 1), date(2026, 9, 30))),
    ("2025年下半年", (date(2025, 7, 1), date(2025, 12, 31))),
])
def test_time_semantics(text, expected):
    assert resolve_target_window(text, "month", today=TODAY) == expected


def test_calendar_year_clamps_leap_day():
    assert resolve_target_window("近1年", "month", today=date(2024, 2, 29))[0] == date(2023, 2, 28)


def test_no_monthly_pop_across_missing_month():
    from app.analysis.operators import PeriodOverPeriodOperator
    out = PeriodOverPeriodOperator().run(["月份", METRIC], [{"月份": "2025-10", METRIC: 10}, {"月份": "2025-12", METRIC: 20}])
    assert out.facts["changes"] == [] and out.warnings


@pytest.mark.asyncio
async def test_real_analysis_task_emits_tools_and_passes_only_target_rows_to_insight(monkeypatch):
    p = plan()
    task = p.tasks[-1]
    binding = task.extraction["分析绑定"]
    columns = ["月份", METRIC]
    rows = [{"月份": f"2025-{m:02}", METRIC: m * 100} for m in range(9, 13)]
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.dataset_store = object()
    orchestrator.analysis_engine = AnalysisEngine()
    async def load(*args):
        return {"task-1": (SimpleNamespace(columns=columns), rows)}
    async def save(*args):
        return SimpleNamespace(dataset_id="derived")
    monkeypatch.setattr(orchestrator, "_load_task_result_references", load)
    monkeypatch.setattr(orchestrator, "_save_analysis_derived_dataset", save)
    chat = ChatRequest(conversation_id="local", message_id="m", application_id="app", question="2025年10月至12月销售趋势", semantic_model_id=81)
    events = []
    async def emit(event):
        events.append(event)
    with progress_scope(emit):
        response = await orchestrator._respond_analysis_task(chat, IDENTITY, task, {}, {}, binding)
    assert response.status == "COMPLETED"
    delivered = chat._dag_deferred_insight["facts"]["query_data"]["rows"]
    assert [row["月份"] for row in delivered] == ["2025-10", "2025-11", "2025-12"]
    assert any("环比" in str(event) for event in events)
    assert all(event.get("stage") in {None, "ANALYSIS_TOOL"} for event in events)
    # Real tool milestones remain ordered within the frozen execution node:
    # main start -> completed sub-tools -> main completion.
    from app.api import _thinking_section
    assert events[0]["status"] == "RUNNING"
    assert events[0]["analysis_tool"] == binding["tool"]
    assert events[-1]["status"] == "COMPLETED"
    assert events[-1]["analysis_tool"] == binding["tool"]
    assert all(event["status"] in {"COMPLETED", "DEGRADED"} for event in events[1:-1])
    assert all(_thinking_section(event["stage"]) == "execution" for event in events)


@pytest.mark.parametrize("intent,question,columns,rows,fact", [
    (PrimaryIntent.COMPARISON_ANALYSIS, "2025年10月和11月销售额对比", ["期间", METRIC, "period_role"],
     [{"期间": "2025-10", METRIC: 100, "period_role": "BASE"}, {"期间": "2025-11", METRIC: 120, "period_role": "CURRENT"}], "absolute_change"),
    (PrimaryIntent.COMPOSITION_ANALYSIS, "各产品销售额占比", ["产品", METRIC],
     [{"产品": "甲", METRIC: 30}, {"产品": "乙", METRIC: 70}], "shares"),
    (PrimaryIntent.ANOMALY_ANALYSIS, "检测销售额异常", ["月份", METRIC],
     [{"月份": f"2025-{i + 1:02}", METRIC: v} for i, v in enumerate([10, 11, 9, 10, 12, 100])], "candidates"),
    (PrimaryIntent.ROOT_CAUSE_ANALYSIS, "分析销售额下降原因", ["产品", METRIC + "变化", "整体变化"],
     [{"产品": "甲", METRIC + "变化": -20, "整体变化": -30},
      {"产品": "乙", METRIC + "变化": -10, "整体变化": -30}], "contribution_sum"),
    (PrimaryIntent.FORECAST_ANALYSIS, "预测未来1个月销售额", ["月份", METRIC],
     [{"月份": f"2025-{m:02}", METRIC: 100 + m * 10} for m in range(1, 13)], "forecast"),
])
def test_all_other_chains_reach_real_deterministic_calculations(intent, question, columns, rows, fact):
    p = plan(question, intent)
    request = request_for(p, question)
    result = AnalysisEngine().analyze(request, columns, rows, KnowledgeContext(query=question))
    assert fact in result.facts
    assert result.facts["decision_source"] == "DETERMINISTIC_ALGORITHM"
    assert result.facts["llm_role"] == "PRESENTATION_ONLY"


def test_forecast_horizon_is_not_silently_downgraded():
    from app.analysis import AnalysisError
    q = "预测未来2个月销售额"
    request = request_for(plan(q, PrimaryIntent.FORECAST_ANALYSIS), q)
    assert request.forecast_horizon_periods == 2
    with pytest.raises(AnalysisError, match="一步|多步|预测"):
        AnalysisEngine().analyze(request, ["月份", METRIC],
            [{"月份": f"2025-{m:02}", METRIC: m * 10} for m in range(1, 13)], KnowledgeContext(query=q))


def test_requirements_do_not_extend_the_public_request_schema():
    from app.domain.models import CanonicalAnalysisRequest
    assert "_analysis_contract" not in CanonicalAnalysisRequest.model_fields
    assert "_analysis_reference_date" not in ChatRequest.model_fields


def test_requested_root_chart_still_excludes_baseline_if_analysis_failed():
    from app.presentation.root_report import apply_requested_root_charts
    p = plan()
    requirements = p.tasks[-1].extraction["分析绑定"]["requirements"]
    original_rows = [{"月份": f"2025-{m:02}", METRIC: m} for m in range(9, 13)]
    materials = [{"task_id": "task-1", "facts": {"metric_column": METRIC, "analysis_scope": requirements,
        "query_data": {"columns": ["月份", METRIC], "rows": original_rows}}}]
    rendered = []
    def render(**kwargs):
        rendered.extend(kwargs["chart_specs"])
        return []
    apply_requested_root_charts("用折线图展示销售趋势", materials, ["task-1"], render)
    assert rendered
    assert [row["月份"] for row in rendered[0]["data"]] == ["2025-10", "2025-11", "2025-12"]
    assert original_rows[0]["月份"] == "2025-09"


@pytest.mark.parametrize("base,expected", [(None, "NO_BASELINE"), (0, "COMPUTABLE")])
def test_yoy_missing_and_zero_baselines_are_not_fabricated(base, expected):
    q = "2025年10月至12月销售额同比趋势"
    request = request_for(plan(q), q)
    rows = [{"月份": f"2025-{m:02}", METRIC: 100} for m in range(10, 13)]
    if base is not None:
        rows += [{"月份": f"2024-{m:02}", METRIC: base} for m in range(10, 13)]
    out = AnalysisEngine().analyze(request, ["月份", METRIC], rows, KnowledgeContext(query=q))
    changes = out.facts["year_over_year"]["changes"]
    assert all(item["status"] == expected and item.get("change_rate") is None for item in changes)
    assert out.warnings


def test_continuous_runs_stop_at_flat_and_missing_periods():
    runs = AnalysisEngine._continuous_runs(["2025-07", "2025-08", "2025-09", "2025-10", "2025-12"], [10, 20, 20, 30, 40])
    assert len(runs) == 2
    assert runs[0]["end_label"] == "2025-08"
    assert runs[1]["start_label"] == "2025-09" and runs[1]["end_label"] == "2025-10"


def test_negated_comparison_does_not_fetch_last_year():
    p = plan("分析2025年10月至12月的销售趋势，不需要同比，只看环比")
    assert len(p.tasks) == 2
    assert "year_over_year_change" not in p.tasks[-1].extraction["分析绑定"]["requirements"]["methods"]
