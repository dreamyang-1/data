"""Compile a validated model proposal using existing query/analysis DAG nodes."""
import copy
from datetime import timedelta

from app.domain.models import PlannerExtraction
from .plans import PLAN_KEY, plan_from_structured
from .registry import intent_for_chain


def compile_decomposition(question, decomposition):
    tasks=decomposition.tasks
    proposals=[(task,plan_from_structured(task.structured)) for task in tasks if (task.structured or {}).get(PLAN_KEY)]
    if not proposals:return None
    first, proposal=proposals[0]
    if any(plan.model_dump()!=proposal.model_dump() for _,plan in proposals):
        raise ValueError('ANALYSIS_TOOL_PLAN_CHANGED_BETWEEN_TASKS')
    # Existing registered arithmetic DAGs retain their component computation.
    if any((task.structured or {}).get('计算绑定')
        and (task.structured or {}).get('计算绑定来源')=='REGISTERED_COMPOUND' for task in tasks):return None
    candidates=[task for task in tasks if (task.structured or {}).get('指标')
        and not str((task.structured or {}).get('是否查询数据库') or '').startswith('否')]
    seed=next((task for task in tasks if (task.structured or {}).get('分析绑定')),None)
    if seed is None:
        seed=(max(candidates,key=lambda task:len((task.structured or {}).get('维度') or []))
            if proposal.chain=='attribution' and candidates else next(iter(candidates),first))
    structured=copy.deepcopy(seed.structured)
    structured.pop('分析绑定',None)
    structured['是否查询数据库']='是'
    if proposal.target_window:
        window=proposal.target_window
        structured['时间粒度']={**(structured.get('时间粒度') or {}),'unit':proposal.grain,
            'time_range':f'{window.start}至{window.end_exclusive-timedelta(days=1)}'}
    elif proposal.comparison_periods:
        start=min(period.start for period in proposal.comparison_periods)
        end=max(period.end_exclusive for period in proposal.comparison_periods)-timedelta(days=1)
        structured['时间粒度']={**(structured.get('时间粒度') or {}),
            'unit':proposal.grain or (structured.get('时间粒度') or {}).get('unit'),
            'time_range':f'{start}至{end}'}
    if proposal.comparison_kind=='objects':
        return _compile_objects(question,structured,proposal)
    from app.planning.analysis_task_planner import build_analysis_task_plan
    return build_analysis_task_plan(question,PlannerExtraction(intent=intent_for_chain(proposal.chain),
        parameters=list(seed.parameters),structured=structured),task_question=question,task_name=seed.name)


def _compile_objects(question,structured,proposal):
    from app.domain.models import AtomicTask,TaskPlan,PrimaryIntent as I
    from app.planning.analysis_task_planner import _retrieval_extraction,_window_query,_metric_text
    from app.planning.task_dag import extraction_to_parameters
    from app.analysis.tools.requirements import build_model_requirements
    from datetime import datetime
    from zoneinfo import ZoneInfo
    requirements=build_model_requirements(proposal,structured,today=datetime.now(ZoneInfo('Asia/Shanghai')).date())
    requirements['comparison_dimensions']=[name for name in structured.get('维度') or [] if name!='业务日期']
    tasks=[];periods=[]
    objects=sorted(proposal.comparison_objects,key=lambda item:0 if item.role=='BASE' else 1)
    for index,item in enumerate(objects,1):
        value=_retrieval_extraction(structured)
        value['过滤条件']=[predicate for predicate in value.get('过滤条件') or []
            if not (predicate.get('entity')==item.entity and predicate.get('field')==item.field)]
        value['过滤条件'].append({'entity':item.entity,'field':item.field,'op':'=','value':item.value})
        task_id=f'task-{index}'
        text=_window_query(value.get('时间粒度') or {},requirements.get('grain'),_metric_text(value),value.get('维度') or [],value)
        tasks.append(AtomicTask(task_id=task_id,question=text,name=text[:60],primary_intent=I.METRIC_QUERY,
            extraction=value,parameters=extraction_to_parameters(value),expected_output=item.role+'对象的同口径指标'))
        periods.append({'task':task_id,'label':item.value,'role':'基期' if item.role=='BASE' else '当前期'})
    analysis=copy.deepcopy(structured)
    analysis['是否查询数据库']='否'
    analysis['分析绑定']={'analysis_type':'comparison','intent':I.COMPARISON_ANALYSIS.value,
        'tool':'对比分析工具','sub_tools':{},'merge':'period_roles','periods':periods,
        'source_question':question,'requirements':requirements,'optional_dependencies':[]}
    tasks.append(AtomicTask(task_id='task-3',question='根据前序两个对象的数据执行指定对比工具，不再查询数据库',
        name='比较明确基准与当前对象',depends_on=['task-1','task-2'],primary_intent=I.COMPARISON_ANALYSIS,
        extraction=analysis,parameters=extraction_to_parameters(analysis),expected_output='明确角色的对象对比结果'))
    return TaskPlan(tasks=tasks,planner='STRUCTURED_MODEL')


def periods_from_proposal(structured):
    plan=plan_from_structured(structured)
    if plan is None:return None
    ordered=sorted(plan.comparison_periods,key=lambda item:0 if item.role=='BASE' else 1)
    return [(f'{item.start}至{item.end_exclusive-timedelta(days=1)}',
        (item.start.year,item.start.month,item.start.day)) for item in ordered] or []
