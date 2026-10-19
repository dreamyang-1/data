"""Compile an explicitly declared quotient into existing query/compute tasks."""
from dataclasses import replace
import copy
import re


def normalize_operation_specs(decomposition):
    """Normalize equivalent spellings without inventing operands or formulas."""
    tasks=[]
    for task in decomposition.tasks:
        structured=copy.deepcopy(task.structured)
        spec=(structured or {}).get('结果操作')
        if isinstance(spec,dict) and isinstance(spec.get('operation'),dict):
            operation=spec['operation']
            steps=operation.get('operations',[]) if operation.get('type')=='pipeline' else [operation]
            for step in steps:
                if not isinstance(step,dict):continue
                kind=step.get('type')
                if kind=='derive':
                    step['operator']={'+':'add','-':'subtract','*':'multiply','/':'divide','÷':'divide'}.get(step.get('operator'),step.get('operator'))
                if kind in {'sort','sort_limit'} and 'descending' not in step and step.get('order') in {'asc','desc'}:
                    step['descending']=step.pop('order')=='desc'
                if kind in {'limit','sort_limit'} and 'count' not in step and 'n' in step:
                    step['count']=step.pop('n')
        tasks.append(replace(task,structured=structured))
    return replace(decomposition,tasks=tuple(tasks))


def validate_decomposition_operations(decomposition):
    from app.planning.dependency_contract import projection
    for index,task in enumerate(decomposition.tasks):
        spec=(task.structured or {}).get('结果操作')
        if not isinstance(spec,dict):continue
        validate_result_operation(spec.get('operation'))
        source=spec.get('source_task_index')
        if type(source) is not int or source not in task.depends_on or not 0<=source<index:
            raise ValueError('本地结果操作没有绑定有效的前序任务')
        columns=[name for _,name in projection(decomposition.tasks[source].structured)]
        if columns:
            validate_operation_inputs(spec['operation'],columns)


def validate_result_operation(operation):
    """Only the existing, bounded data operations; no SQL/code or nested plans."""
    if not isinstance(operation, dict):
        raise ValueError('结果操作必须是受控操作对象')
    if operation.get('type') == 'pipeline':
        steps = operation.get('operations')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 5:
            raise ValueError('结果操作只允许1至5步')
    else:
        steps = [operation]
    allowed = {'derive','aggregate','filter','select','sort','sort_limit','limit','extrema','extrema_difference','monthly_growth'}
    if any(not isinstance(step, dict) or step.get('type') not in allowed for step in steps):
        raise ValueError('未知或嵌套结果操作')
    def fields(values):
        return all(isinstance(value, str) and value.strip() for value in values)
    for step in steps:
        if step['type'] == 'monthly_growth':
            from app.services.period_computation import monthly_window
            monthly_window(step)
            if step.get('method') not in {'arithmetic','compound'}:
                raise ValueError('请确认平均月度增长率采用算术平均还是复合平均')
            if (not fields([step.get(key) for key in ('field','period_field','result_field')])
                    or not isinstance(step.get('group_by',[]),list) or not fields(step.get('group_by',[]))):
                raise ValueError('月度增长缺少明确指标、月份或分组字段')
        elif step['type'] == 'derive':
            if (step.get('operator') not in {'add','subtract','multiply','divide'}
                    or not fields([step.get(key) for key in ('left_field','right_field','result_field')])):
                raise ValueError('逐行计算缺少明确操作或字段')
        elif step['type'] == 'aggregate':
            groups,specs = step.get('group_by',[]),step.get('aggregations')
            if (not isinstance(groups,list) or not fields(groups) or len(groups)>20
                    or not isinstance(specs,list) or not 1<=len(specs)<=20):
                raise ValueError('聚合计算缺少分组或指标定义')
            for spec in specs:
                if (not isinstance(spec,dict) or spec.get('function') not in {'count','sum','avg','min','max'}
                        or not fields([spec.get('field')]) or ('alias' in spec and not fields([spec['alias']]))):
                    raise ValueError('聚合计算缺少明确函数或字段')
    return steps


def validate_operation_inputs(operation, columns):
    """Check each step against the preceding step's actual output schema."""
    available=set(columns)
    for step in validate_result_operation(operation):
        kind=step['type']
        required=[]
        if kind=='derive':
            required=[step['left_field'],step['right_field']]
        elif kind=='aggregate':
            required=[*step.get('group_by',[]),*(spec['field'] for spec in step['aggregations'])]
        elif kind=='select':
            required=step.get('columns',[])
        elif kind=='monthly_growth':
            # Month aliases and identity companion columns are resolved from
            # the actual ASL result by the monthly input binder. The metric
            # operand itself must already be part of the query projection.
            required=[step['field']]
        elif kind in {'filter','sort','sort_limit','extrema','extrema_difference'}:
            required=[step.get('field')]
        if any(not isinstance(field,str) or field not in available for field in required):
            raise ValueError('结果操作引用了前序结果未包含的输入字段：'+','.join(str(field) for field in required if field not in available))
        if kind=='derive':
            available.add(step['result_field'])
        elif kind=='aggregate':
            available=set(step.get('group_by',[]))|{spec.get('alias') or f"{spec['function']}_{spec['field']}" for spec in step['aggregations']}
        elif kind=='select':
            available=set(required)
        elif kind=='monthly_growth':
            available=set(step.get('group_by',[]))|{step['result_field']}
    return available


def materialize_declared_result_operation(decomposition):
    """Publish a query and its declared postprocessing as separate tasks."""
    from app.domain.models import PrimaryIntent
    from app.planning.task_dag import DecomposedTask, database_query_choice, TaskInputBindingError, extraction_to_parameters
    if len(decomposition.tasks) != 1:
        return decomposition
    task = decomposition.tasks[0]
    spec = (task.structured or {}).get('结果操作')
    if spec is None or (task.structured or {}).get('计算待确认') or database_query_choice(task.structured) is False:
        return decomposition
    if isinstance(spec,dict) and isinstance(spec.get('operation'),dict):
        spec=copy.deepcopy(spec)
        operation=spec['operation']
        steps=operation.get('operations',[]) if operation.get('type')=='pipeline' else [operation]
        if steps and isinstance(steps[0],dict) and steps[0].get('type')=='monthly_growth':
            from app.services.period_computation import resolve_monthly_window
            wording=task.question+' '+str((task.structured.get('时间粒度') or {}).get('time_range') or '')
            resolved=resolve_monthly_window(steps[0],wording)
            if resolved is None:
                structured=copy.deepcopy(task.structured)
                structured.pop('结果操作')
                structured['计算待确认']={'question':'请确认月度增长统计的月份起止，或是否使用最近若干个完整自然月；滚动窗口可能包含不完整月份，不能自动等同。',
                    'candidates':[],'affected_slots':['analysis_window']}
                return replace(decomposition,tasks=(replace(task,structured=structured),))
            if operation.get('type')=='pipeline':
                operation['operations'][0]=resolved
            else:
                spec['operation']=resolved
    try:
        # In a one-query plan, index 0 unambiguously means that same query's
        # result. Materialize it as a real predecessor before assigning IDs.
        if (not isinstance(spec, dict) or 'source_task_id' in spec
                or ('source_task_index' in spec and (type(spec['source_task_index']) is not int
                    or spec['source_task_index']!=0 or task.depends_on))):
            raise ValueError('查询后的结果操作只引用本任务的查询结果')
        validate_result_operation(spec.get('operation'))
        from app.planning.dependency_contract import projection
        columns=[name for _,name in projection(task.structured)]
        if columns:
            validate_operation_inputs(spec['operation'],columns)
    except ValueError as exc:
        raise TaskInputBindingError(str(exc)) from exc
    query = copy.deepcopy(task.structured)
    query.pop('结果操作')
    query['输出要求'] = '返回后续结果操作需要的原始指标和分组字段，不在文字中声称计算完成'
    query['是否查询数据库'] = '是'
    question=task.question
    steps=validate_result_operation(spec['operation'])
    if steps[0]['type']=='monthly_growth':
        from app.services.period_computation import comparison_query_range
        time_range=comparison_query_range(steps[0])
        query['时间粒度']={'unit':'月','time_range':time_range,'anchor':None}
        # Absolute dates are trusted compiler output, not model-authored user
        # conditions. Show them in typed parameters and keep the original
        # question eligible for the existing anti-invention grounding guard.
        question=f'{question}（取数含比较基期，最终增长统计月份不变）'
    compute = DecomposedTask(question=f'根据上述任务的查询结果完成{task.expected_output or task.name or "结果计算"}，不再查询数据库',
        name='计算查询结果',depends_on=(0,),expected_output=task.expected_output,
        intent=PrimaryIntent.COMPARISON_ANALYSIS,structured={'意图':'指标计算','是否查询数据库':'否',
        '结果操作':{'source_task_index':0,'operation':copy.deepcopy(spec['operation'])}})
    names=[item.get('name') if isinstance(item,dict) else item for item in query.get('指标',[])]
    name=('统计'+'、'.join(value for value in names if isinstance(value,str))+'计算输入')[:60]
    return replace(decomposition,tasks=(replace(task,structured=query,question=question,
        name=name,parameters=tuple(extraction_to_parameters(query)),expected_output='供后续计算使用的原始指标与分组数据'),compute))


def declared_division(extraction):
    """Only literal declared input columns; no arbitrary expression evaluation."""
    columns = [item.get('name') if isinstance(item, dict) else item
               for item in (extraction or {}).get('指标', [])]
    columns = [name for name in columns if isinstance(name, str) and name]
    if len(set(columns)) < 2:
        return None
    names = '|'.join(re.escape(name) for name in sorted(set(columns), key=len, reverse=True))
    requirement = (extraction or {}).get('输出要求')
    if not isinstance(requirement, str):
        return None
    match = re.search(rf'(?P<label>[^=＝，,。;；：:]{{1,80}})[=＝]\s*(?P<left>{names})\s*(?:/|÷|除以)\s*(?P<right>{names})(?![\w.])', requirement)
    if match is None:
        return None
    label = re.sub(r'^(?:计算|求出|求得|统计)\s*', '', match['label']).strip()
    if not label or label in columns or match['left'] == match['right']:
        return None
    return {'operation':'divide', 'numerator':{'column':match['left']},
            'denominator':{'column':match['right']}, 'result_field':label,
            'format':'percent' if re.search(r'率|占比|比例|%', label) else 'number'}


def materialize_declared_division(decomposition):
    """Do not ask ASL to return an unregistered expression as raw metrics.

    A single query requesting two registered operands plus an explicit quotient
    becomes one input query and one bounded computation, before planning display.
    Existing multi-task and registered single-metric queries remain unchanged.
    """
    from app.domain.models import PrimaryIntent
    from app.planning.task_dag import DecomposedTask, database_query_choice
    if len(decomposition.tasks) != 1:
        return decomposition
    task = decomposition.tasks[0]
    if database_query_choice(task.structured) is False or task.intent in {
            PrimaryIntent.CHAT,PrimaryIntent.OUT_OF_SCOPE,PrimaryIntent.CAPABILITY_HELP,
            PrimaryIntent.METRIC_DEFINITION,PrimaryIntent.DATA_LINEAGE}:
        return decomposition
    binding = declared_division(task.structured)
    if binding is None:
        return decomposition
    left, right, label = binding['numerator']['column'], binding['denominator']['column'], binding['result_field']
    query = copy.deepcopy(task.structured)
    query['输出要求'] = f'仅返回{left}和{right}，派生结果由后续计算任务生成'
    query['是否查询数据库'] = '是'
    for role in ('numerator','denominator'):
        binding[role]['source_task_index'] = 0
    compute = DecomposedTask(question=f'根据上述任务的查询结果计算{label}，{left}除以{right}，不再查询数据库',
        name=f'计算{label}'[:60], depends_on=(0,), expected_output=label,
        intent=PrimaryIntent.COMPARISON_ANALYSIS, structured={
            '意图':'指标计算','指标':[{'name':label}], '是否查询数据库':'否','计算绑定':binding})
    source = replace(task, name=f'统计{left}和{right}'[:60],structured=query,
                     expected_output=f'{left}和{right}')
    return replace(decomposition,tasks=(source,compute))
