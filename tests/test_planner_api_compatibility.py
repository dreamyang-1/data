"""Both deployed and legacy planner contracts remain usable by orchestration."""
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock
import sys

import pytest

from app.domain.models import AtomicTask, PlannerExtraction, PrimaryIntent, TaskPlan
from app.planning.compat import plan_question, prepare_execution


EXTRACTION = PlannerExtraction(intent=PrimaryIntent.METRIC_QUERY, parameters=['销售额（指标）'])


@pytest.mark.asyncio
async def test_legacy_planner_receives_identical_authoritative_inputs():
    calls = []
    async def plan(question, semantic_context='', semantic_model_id=None):
        calls.append((question, semantic_context, semantic_model_id))
        return Obj(plan=None, single_extraction=EXTRACTION)
    result = await plan_question(Obj(plan=plan), '补全问题', semantic_context='当前模型规范', semantic_model_id=121)
    assert calls == [('补全问题', '当前模型规范', 121)]
    assert result.single_extraction is EXTRACTION and result.plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize('prior', [None, Obj(is_compound=True)])
async def test_router_reuses_preceding_judgment_and_unwraps_single_outcome(prior):
    final_judgment = Obj(is_compound=False)
    output = Obj(plan=None, single=Obj(single_extraction=EXTRACTION), judgment=final_judgment)
    calls = []
    async def route_and_plan(question, *, semantic_context='', semantic_model_id=None):
        calls.append(('route', question, semantic_context, semantic_model_id))
        return output
    async def plan_with_judgment(judgment, question, *, semantic_context='', semantic_model_id=None):
        assert judgment is prior
        calls.append(('reuse', question, semantic_context, semantic_model_id))
        return output
    router = Obj(route_and_plan=route_and_plan, plan_with_judgment=plan_with_judgment)
    result = await plan_question(router, '补全问题', semantic_context='规范', semantic_model_id=120, prior_judgment=prior)
    assert calls == [('reuse' if prior is not None else 'route', '补全问题', '规范', 120)]
    assert result.single_extraction is EXTRACTION and result.judgment is final_judgment


@pytest.mark.asyncio
async def test_compound_router_keeps_plan_and_has_no_single_extraction():
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[AtomicTask(task_id='t1', question='查销售额'), AtomicTask(task_id='t2', question='查订单数')])
    async def route_and_plan(question): return Obj(plan=plan, single=None, judgment=Obj(is_compound=True))
    result = await plan_question(Obj(route_and_plan=route_and_plan), '查销售额及订单数', semantic_context='', semantic_model_id=121)
    assert result.plan is plan and result.single_extraction is None


def test_execution_supports_legacy_instance_methods():
    plan = Obj()
    planner = Obj(deduplicate=lambda p: (p, {'duplicate':'t1'}), execution_layers=lambda p: [['t1']])
    assert prepare_execution(planner, plan) == (plan, {'duplicate':'t1'}, [['t1']])


def test_execution_supports_module_functions_on_49(monkeypatch):
    plan = Obj()
    monkeypatch.setitem(sys.modules, 'app.planning.compound_question', Obj(
        deduplicate_plan=lambda p: (p, {}), execution_layers=lambda p: [['t1'], ['t2']]))
    assert prepare_execution(Obj(), plan) == (plan, {}, [['t1'], ['t2']])


@pytest.mark.asyncio
async def test_planning_failure_is_not_reinterpreted_as_an_empty_plan():
    from app.planning import TaskPlanningError
    async def route_and_plan(question): raise TaskPlanningError('invalid dependency')
    with pytest.raises(TaskPlanningError):
        await plan_question(Obj(route_and_plan=route_and_plan), '问题', semantic_context='', semantic_model_id=121)


@pytest.mark.asyncio
async def test_native_router_api_when_installed():
    try:
        from app.planning.compound_question import CompoundQuestionRouter, CompoundJudgment
    except ModuleNotFoundError:
        pytest.skip('Server router is verified separately against the actual 49 snapshot')
    from app.config import Settings
    router = CompoundQuestionRouter(Settings(env='test', multi_question_model_enabled=False))
    judgment = CompoundJudgment(is_compound=False)
    router._single.plan_single = AsyncMock(return_value=Obj(single_extraction=EXTRACTION))
    result = await plan_question(router, '问题', semantic_context='', semantic_model_id=121, prior_judgment=judgment)
    assert result.single_extraction is EXTRACTION
    plan = TaskPlan(planner='DETERMINISTIC_RULE', tasks=[AtomicTask(task_id='t1', question='查销售额'), AtomicTask(task_id='t2', question='查产品', depends_on=['t1'])])
    assert [[t.task_id for t in group] for group in prepare_execution(router, plan)[2]] == [['t1'], ['t2']]
