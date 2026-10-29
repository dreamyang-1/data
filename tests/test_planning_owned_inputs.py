"""Planning owns data dependencies across extraction, expansion and execution."""
import copy

import pytest

from app.domain.models import PrimaryIntent
from app.planning.compound_question import CompoundJudgment, CompoundQuestionRouter
from app.planning.task_plan_stages import TaskBoundary, complete_boundaries
from test_task_planning_stages import setup_planner, SPEC


@pytest.mark.asyncio
async def test_planned_input_reaches_runtime_even_when_extraction_omits_or_forges_binding(monkeypatch):
    inputs=[{'source_task_index': 0, 'source_column': '业务员.销售员编码'}]
    plan={'tasks': [
        {'question': '统计2025年各业务员含税销售总额，返回销售员编码', 'expected_output':'业务员编码及销售总额',
         'depends_on': [], 'dependency_bindings': []},
        {'question': '查询前序入选业务员所属公司', 'expected_output':'人员及所属公司',
         'depends_on': [0], 'dependency_bindings': inputs}]}
    def extraction(fixed):
        return {'tasks': [{'task_index': i, 'extraction': {
            '实体': ['业务员'], '展示字段': [{'entity':'业务员','field':'销售员编码'}],
            '是否查询数据库':'是',
            **({'依赖绑定':[{'source_task_index':99,'source_column':'错误字段'}]} if i else {})}}
            for i, _ in enumerate(fixed['tasks'])]}
    planner,_=setup_planner(monkeypatch, plan, extract=extraction)
    router=CompoundQuestionRouter(planner.settings)
    router._single=planner
    outcome=await router.plan_with_judgment(CompoundJudgment(is_compound=False,intent='明细查询'),
        '统计2025年各业务员销售总额并查询所属公司',semantic_model_id=130)
    task=outcome.plan.tasks[1]
    assert task.dependency_bindings == [{'source_task_id':'task-1','source_column':'业务员.销售员编码'}]
    assert '依赖绑定' not in task.extraction
    assert task.depends_on == ['task-1']
    assert plan['tasks'][1]['dependency_bindings'] == inputs


def test_formula_expansion_moves_company_input_to_final_ranking_not_component_query():
    source=[TaskBoundary(question='统计2025年平均每单销售金额最高的五名业务员，返回销售员编码',
                         expected_output='平均每单销售金额最高的五名业务员及销售员编码',dependency_bindings=[]),
            TaskBoundary(question='查询前序入选人员所属公司',expected_output='人员及所属公司',depends_on=[0],
                         dependency_bindings=[{'source_task_index':0,'source_column':'业务员.销售员编码'}])]
    before=copy.deepcopy(source)
    result=complete_boundaries(source,spec=SPEC,
        catalog=(frozenset(['平均每单销售金额','含税销售总额','订单笔数']),{}),
        dependencies={'平均每单销售金额':('含税销售总额','订单笔数')})
    assert len(result)==4
    assert result[-1].depends_on==[2]
    assert result[-1].dependency_bindings==[{'source_task_index':2,'source_column':'业务员.销售员编码'}]
    assert source==before
