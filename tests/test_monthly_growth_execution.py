"""Monthly growth uses explicit methods and a complete comparison base."""
import pytest

from app.domain.models import AtomicTask
from minio_followup_store import apply_followup_operation
from test_complex_execution_correctness import service, context, persist, IDENTITY


def operation(method='arithmetic'):
    return {'type':'monthly_growth','field':'数量','period_field':'月份',
        'group_by':['组织编码'],'method':method,'period_start':'2025-02','period_end':'2025-03',
        'result_field':'平均增速','format':'percent'}


def rows():
    return [{'组织编码':'A','月份':'2025-01','数量':100},
            {'组织编码':'A','月份':'2025-02','数量':200},
            {'组织编码':'A','月份':'2025-03','数量':100},
            {'组织编码':'B','月份':'2025-01','数量':50},
            {'组织编码':'B','月份':'2025-02','数量':100},
            {'组织编码':'B','月份':'2025-03','数量':200}]


@pytest.mark.asyncio
@pytest.mark.parametrize('method,expected', [('arithmetic',['25.00%','100.00%']),('compound',['0.00%','100.00%'])])
async def test_explicit_growth_method_runs_on_actual_complete_persisted_months(method,expected):
    agent=service();chat,_=context()
    await persist(agent,chat,['组织编码','月份','数量'],list(reversed(rows())),conversation='source')
    task=AtomicTask(task_id='growth',question='计算明确口径的平均增速',depends_on=['source'],extraction={
        '是否查询数据库':'否','结果操作':{'source_task_id':'source','operation':operation(method)}})
    result=await agent._respond_pure_computation(chat,IDENTITY,task,{'source':'source'})
    assert result.status=='COMPLETED'
    actual={row['组织编码']:row['平均增速'] for row in result.evidence[0].payload['preview']}
    assert [actual['A'],actual['B']]==expected


@pytest.mark.parametrize('damage', ['no_base','missing_month','zero_base','duplicate_month','null_value','unknown_method'])
def test_monthly_growth_rejects_missing_or_ambiguous_inputs(damage):
    data=rows();spec=operation()
    if damage=='no_base':
        data=[row for row in data if row['月份']!='2025-01']
    elif damage=='missing_month':
        data=[row for row in data if not(row['组织编码']=='A' and row['月份']=='2025-02')]
    elif damage=='zero_base':
        data[0]['数量']=0
    elif damage=='duplicate_month':
        data.append(dict(data[0]))
    elif damage=='null_value':
        data[1]['数量']=None
    else:
        spec['method']=None
    with pytest.raises(ValueError):
        apply_followup_operation(['组织编码','月份','数量'],data,spec)


def test_query_operation_requests_one_additional_month_for_the_comparison_base():
    from app.domain.models import PrimaryIntent
    from app.planning.calculation_bindings import materialize_declared_result_operation
    from app.planning.task_dag import DecomposedTask,QuestionDecomposition
    plan=QuestionDecomposition(tasks=(DecomposedTask(question='统计2025年2至3月平均月度增长率',intent=PrimaryIntent.METRIC_QUERY,
        structured={'指标':[{'name':'数量'}],'是否查询数据库':'是','时间粒度':{'unit':'月','time_range':'2025年2至3月'},
                    '结果操作':{'operation':operation()}}),))
    result=materialize_declared_result_operation(plan)
    assert result.tasks[0].structured['时间粒度']['time_range']=='2025-01-01至2025-03-31'
    assert '比较基期' in result.tasks[0].question
    assert result.tasks[1].structured['结果操作']['operation']['period_start']=='2025-02'


def test_missing_model_dates_are_compiled_from_explicit_complete_natural_months(monkeypatch):
    from datetime import date
    import app.intent.classifier as classifier
    from app.services.period_computation import resolve_monthly_window
    class ReferenceDate(date):
        @classmethod
        def today(cls):
            return cls(2026,10,19)
    monkeypatch.setattr(classifier,'date',ReferenceDate)
    spec={**operation(),'period_start':None,'period_end':None}
    result=resolve_monthly_window(spec,'最近12个完整自然月')
    assert (result['period_start'],result['period_end'])==('2025-10','2026-09')
    assert resolve_monthly_window(spec,'近12个月') is None  # rolling != full calendar months


def test_growth_ranking_sorts_percentages_numerically_after_calculation():
    pipeline={'type':'pipeline','operations':[operation(),
        {'type':'sort_limit','field':'平均增速','descending':True,'count':1}]}
    _,ranked=apply_followup_operation(['组织编码','月份','数量'],rows(),pipeline)
    assert ranked==[{'组织编码':'B','平均增速':'100.00%'}]


@pytest.mark.asyncio
async def test_compiled_comparison_base_passes_the_real_router_grounding_guard():
    from unittest.mock import AsyncMock
    from app.domain.models import PrimaryIntent
    from app.planning.compound_question import CompoundQuestionRouter,CompoundJudgment
    from app.planning.task_dag import SingleQuestionPlanner,QuestionDecomposition,DecomposedTask
    agent=service()
    planner=SingleQuestionPlanner(agent.settings.model_copy(update={'intent_model_enabled':True,
        'multi_question_model_enabled':True,'intent_model_api_key':'test'}))
    question='统计各组织最近12个完整自然月数量月环比增长率的算术平均'
    spec={**operation(),'period_start':None,'period_end':None}
    planner._model_decompose=AsyncMock(return_value=QuestionDecomposition(intent=PrimaryIntent.METRIC_QUERY,
        tasks=(DecomposedTask(question=question,intent=PrimaryIntent.METRIC_QUERY,structured={
            '指标':[{'name':'数量'}],'维度':['组织编码'],'是否查询数据库':'是',
            '时间粒度':{'unit':'月','time_range':'最近12个完整自然月'},'结果操作':{'operation':spec}}),)))
    router=CompoundQuestionRouter(planner.settings);router._single=planner
    result=await router.plan_with_judgment(CompoundJudgment(is_compound=False),question)
    assert result.plan is not None
    assert len(result.plan.tasks)==2
    query,compute=result.plan.tasks
    assert '比较基期' in query.question
    assert any('时间范围' in value and '至' in value for value in query.parameters)
    assert compute.extraction['结果操作']['operation']['period_start'] is not None
