"""Bind registered arithmetic to existing query/compute tasks."""
from dataclasses import replace
import copy
import re


ARITHMETIC_OPERATORS = {
    'add': r'\+|加上|加',
    'subtract': r'−|－|-|减去|减',
    'multiply': r'\*|×|乘以|乘',
    'divide': r'/|÷|除以',
    'power': r'\*\*|\^|乘方',
    'modulo': r'%|取余|取模',
}


def arithmetic_roles(binding):
    """Retain the published ratio schema while allowing ordinary binary math."""
    from app.numeric_operations import operator_roles
    if not isinstance(binding, dict):
        raise ValueError('计算绑定必须声明受控运算')
    roles = operator_roles(binding.get('operation'))
    if binding['operation'] == 'divide' and any(key in binding for key in ('numerator', 'denominator')):
        if any(key in binding for key in ('left', 'right', 'input')):
            raise ValueError('计算操作数不能混用两套字段')
        return ('numerator', 'denominator')
    if any(key in binding for key in ('numerator', 'denominator')):
        raise ValueError('加减乘运算必须使用左右操作数')
    if any(key in binding and key not in roles for key in ('input','left','right')):
        raise ValueError('运算操作数个数不匹配')
    return roles


def registered_arithmetic_binding(spec, name, components):
    """Read one binary formula without treating dependency order as math."""
    row = next((line for line in spec.splitlines() if line.strip().startswith('| ' + name + ' |')), '')
    cells = [cell.strip() for cell in row.strip().strip('|').split('|')]
    if len(cells) < 4:
        return None
    definition=cells[2]
    if len(components)==1:
        column=components[0];escaped=re.escape(column)
        from app.numeric_operations import UNARY
        for op in sorted(UNARY):
            if re.fullmatch(re.escape(op)+r'\s*\(\s*'+escaped+r'\s*\)',definition.strip()):
                return {'operation':op,'input':{'column':column},'result_field':name,'format':'number'}
        for op,pattern in {'square':escaped+r'的?平方(?!根)','cube':escaped+r'的?立方(?!根)',
            'sqrt':r'(?:sqrt\s*\(\s*'+escaped+r'\s*\)|'+escaped+r'的?平方根)',
            'cbrt':r'(?:cbrt\s*\(\s*'+escaped+r'\s*\)|'+escaped+r'的?立方根)',
            'abs':r'(?:abs\s*\(\s*'+escaped+r'\s*\)|'+escaped+r'的?绝对值)'}.items():
            match=re.search(pattern,definition)
            if match and not re.search(r'[+*/^×÷]|平方|立方|开根|方差|标准差|平均',definition[:match.start()]+definition[match.end():]):
                return {'operation':op,'input':{'column':column},'result_field':name,'format':'number'}
        literal=r'(?<![\w.])(-?(?:\d+(?:\.\d*)?|\.\d+))(?![\w.])'
        for op,pattern in ARITHMETIC_OPERATORS.items():
            match=re.search(escaped+r'\s*(?:'+pattern+r')\s*'+literal,definition)
            if match:
                remaining=definition[:match.start()]+definition[match.end():]
                if re.search(r'[+*/^×÷]|加|减|乘|除|平方|开根',remaining):return None
                value=float(match[1]);value=int(value) if value.is_integer() else value
                return {'operation':op,'left':{'column':column},'right':{'value':value},'result_field':name,'format':'number'}
        return None
    if len(components)!=2:return None
    choices = [(operator, left, right) for operator, pattern in ARITHMETIC_OPERATORS.items()
               for left in components for right in components
               if left != right and re.search(re.escape(left) + r'\s*(?:' + pattern + r')\s*'
                                              + re.escape(right), cells[2])]
    if len(choices) != 1:
        return None
    operator, left, right = choices[0]
    match=re.search(re.escape(left)+r'\s*(?:'+ARITHMETIC_OPERATORS[operator]+r')\s*'+re.escape(right),definition)
    remaining=definition[:match.start()]+definition[match.end():]
    # Never silently drop a second operation from a registered formula.
    if re.search(r'[+*/^×÷]|加|减|乘|除|平方|开根',remaining):
        if not (operator=='divide' and '%' in cells[3] and re.fullmatch(r'\s*[×*]\s*100\s*%?\s*',remaining)):
            return None
    roles = ('numerator', 'denominator') if operator == 'divide' else ('left', 'right')
    return {'operation':operator,roles[0]:{'column':left},roles[1]:{'column':right},
            'result_field':name,'format':'percent' if '%' in cells[3] else 'number'}


async def resolve_compound_formulas(settings, transport, spec_text, dependencies, required):
    """Bind registered formula roles in a small schema, independently of task prose.

    Dependency order is not operand order. The current description is the only
    source; results can only reference that metric's registered components.
    """
    import httpx
    if not required:
        return {}
    rows = [line for line in spec_text.splitlines() if line.lstrip().startswith('|')
            and any(line.strip().startswith('| ' + name + ' |') for name in required)]
    if not rows:
        raise ValueError('复合指标缺少当前规范中的公式证据')
    # Older published descriptions can declare dependencies without a formula.
    # Keep their existing plan and legacy supported calculation path; a new
    # formula must never be inferred from component order or dependency text.
    required = [name for name in required if any(
        line.strip().startswith('| ' + name + ' |') and len(line.strip().strip('|').split('|')) >= 4
        and (registered_arithmetic_binding(line,name,dependencies[name]) is not None
             or re.search('|'.join(ARITHMETIC_OPERATORS.values())+r'|平方|立方|sqrt\(|cbrt\(|abs\(', line.strip().strip('|').split('|')[2]))
        for line in rows)]
    if not required:
        return {}
    exact = {}
    for name in required:
        binding = registered_arithmetic_binding(spec_text, name, dependencies[name])
        if binding is not None:
            exact[name] = binding
    if len(exact) == len(required):
        return exact
    required = [name for name in required if name not in exact]
    for name in required:
        row=next(line for line in rows if line.strip().startswith('| '+name+' |'))
        definition=row.strip().strip('|').split('|')[2]
        normalized=definition
        for component in sorted(dependencies[name],key=len,reverse=True):
            normalized=normalized.replace(component,'input')
        # A small role-binding call cannot encode a multi-step formula. Leave
        # that to the planner's typed pipeline, rather than accepting a partial
        # binary operation that silently loses the rest of the expression.
        if len(re.findall(r'\*\*|[+*/^×÷]|加上|减去|乘以|除以|(?<=input)-',normalized))>1:
            raise ValueError('登记公式包含多步运算，必须规划完整的结果操作pipeline，不能压缩为单个计算绑定')
    body = {'model': settings.intent_model_name, 'temperature': 0, 'enable_thinking': False,
        'response_format': {'type': 'json_object'}, 'messages': [
        {'role': 'system', 'content': '只绑定给定规范行中的计算公式，不回答业务问题。'
         '输出JSON {"bindings":[{"metric":"登记指标名","operation":"add或subtract或multiply或divide或power或modulo",'
         '"left":"左成分登记名","right":"右成分登记名","format":"number或percent"}]}。'
         'divide也兼容numerator、denominator字段。操作数顺序必须按公式含义绑定到该指标的依赖指标登记原名，不能使用依赖指标的排列顺序。'
         '只支持明确的两个成分加减乘除；多步公式不能压缩成一个运算。不能确定或不支持时不输出该项。不得添加其他指标。'},
        {'role': 'user', 'content': json_text({'required': required, 'components': {name: dependencies[name] for name in required},
                                            'registered_rows': rows})}]}
    async with httpx.AsyncClient(base_url=settings.intent_model_base_url.rstrip('/'),
        timeout=settings.multi_question_model_timeout_seconds, transport=transport) as client:
        response = await client.post('/chat/completions', json=body,
            headers={'Authorization': 'Bearer ' + settings.intent_model_api_key.get_secret_value()})
        response.raise_for_status()
    import json
    from app.planning.task_dag import _strip_json_fences
    data = json.loads(_strip_json_fences(response.json()['choices'][0]['message']['content']))
    if not isinstance(data, dict) or not isinstance(data.get('bindings'), list):
        raise ValueError('公式绑定必须返回明确的绑定列表')
    result = {}
    for item in data.get('bindings', []):
        if not isinstance(item, dict) or any(not isinstance(item.get(key), str)
                for key in ('metric', 'operation', 'format')):
            raise ValueError('公式绑定格式错误')
        roles = arithmetic_roles(item)
        name = item.get('metric')
        if (name not in required or name in result
                or item.get('format') not in {'number', 'percent'}
                or any(not isinstance(item.get(role), str) for role in roles)
                or {item.get(role) for role in roles} != set(dependencies[name])
                or len(dependencies[name]) != len(roles)):
            raise ValueError('公式绑定超出当前指标的登记成分')
        result[name] = {'operation': item['operation'], **{role: {'column': item[role]} for role in roles},
                        'result_field': name, 'format': item['format']}
    if set(result) != set(required):
        raise ValueError('未获得当前复合指标的明确计算公式')
    return {**exact, **result}


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


def normalize_result_operation(operation):
    """Adapt the planner's order/limit spellings to the existing dataset API."""
    value = copy.deepcopy(operation)
    if isinstance(value,dict) and value.get('type')=='pipeline' and 'steps' in value:
        steps=value.pop('steps')
        if 'operations' in value and value['operations']!=steps:
            raise ValueError('结果流水线同时提供了不一致的steps和operations')
        value['operations']=steps
    steps = value.get('operations', []) if isinstance(value, dict) and value.get('type') == 'pipeline' else [value]
    if not isinstance(steps, list):
        return value
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get('type') in {'sort', 'sort_limit'} and 'order' in step:
            direction = step.pop('order')
            if str(direction).lower() not in {'asc', 'desc'}:
                raise ValueError('结果排序方向无效')
            descending = str(direction).lower() == 'desc'
            if 'descending' in step and step['descending'] != descending:
                raise ValueError('结果排序方向存在冲突')
            step['descending'] = descending
        if step.get('type') == 'sort_limit' and 'limit' in step:
            count = step.pop('limit')
            if 'count' in step and step['count'] != count:
                raise ValueError('结果人数限制存在冲突')
            step['count'] = count
    return value


def materialize_computation_binding(extraction, *, task_index, task_offset=0, dependencies):
    """Resolve local indices once, before exposing the plan or running tasks."""
    from app.planning.task_dag import TaskInputBindingError
    result = copy.deepcopy(extraction)
    if not isinstance(result, dict):
        return result
    def bind(source):
        if not isinstance(source, dict):
            raise TaskInputBindingError('计算操作数必须是对象')
        if 'source_task_index' in source:
            index = source.pop('source_task_index')
            if type(index) is not int or not 0 <= index < task_index:
                raise TaskInputBindingError('计算输入必须引用本问题内前序任务')
            source['source_task_id'] = f'task-{task_offset + index + 1}'
        elif 'source_task_id' not in source and len(dependencies) == 1:
            source['source_task_id'] = dependencies[0]
        if source.get('source_task_id') not in dependencies:
            raise TaskInputBindingError('计算输入必须属于声明的依赖任务')
    binding = result.get('计算绑定')
    if binding is not None:
        try:
            roles = arithmetic_roles(binding)
        except ValueError as exc:
            raise TaskInputBindingError(str(exc)) from exc
        for role in roles:
            operand = binding.get(role)
            if isinstance(operand, dict) and 'value' in operand:
                from app.numeric_operations import literal
                try:
                    if set(operand) != {'value'}:
                        raise ValueError('数值常量不能混用任务引用或字段')
                    literal(operand['value'])
                except ValueError as exc:
                    raise TaskInputBindingError(str(exc)) from exc
                continue
            if not isinstance(operand, dict) or not isinstance(operand.get('column'), str) or not operand['column'].strip():
                raise TaskInputBindingError('计算绑定缺少输入字段')
            bind(operand)
    spec = result.get('结果操作')
    if spec is not None:
        if not isinstance(spec, dict):
            raise TaskInputBindingError('结果操作必须是对象')
        bind(spec)
        try:
            spec['operation'] = normalize_result_operation(spec.get('operation'))
            validate_result_operation(spec.get('operation'))
        except ValueError as exc:
            raise TaskInputBindingError(str(exc)) from exc
    return result


def validate_result_operation(operation):
    """Only the existing, bounded data operations; no SQL/code or nested plans."""
    operation = normalize_result_operation(operation)
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
        if step['type'] in {'sort', 'sort_limit'}:
            if not fields([step.get('field')]) or type(step.get('descending', True)) is not bool:
                raise ValueError('排序操作缺少明确字段或方向')
        if step['type'] in {'limit', 'sort_limit'}:
            if type(step.get('count')) is not int or not 1 <= step['count'] <= 10000:
                raise ValueError('结果限制必须是1至10000的整数')
        if step['type'] == 'monthly_growth':
            from app.services.period_computation import monthly_window
            monthly_window(step)
            if step.get('method') not in {'arithmetic','compound'}:
                raise ValueError('请确认平均月度增长率采用算术平均还是复合平均')
            if (not fields([step.get(key) for key in ('field','period_field','result_field')])
                    or not isinstance(step.get('group_by',[]),list) or not fields(step.get('group_by',[]))):
                raise ValueError('月度增长缺少明确指标、月份或分组字段')
        elif step['type'] == 'derive':
            from app.numeric_operations import validate_derive
            validate_derive(step)
        elif step['type'] == 'aggregate':
            groups,specs = step.get('group_by',[]),step.get('aggregations')
            if (not isinstance(groups,list) or not fields(groups) or len(groups)>20
                    or not isinstance(specs,list) or not 1<=len(specs)<=20):
                raise ValueError('聚合计算缺少分组或指标定义')
            for spec in specs:
                if (not isinstance(spec,dict)
                        or not fields([spec.get('field')]) or ('alias' in spec and not fields([spec['alias']]))):
                    raise ValueError('聚合计算缺少明确函数或字段')
                from app.numeric_operations import validate_aggregation
                validate_aggregation(spec)
    return steps


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
    except ValueError as exc:
        raise TaskInputBindingError(str(exc)) from exc
    query = copy.deepcopy(task.structured)
    query.pop('结果操作')
    query['排序'] = []
    query['限制'] = None
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
        '结果操作':{'source_task_index':0,'operation':copy.deepcopy(spec['operation'])},
        '排序':copy.deepcopy((task.structured or {}).get('排序') or []),
        '限制':(task.structured or {}).get('限制')})
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
    query['排序'] = []
    query['限制'] = None
    query['输出要求'] = f'仅返回{left}和{right}，派生结果由后续计算任务生成'
    query['是否查询数据库'] = '是'
    for role in ('numerator','denominator'):
        binding[role]['source_task_index'] = 0
    compute = DecomposedTask(question=f'根据上述任务的查询结果计算{label}，{left}除以{right}，不再查询数据库',
        name=f'计算{label}'[:60], depends_on=(0,), expected_output=label,
        intent=PrimaryIntent.COMPARISON_ANALYSIS, structured={
            '意图':'指标计算','指标':[{'name':label}], '是否查询数据库':'否','计算绑定':binding,
            '排序':copy.deepcopy((task.structured or {}).get('排序') or []),
            '限制':(task.structured or {}).get('限制')})
    source = replace(task, name=f'统计{left}和{right}'[:60],structured=query,
                     expected_output=f'{left}和{right}')
    return replace(decomposition,tasks=(source,compute))
