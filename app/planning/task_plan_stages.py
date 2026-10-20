"""Internal task boundaries are fixed before intent/parameter extraction."""
from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskBoundary(BaseModel):
    # Ignore legacy combined responses, but never consume their extraction here.
    model_config = ConfigDict(extra='ignore')
    question: str = Field(min_length=2, max_length=1000)
    name: str | None = Field(default=None, max_length=60)
    depends_on: list[int] = Field(default_factory=list)
    expected_output: str | None = Field(default=None, max_length=300)
    metric_targets: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    sample_requirements: list[str] = Field(default_factory=list)
    component_input: bool = False
    preserve_ranking: bool = False
    postprocess: dict[str, Any] | None = None
    clarification: dict[str, Any] | None = None


class TaskBoundaries(BaseModel):
    model_config = ConfigDict(extra='ignore')
    tasks: list[TaskBoundary] = Field(min_length=1, max_length=12)


PLANNING_PROMPT = '''你是任务规划器。先确定完成原问题所需的业务任务、输入依赖、结果粒度和交付物，只输出JSON任务计划。
本阶段不判断任务意图、不提取extraction，不输出实体/字段/过滤条件JSON，不生成ASL或SQL。
metric_targets仅记录任务要交付的规范登记指标名；grain记录结果中一行代表什么，只用业务名称。

任务边界规则：
1. 依据最终交付物和真实数据依赖决定任务数量。一个业务问题可以需要多个任务；一份数据集不等于整个目标已经完成。普通原子指标查询、同一粒度的普通多指标查询可以保持一个任务。
2. 规范登记了依赖成分的复合指标，先按各成分自己的口径取数，再计算目标指标。复合指标做分布、排名、趋势或比较时仍保留计算依赖。不得用“查询均价明细，供后续分析”代替完整计划。
3. 按订单/客户等逐对象计算的指标必须保留规范规定的对象粒度及唯一业务键。分布分析需要逐样本数据，不能用整批数据的一个加权均值代替；时间范围不等于时间分组，价格分布不等于时间趋势。
4. 每个任务的question完整保留对它有效的时间、筛选、分组和交付要求。复合指标成分按各自登记口径分配条件；共用区域分母不按经销商分组。不得凭字段名称猜业务口径。
5. 先排名或筛选对象、再查询这些对象的其他关系或明细时，拆为前置查询和依赖查询。不得把后续返回对象加入前置排名的分组，否则会改变排名粒度。后续任务不继承前置Top N限制。同一对象的唯一关联展示属性可在原查询返回，不为展示字段强制拆任务。
6. 不关联的独立任务不消费彼此结果。有依赖时保留原问题明确的共享时间和筛选范围；用户明确指定不同范围时按各自要求。depends_on从0开始且只引用排在前面的任务。
7. 根据已有结果直接计算、筛选或投影且无须新数据时，作为依赖任务，question写明“不再查询数据库”。需要前序数据集中没有的新字段或新关系时应查询数据库，不得用前序表代替新结果。
8. 不把ASL、SQL、取数、校验、洞察等内部技术步骤拆成业务任务；不重复或遗漏用户要求交付的结果。分析可以由最后的计算任务交付，不为流程名称增加空任务。
9. name是动词加业务目标的陈述句，expected_output写明可验收结果。metric_targets必须使用规范登记原名；解释口径、血缘或闲聊任务不展开指标依赖。
10. 若已有上一轮完整结果可提供参数，不重复原查询；采样不能当成完整名单。仅在完整性已确认时复用具体参数。
11. postprocess仅用于明确可执行的既有数据操作，格式为{"source_task_index":0,"operation":{...}}；operation只支持derive、aggregate、filter、select、sort、sort_limit、limit或其pipeline。不得自造distribution、distribution_analysis、binning等操作。普通分布描述并入最终计算任务的交付物，由现有结果分析完成，不另造无法执行的分析任务。需要确认的计算口径保留clarification。禁止添加数据库字段、编程代码或未给出的业务条件。
'''


PARAMETER_PROMPT = '''任务边界已经确定。请为给定任务逐项提取extraction，只输出JSON。
不得新增、删除、合并任务，不得修改任务问题、依赖和交付物；按task_index对应原任务。
任务意图在本次提取中顺带给出，不另做一次识别调用；它用于执行路由，不能删除规划已确定的指标或结果粒度。
每个任务的参数以该任务范围为准，原问题只用于解释共享上下文；不可把其他独立任务的筛选条件混入。
任务要求交付已注册指标时，即使是明细输出，也必须保留该指标的计算语义。需要聚合的指标按指标查询提取；原始属性明细继续按明细查询提取。
逐对象样本需要保留任务指定的分组和唯一业务键；指标登记的聚合后有效样本条件不能改成逐行过滤。
expected_output是必须完成的交付物，把分析目标保留在输出要求。计算任务必须给出当前规范支持的公式及前序任务输入，不得只写“根据结果计算”。
sample_requirements是父复合指标登记的有效样本口径，必须在每个成分任务中保留，按相同分组聚合后筛选，不能只约束其中一份输入或改成逐行剔除负数。
component_input=true表示复合指标的成分统计取数，按指标查询提取；指标及分组按计划保留，不能改成原始金额/数量明细。成分统计不提前排序取前N，最终排名由计算任务完成；明确的前置入选范围仍通过任务依赖传递。
只使用当前规范中的业务对象；物理字段和关系最终绑定交给原有ASL链路。无法确定业务含义时沿用现有澄清方式，不编造参数。
输出格式：{"tasks":[{"task_index":0,"extraction":{...}}]}，每个任务恰好一项。
'''


def metric_definition(spec: str, name: str) -> str:
    for line in spec.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) >= 3 and cells[0] == name:
            return cells[2]
    return ''


def canonical_formula_spec(spec: str, catalog) -> str:
    """Registered aliases in a formula do not require another inference call."""
    registered, aliases = catalog
    terms = sorted(set(registered) | set(aliases), key=len, reverse=True)
    if not terms:
        return spec
    pattern = re.compile('|'.join(re.escape(term) for term in terms))
    lines = []
    for line in spec.splitlines():
        cells = line.split('|')
        if len(cells) > 4 and cells[1].strip() in registered:
            cells[3] = pattern.sub(lambda match: aliases.get(match[0], match[0]), cells[3])
            line = '|'.join(cells)
        lines.append(line)
    return '\n'.join(lines)


def targets_in_text(text: str, registered, aliases) -> list[str]:
    found = []
    covered = []
    for term in sorted(set(registered) | set(aliases), key=len, reverse=True):
        if len(term) < 2 or term not in text or any(term in item for item in covered):
            continue
        covered.append(term)
        name = aliases.get(term, term)
        if name not in found:
            found.append(name)
    return found


def boundary_targets(task, catalog) -> list[str]:
    registered, aliases = catalog
    # An explicit empty list is meaningful: a dependent detail lookup may
    # mention its parent's ranking measure without returning that measure.
    names = (task.metric_targets if 'metric_targets' in task.model_fields_set else
             targets_in_text(task.expected_output or task.question, registered, aliases))
    return list(dict.fromkeys(aliases.get(name, name) for name in names))


def grouping_labels(spec: str) -> set[str]:
    """Business field labels, never example values or metric-table cells."""
    result = set()
    columns = []
    headers = {'实体','实体名称','维度','维度名称','属性','属性名称','字段','字段名称'}
    for line in spec.splitlines():
        if not line.lstrip().startswith('|'):
            columns=[]
            continue
        cells=[cell.strip() for cell in line.strip().strip('|').split('|')]
        if any(cell in headers for cell in cells):
            columns=[index for index,cell in enumerate(cells) if cell in headers or '属性' in cell]
            continue
        if not cells or set(''.join(cells)) <= {'-',':',' '}:
            continue
        for index in columns:
            if index < len(cells):
                result.update(part.strip() for part in re.split(r'[,，、;；]',cells[index]) if part.strip())
    return result


def _attach_result_analysis(boundaries, catalog):
    """Keep narrative insight with its producing task, not an empty operation."""
    registered, aliases = catalog
    result, indices = [], {}
    for old_index, original in enumerate(boundaries):
        task = original.model_copy(deep=True)
        if any(type(dep) is not int or dep < 0 or dep >= old_index for dep in task.depends_on):
            raise ValueError('任务依赖必须引用排在其前面的任务')
        task.depends_on = list(dict.fromkeys(indices[dep] for dep in task.depends_on))
        def targets(item):
            return set(boundary_targets(item, catalog))
        narrative = ('不再查询数据库' in task.question and len(task.depends_on) == 1
                     and re.search(r'分布|洞察|描述', task.question)
                     and targets(task) == targets(result[task.depends_on[0]]) and bool(targets(task)))
        executable = False
        if task.postprocess:
            from app.planning.calculation_bindings import validate_result_operation
            try:
                validate_result_operation(task.postprocess.get('operation'))
                executable = True
            except ValueError:
                pass
        if narrative and not executable:
            parent_index = task.depends_on[0]
            parent = result[parent_index]
            goal = task.expected_output or task.question
            parent.expected_output = (goal + ('；取数交付：' + parent.expected_output if parent.expected_output else ''))[:300]
            indices[old_index] = parent_index
            continue
        if task.postprocess and 'source_task_index' in task.postprocess:
            old_source = task.postprocess['source_task_index']
            if old_source in indices:
                task.postprocess['source_task_index'] = indices[old_source]
        indices[old_index] = len(result)
        result.append(task)
    return result


def _bind_local_calculations(boundaries, *, spec, catalog, dependencies):
    """An operation referencing preceding inputs is local even without a phrase."""
    registered, aliases = catalog
    result = [task.model_copy(deep=True) for task in boundaries]
    def targets(task):
        return set(boundary_targets(task, catalog))
    for index, task in enumerate(result):
        if any(type(dep) is not int or dep < 0 or dep >= index for dep in task.depends_on):
            raise ValueError('任务依赖必须引用排在其前面的任务')
        postprocess = task.postprocess or {}
        compound_targets = [name for name in targets(task) if name in dependencies]
        if task.depends_on and '不再查询数据库' in task.question and not postprocess:
            available = set().union(*(targets(result[dep]) for dep in task.depends_on))
            formula_ready = bool(compound_targets) and all(
                set(dependencies[name]).issubset(available) for name in compound_targets)
            narrative_ready = bool(targets(task)) and targets(task).issubset(available) and bool(
                re.search(r'分布|洞察|描述', task.question))
            if not formula_ready and not narrative_ready:
                # A phrase cannot make unavailable data appear in an upstream
                # result. With no executable operation/formula, retain a query.
                task.question = task.question.replace('，不再查询数据库', '').replace('不再查询数据库', '')
        if not (task.depends_on and postprocess.get('source_task_index') in task.depends_on):
            continue
        if '不再查询数据库' not in task.question:
            task.question += '，不再查询数据库'
        if not compound_targets:
            continue
        from app.planning.calculation_bindings import validate_result_operation
        try:
            validate_result_operation(postprocess.get('operation'))
        except ValueError:
            # A known registered formula supplies the operation. Do not accept
            # invented expression/steps syntax from a model as executable code.
            task.postprocess = None
        for name in compound_targets:
            definition = metric_definition(spec,name)
            grain = re.findall(r'(?:先)?按([^，。；、/÷]{1,24}?)(?:分别)?汇总',definition)
            task.grain = list(dict.fromkeys([*task.grain,*grain]))
            if len(task.depends_on) == 1:
                source = result[task.depends_on[0]]
                if set(dependencies[name]).issubset(targets(source)):
                    source.grain = list(dict.fromkeys([*source.grain,*grain]))
                    source.component_input = True
                    source.preserve_ranking = True  # existing input scope stays authoritative
                    if re.search(r'[<>]|大于|小于|排除|剔除|仅|有效',definition):
                        source.sample_requirements = list(dict.fromkeys([*source.sample_requirements,definition]))
    return result


def complete_boundaries(boundaries: list[TaskBoundary], *, spec: str, catalog, dependencies,
                        source_question: str = '') -> list[TaskBoundary]:
    """Expand registered dependencies while no intent or extraction exists yet."""
    registered, aliases = catalog
    boundaries = _bind_local_calculations(boundaries,spec=spec,catalog=catalog,dependencies=dependencies)
    boundaries = _attach_result_analysis(boundaries, catalog)
    known_groups = grouping_labels(spec)
    result = []
    terminal_by_old = {}
    external_edges = []
    for old_index, source in enumerate(boundaries):
        if any(type(dep) is not int or dep < 0 or dep >= old_index for dep in source.depends_on):
            raise ValueError('任务依赖必须引用排在其前面的任务')
        task = source.model_copy(deep=True)
        targets = boundary_targets(task, catalog)
        # A planner may list formula inputs while its deliverable still asks
        # for the composite result. Recover that final target before extraction.
        delivery = task.expected_output or task.question
        delivery = re.split(r'用于|供后续|以便|用来', delivery, maxsplit=1)[0]
        inferred = targets_in_text(delivery, registered, aliases)
        explicit = targets_in_text(source_question or task.question, registered, aliases)
        for name in inferred:
            if name in dependencies and name not in targets:
                targets = [target for target in targets if target not in dependencies[name] or target in explicit]
                targets.append(name)
        task.metric_targets = list(dict.fromkeys(aliases.get(name, name) for name in targets))
        if known_groups:
            task.grain = [name for name in task.grain if name in known_groups or name in {
                '秒','分钟','小时','日','周','月','季度','半年','年','月份'}]
        # Metadata and local calculations are not new database component queries.
        local = '不再查询数据库' in task.question
        metadata = re.search(r'^(?:请)?(?:解释|说明|介绍|定义|查看口径|查询血缘)', task.question)
        compound = [name for name in task.metric_targets if name in dependencies
                    and all(part in registered for part in dependencies[name])]
        if local or metadata or not compound or task.clarification:
            external_edges.append(len(result))
            source_index = len(result)
            if task.postprocess and not local and not task.clarification:
                from app.planning.calculation_bindings import validate_result_operation
                validate_result_operation(task.postprocess.get('operation'))
                query = task.model_copy(deep=True, update={'postprocess': None,
                    'expected_output': '返回后续计算需要的完整指标和分组数据'})
                result.append(query)
                compute = TaskBoundary(question=f'根据上述任务的查询结果完成{task.expected_output or task.question}，不再查询数据库',
                    name='计算查询结果', depends_on=[source_index], expected_output=task.expected_output,
                    grain=list(task.grain), postprocess={'source_task_index':source_index,
                    'operation':copy.deepcopy(task.postprocess['operation'])})
                result.append(compute)
                terminal_by_old[old_index] = [len(result)-1]
            else:
                terminal_by_old[old_index] = [source_index]
                result.append(task)
            continue
        grains = {}
        for name in compound:
            grain = list(task.grain)
            for match in re.finditer(r'(?:先)?按([^，。；、/÷]{1,24}?)(?:分别)?汇总', metric_definition(spec, name)):
                if match[1] not in grain:
                    grain.append(match[1])
            grains[name] = grain
        other = [name for name in task.metric_targets if name not in compound]
        queries = []
        keys_by_metric = {}
        for name in compound:
            definition = metric_definition(spec, name)
            sample_scope = definition if re.search(r'[<>]|大于|小于|排除|剔除|仅|有效', definition) else ''
            keys_by_metric[name] = [(part, tuple(grains[name]), sample_scope) for part in dependencies[name]]
            queries.extend(key for key in keys_by_metric[name] if key not in queries)
        other_keys = [(name, tuple(task.grain), '') for name in other]
        queries.extend(key for key in other_keys if key not in queries)
        component_indices = {}
        for key in queries:
            name, grouping, _ = key
            grain = list(grouping)
            wording = task.question
            for target in sorted(task.metric_targets, key=len, reverse=True):
                wording = wording.replace(target, name)
            for alias, canonical in aliases.items():
                if canonical in task.metric_targets:
                    wording = wording.replace(alias, name)
            question = f'统计{name}，范围与分组：{wording}'
            if grain:
                question += '，按' + '、'.join(grain) + '汇总'
            component_indices[key] = len(result)
            external_edges.append(len(result))
            result.append(TaskBoundary(question=question, name=f'统计{name}', depends_on=list(task.depends_on),
                metric_targets=[name], grain=grain, sample_requirements=[key[2]] if key[2] else [], component_input=True,
                expected_output='按' + '、'.join(grain) + '返回' + name if grain else name))
        terminals = [component_indices[key] for key in other_keys]
        for name in compound:
            indices = [component_indices[key] for key in keys_by_metric[name]]
            terminals.append(len(result))
            result.append(TaskBoundary(question=f'根据上述任务的查询结果计算{name}，完成原要求：{task.question}，不再查询数据库',
                name=f'计算{name}', depends_on=indices, metric_targets=[name], grain=grains[name],
                expected_output=task.expected_output or task.question))
        terminal_by_old[old_index] = terminals
    for index in external_edges:
        operation = result[index].postprocess
        if operation and result[index].depends_on and 'source_task_index' in operation:
            sources = terminal_by_old.get(operation['source_task_index'], [])
            if len(sources) != 1:
                raise ValueError('结果操作无法唯一绑定前序问题的输出')
            operation['source_task_index'] = sources[0]
        result[index].depends_on = list(dict.fromkeys(new for old in result[index].depends_on for new in terminal_by_old[old]))
    if len(result) > 12:
        raise ValueError('任务展开超过现有12任务上限，请减少本轮业务目标')
    return result


def parameter_items(payload: dict, boundaries: list[TaskBoundary]) -> list[dict | None]:
    """Join extraction by stable task index; text cannot replace a planned task."""
    items = payload.get('tasks')
    if len(boundaries) == 1 and items == [] and payload.get('intent') in {
            'CHAT', 'CAPABILITY_HELP', 'OUT_OF_SCOPE', '闲聊', '非数据任务'}:
        return [None]
    if not isinstance(items, list):
        if len(boundaries) == 1 and 'extraction' in payload:
            return [payload.get('extraction')]
        raise ValueError('参数提取必须覆盖已规划的每个任务')
    result = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError('任务参数格式错误')
        index = item.get('task_index', position)
        if type(index) is not int or not 0 <= index < len(boundaries) or index in result:
            raise ValueError('任务参数下标缺失或重复')
        result[index] = copy.deepcopy(item.get('extraction'))
    if set(result) != set(range(len(boundaries))):
        raise ValueError('参数提取遗漏已规划任务')
    return [result[index] for index in range(len(boundaries))]
