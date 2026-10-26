"""Internal task boundaries are fixed before parameter extraction."""
from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class TaskOutline(BaseModel):
    """The planning model owns task wording and edges, never query parameters."""
    model_config = ConfigDict(extra='ignore')
    question: str = Field(min_length=2, max_length=1000)
    name: str | None = Field(default=None, max_length=60)
    depends_on: list[int] = Field(default_factory=list)
    expected_output: str | None = Field(default=None, max_length=300, description=(
        '本任务需要新查询或计算的交付内容；依赖任务引用的前序入选条件放在question中，'
        '不要当成本任务的新计算目标。若本步仍需按指标计算、排名或筛选，必须写明该指标。'))
    definition_only: bool = False


class TaskOutlines(BaseModel):
    model_config = ConfigDict(extra='ignore')
    tasks: list[TaskOutline] = Field(min_length=1, max_length=12)


class TaskBoundary(TaskOutline):
    # Internal semantic-definition hints used to expand component tasks.
    # They are not accepted from the planning model or executable parameters.
    metric_targets: list[str] = Field(default_factory=list)
    grain: list[str] = Field(default_factory=list)
    sample_requirements: list[str] = Field(default_factory=list)
    component_input: bool = False
    preserve_ranking: bool = False
    postprocess: dict[str, Any] | None = None
    clarification: dict[str, Any] | None = None
    # Derived only from the selected metric's current registered definition.
    # It is not an ASL field binding and cannot be supplied by a model.
    _whole_object_scope: tuple[str, str, str] | None = PrivateAttr(default=None)
    _cohort_key: str | None = PrivateAttr(default=None)


class TaskBoundaries(BaseModel):
    model_config = ConfigDict(extra='ignore')
    tasks: list[TaskBoundary] = Field(min_length=1, max_length=12)


PLANNING_PROMPT = '''你是任务规划器。依据本轮补全后的完整问题、已识别任务意图和当前模型的语义文件，确定业务任务、输入依赖与交付物，只输出JSON任务计划。

一、输入与职责
- 用户消息是本轮完整问题；后文动态提供已识别任务意图、当前语义文件正文、可选的通用规则和[上一轮任务表]，最后附JSON Schema。文件正文已经提供时直接使用，不要求访问文件路径。没有历史块就没有可用历史结果。
- 沿用已识别意图，不重新判断或输出意图。用户问题决定要交付什么；当前语义文件解释实体关系、指标名称、公式、单位及适用范围。使用登记原名，不从其他模型或示例借用口径；文件未提供的定义、关联和历史结果不可编造。
- 本阶段只写自然语言任务，不输出extraction、metric_targets、grain、实体、指标、维度、展示字段、过滤条件、时间粒度、排序、限制、计算绑定、postprocess、结果操作等结构化参数，不生成ASL、SQL或代码；这些全部由后续参数提取阶段生成。

二、唯一输出格式
外层为{"tasks":[...]}，共1～12个任务，不加Markdown、解释或思考过程。每项都输出以下五个字段，字段类型遵守末尾Schema：
- question：2～1000字的完整自然语言，写清本步目标、适用条件、输入来源；仅写本步需要执行的业务。
- name：动词加业务目标，不超过60字。
- depends_on：整数数组，下标从0开始，只引用本计划中排在前面的任务；无依赖为[]。历史任务不是本计划下标。
- expected_output：不超过300字，可验收的本步交付结果。
- definition_only：布尔值，普通任务false，仅说明定义/口径的任务true。
格式示意：{"tasks":[{"question":"解释所问指标的统计口径","name":"解释指标口径","depends_on":[],"expected_output":"所问指标的含义、公式与适用范围","definition_only":true}]}。示意文字不能替代实际问题与指标名称。

三、先定交付与粒度，再拆任务
1. 任务数量由交付物及真实数据依赖决定；同范围、同粒度的普通原子指标或多指标查询可合为一个任务。属性、名单、明细不能擅改成指标统计。同口径、同范围、同粒度的取数或计算只安排一次；不同年份、群体或口径的比较不是重复任务。
2. 整体/总计/单个对象的问题最终返回相应整体结果，不自行增加经销商、医院、日期、科室等明细分组。必要粒度也可来自排名、趋势、比较、分布或登记的逐对象计算，不限于出现“各/每/按”字样；中间计算保留必要粒度，不能据此扩大最终交付范围。
3. 筛选不等于分组：“骨科那边”限定查询范围，不自动按科室分组；日期范围不等于按日期分组，价格分布不等于时间趋势。展示姓名及唯一编码用于识别同一对象，不擅自改变统计对象。
4. 当前语义文件登记了成分依赖的复合指标，按各成分口径取数，再计算目标；不因名称含“平均/率/时长”就猜公式、拆空任务。复合指标用于排名、趋势、比较或分布时仍保留计算依赖。用户明确给出的临时算式可按所给运算规划，不冒充登记指标。成分任务的question和expected_output只写本步成分指标，不附“作为某复合指标的分子/分母”等目标说明；目标复合指标只在最终计算任务提出，避免把成分任务再次拆同一公式。
5. 各成分按自己的登记口径保留范围与粒度，不强制完全相同。例如经销商的区域医院覆盖率可用经销商粒度分子和区域粒度共享分母，分母不按经销商分组；只问上海市整体时不改成各经销商覆盖率。不能平均各组比率冒充整体比率，不能凭名称改变登记公式。
6. 成分任务保留完整的必要对象及唯一业务键，使计算能对齐；目标是复合指标Top N时，在成分齐全并算出目标指标后才排名，不能分别取销售额Top5和订单数Top5再相除。

四、条件完整，作用范围准确
7. 逐项保留业务对象及完整名称、日期起止、地域、包含/排除、数值阈值及单位、排名指标/方向/名次、入选范围和最终交付。每项条件必须出现在适用任务的question中，不能仅放name或expected_output，不能用“指定医院/指定时间”省略真实值。保留用户字面量，数值与单位换算交后续按登记类型处理，不自行把3个月写成90天。
8. 共享条件在适用任务逐项保留；用户分别要求不同年份或医院时各自保留。只用于选人的“2025年销售排名”是后续查所属公司的入选背景，不应变成公司归属查询的订单日期筛选。若后续仍查同一销售期间的产品，则必须继续保留该期间和医院。不能把背景中的条件一律继承或一律删除。
9. 区分选择对象的条件与对象内部计算范围。若登记要求整张订单全部商品的金额/数量，而用户用某商品选择订单：先取满足商品、医院、时间等条件的完整去重订单号名单，再依据该名单统计每张订单全部商品，最后计算。商品条件只用于选单；不可继续限制整单成分。客户、合同等对象同理。

五、依赖与本步交付
10. 先排名/筛选，再查询入选对象的新关系或明细时，拆成前置任务和依赖任务。前置任务保留入选对象的唯一业务键；不得把后续返回对象加入前置排名的分组，例如产品或公司。唯一关联展示属性能在原查询可靠返回时可一并返回，不为展示字段强制拆任务；若尚未取得，则只用一个依赖查询统一补充。
11. depends_on列出本任务实际使用的直接输入任务；多个输入必须全部列出。不关联的独立任务不消费彼此结果，无须互相依赖，可并行执行。不为展示顺序制造依赖，不依赖自身或后面的任务。
12. 依赖question用“根据前序……任务返回的完整名单/结果”说明输入来自何种业务任务、只处理哪些入选对象，并保留本步适用条件；依赖数组不能代替此限定。不要编造尚未查询出的姓名、编码或数值。前置Top1仅决定入选人员数量，不得变成产品只取1条；并列名次按用户或登记规则处理。
13. expected_output只写本步新增交付及必要对象标识，不把前序选人指标再当成本步计算目标。查入选人员公司只写姓名、编码及公司名称；不能再次要求计算前五排名。若本步确需计算新指标、在2024年重算或按指标筛选，即使最终只返回名单，也须写明新指标、范围及筛选/排名要求。
14. 仅对已有结果计算、筛选或投影且无需新数据时，question必须原样包含“不再查询数据库”，并写清输入来源、运算目标、口径和排序要求。输入所缺的新字段或新关系必须安排查询，不能声称从已有表中得到。纯数字计算可无数据库依赖。
15. 方差、标准差、中位数、分位数、相关系数等先取完整样本及必要粒度，再计算；保留总体/样本口径，不能对汇总的单个总数假装做样本统计。平方、开根、对数、取余等用本地计算，输入仍为登记指标或字段，不把临时计算结果名当成数据库指标。普通分布描述并入最终计算交付；不把ASL、SQL、校验、洞察等内部步骤单列为空业务任务。

六、说明与历史复用
16. 只问含义、口径、公式、血缘而不求数值的任务，definition_only=true，question写明具体解释对象；不拆取数、成分或计算。既查数值又解释的组合请求分别保留数据任务和说明任务，不能整体变成解释。
17. [上一轮任务表]通常只含摘要/采样。仅明确标记“全部结果已完整提供”且所需字段可见，才复用具体名单，避免重复查询；未证明完整、被省略或来自其他模型/业务域的结果不能当本轮完整输入。缺少所需完整名单时按原口径取得它，不编造历史值，不用预览样本冒充全量。保留新问题要求的范围。

七、业务示例（仅示意，指标、公式、对象键以当前语义文件为准）
A. “给出2025年订单平均金额最高的前五位销售人员，以及他们所在的销售公司”：若文件将平均每单销售金额登记为含税销售总额/订单笔数，则先并行取2025年各业务员的两项完整成分，每项附姓名、唯一编码，不提前Top5；再依赖两项计算均额、降序取前5，不再查询数据库；若公司尚未返回，最后依据入选名单统一查公司。最后一步expected_output仅为“入选业务员姓名、编码及所属公司名称”，不重复公式，也不对公司表加2025年订单筛选。
B. “2025年9月到12月期间，上海交通大学医学院附属新华医院销售订单金额最多的销售员是谁？销售了哪些产品？”：先在该完整医院名称及期间内按登记销售金额选销售员第1名，返回身份；再查询前序入选销售员在相同医院、相同期间销售的全部产品。两步question都写出实际医院名称和期间，第二步不能查全院所有产品或只取1条产品。
C. “统计上海市区域医院覆盖率”：若当前登记口径支持区域整体覆盖率，分子查询上海市有效订单去重医院数，分母查询上海市区域全部医院总数，再计算覆盖率；两个输入均保留上海市，不额外拆各经销商分布。若用户问“上海地区各经销商”，分子才保留经销商粒度，共享区域分母仍按登记范围查询一次。

输出前自检（不输出检查过程）：交付是否齐全；每步条件、粒度及唯一对象是否正确；是否重复拆公式或误传背景条件/Top N；依赖是否指向全部所需的前序输入；本步expected_output是否准确；历史完整性是否有证据；是否只有1～12项、五字段的合法JSON。
'''

PARAMETER_PROMPT = '''任务边界已经确定。请为给定任务逐项提取extraction，只输出JSON。
不得新增、删除、合并任务，不得修改任务问题、依赖和交付物；按task_index对应原任务。
意图只由意图识别节点提供，本阶段不判断、不修改、不输出意图或intent字段。任务职责只决定查库、计算等执行参数，不能改变已识别意图。你是结构化参数的唯一生成阶段；指标、维度、展示字段、筛选、时间、排序、限制和计算参数均在本阶段确定，后续不会用规划字段补齐或覆盖，空列表也保留其原意。
规划只提供自然语言任务与依赖，内部附带的登记指标成分、粒度和样本口径仅用于理解语义文件，不能把业务对象或筛选字段直接当成维度。只有用户要求的分组或登记指标计算确实需要的样本粒度才提取为维度。例：“看看骨科那边卖得怎么样”提取骨科筛选，不因这一筛选自动添加“适用科室”维度。
每个任务的参数以该任务范围为准，原问题只用于解释共享上下文；不可把其他独立任务的筛选条件混入。
提取完成前逐项对照任务question：日期范围必须进入时间粒度，医院/地域/商品等限定进入过滤条件，明确的排名指标、方向及Top N进入排序和限制；不能只在输出要求中描述这些约束。前序选出的对象通过已有依赖机制传递，不能编造尚未查询出的姓名或编码，也不能省略入选对象限定。用户问“金额最多的销售员”时，前置查询必须保留按金额降序取第1名；后续“销售了哪些产品”保留相同时间、医院和前序入选销售员，不沿用产品Top1。没有要求排名、时间分组或新增过滤时不要自行添加。
definition_only=true的任务只解释口径，是否查询数据库为“否”；保留指标名称，不提取实际取数、计算绑定或结果操作。指标已经登记不影响解释意图。
逐对象样本需要保留任务指定的分组和唯一业务键；指标登记的聚合后有效样本条件不能改成逐行过滤。
expected_output是必须完成的交付物，把分析目标保留在输出要求。计算任务必须给出当前规范支持的公式及前序任务输入，不得只写“根据结果计算”。
sample_requirements是父复合指标登记的有效样本口径，必须在每个成分任务中保留，按相同分组聚合后筛选，不能只约束其中一份输入或改成逐行剔除负数。
component_input=true表示复合指标的成分统计取数，提取聚合取数参数但不改判意图；指标及分组按计划保留，不能改成原始金额/数量明细。成分统计不提前排序取前N，最终排名由计算任务完成；明确的前置入选范围仍通过任务依赖传递。
先选对象再汇总完整对象的任务，只沿用前序完整对象名单，不继承用来筛选入选对象的商品等明细条件。按订单号汇总全部商品时，查询维度和展示字段只保留订单号，不能再按商品分组；时间范围在前置名单中落实，不把整单拆成时间内的部分明细。
只使用当前规范中的业务对象；物理字段和关系最终绑定交给原有ASL链路。无法确定业务含义时沿用现有澄清方式，不编造参数。
输出格式：{"tasks":[{"task_index":0,"extraction":{...}}]}，每个任务恰好一项。
'''


def metric_definition(spec: str, name: str) -> str:
    for line in spec.splitlines():
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) >= 3 and cells[0] == name:
            return cells[2]
    return ''


def registered_metric_conditions(spec: str, targets, catalog) -> tuple[str, ...]:
    """Exact numeric predicates from the selected metric's current definition.

    These are grounding evidence for task wording, never ASL bindings. A
    number by itself does not authorize a different field/operator/threshold.
    """
    registered, aliases = catalog
    operators = {'大于等于':'>=','小于等于':'<=','不等于':'!=','大于':'>','小于':'<','等于':'=',
                 '>=':'>=','<=':'<=','!=':'!=','<>':'!=','>':'>','<':'<','=':'='}
    result = []
    for predicate in registered_metric_predicates(spec,targets,catalog):
        canonical=predicate['field'];number=str(predicate['value'][0]);operator=predicate['op']
        names = [canonical,*(alias for alias,name in aliases.items() if name==canonical)]
        for name in names:
            for spelling,normalized in operators.items():
                if normalized == operator:
                    result.append(name + spelling + number)
    return tuple(dict.fromkeys(result))


def registered_metric_predicates(spec: str, targets, catalog) -> list[dict]:
    registered, aliases = catalog
    terms = sorted(set(registered) | set(aliases), key=len, reverse=True)
    if not terms:
        return ()
    operators = {'大于等于':'>=','小于等于':'<=','不等于':'!=','大于':'>','小于':'<','等于':'=',
                 '>=':'>=','<=':'<=','!=':'!=','<>':'!=','>':'>','<':'<','=':'='}
    pattern = re.compile('(' + '|'.join(re.escape(term) for term in terms) + r')\s*('
        + '|'.join(re.escape(op) for op in operators) + r')\s*([+-]?\d+(?:\.\d+)?)')
    result = []
    for target in dict.fromkeys(targets):
        if target not in registered:
            continue
        for field,operator,number in pattern.findall(metric_definition(spec,target)):
            value=float(number) if '.' in number else int(number)
            predicate={'field':aliases.get(field,field),'op':operators[operator],'value':[value]}
            if predicate not in result:
                result.append(predicate)
    return result


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
    if 'metric_targets' in task.model_fields_set:
        names = task.metric_targets
    else:
        # A dependent query's question also describes how its input cohort was
        # selected. Only its own deliverable requests new metrics; otherwise
        # "companies of the top five by average amount" expands that average
        # a second time. Local calculations, definitions and older outlines
        # without a deliverable retain the existing wording-based behavior.
        texts = (task.question, task.expected_output or '')
        if (task.depends_on and task.expected_output and task.expected_output.strip()
                and not task.definition_only and '不再查询数据库' not in task.question):
            texts = (task.expected_output,)
        # Purpose clauses describe a downstream result, not this task's inputs.
        wording = '；'.join(re.split(r'用于|供后续|以便|用来', text, maxsplit=1)[0]
                           for text in texts)
        names = targets_in_text(wording, registered, aliases)
    return list(dict.fromkeys(aliases.get(name, name) for name in names))


def component_task_title(task: TaskBoundary, extraction: dict) -> str | None:
    """Describe the actual component query using its extracted business scope."""
    if not task.component_input:
        return task.name
    temporal = extraction.get('时间粒度') or {}
    time_range = temporal.get('time_range') if isinstance(temporal, dict) else None
    time_text = time_range if isinstance(time_range, str) else ''
    def names(key):
        return [str(item.get('name') or item.get('field') or '') if isinstance(item, dict) else str(item)
                for item in extraction.get(key) or []]
    grain, metrics = names('维度'), names('指标')
    grouping = '各' + '、'.join(grain) + '的' if grain else ''
    title = f'统计{time_text}{grouping}' + '、'.join(metrics)
    labels = []
    for item in extraction.get('展示字段') or []:
        label = str(item.get('field') or item.get('name') or '') if isinstance(item, dict) else str(item)
        if label and label not in {*grain, *metrics} and label not in labels:
            labels.append(label)
    if labels:
        title += '，附' + '、'.join(labels)
    return title[:60]


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
        analysis_targets = targets(task) or set(targets_in_text(
            task.expected_output or task.question,registered,aliases)) or set(targets_in_text(task.question,registered,aliases))
        narrative = (not task.definition_only and '不再查询数据库' in task.question and len(task.depends_on) == 1
                     and re.search(r'分布|洞察|描述', task.question)
                     and analysis_targets == targets(result[task.depends_on[0]]) and bool(analysis_targets))
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
            if not re.search(r'分布|洞察|描述',parent.question):
                parent.question += '，交付要求：' + goal
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
        if task.definition_only:
            continue
        if any(type(dep) is not int or dep < 0 or dep >= index for dep in task.depends_on):
            raise ValueError('任务依赖必须引用排在其前面的任务')
        postprocess = task.postprocess or {}
        compound_targets = [name for name in targets(task) if name in dependencies]
        if compound_targets and postprocess:
            from app.planning.calculation_bindings import validate_result_operation
            try:
                validate_result_operation(postprocess.get('operation'))
            except ValueError:
                task.postprocess = None
                postprocess = {}
        if compound_targets and task.depends_on:
            available = set().union(*(targets(result[dep]) for dep in task.depends_on))
            keys = {key for name in compound_targets for key in re.findall(
                r'(?:先)?按([^，。；、/÷]{1,24}?)(?:分别)?汇总',metric_definition(spec,name))}
            key_lists_only = bool(keys) and all(not targets(result[dep]) and any(re.search(
                re.escape(key) + r'(?:完整|去重|的)*(?:名单|列表)',
                (result[dep].expected_output or '') + result[dep].question) for key in keys)
                for dep in task.depends_on)
            if key_lists_only and any(not set(dependencies[name]).issubset(available) for name in compound_targets):
                # Declared local code cannot obtain monetary/quantity inputs
                # from a predecessor that only promises object identities.
                task.postprocess = None
                postprocess = {}
        # Local operation parameters are supplied later by extraction.
        available = set().union(*(targets(result[dep]) for dep in task.depends_on)) if task.depends_on else set()
        formula_ready = bool(compound_targets) and all(
            set(dependencies[name]).issubset(available) for name in compound_targets)
        if not (task.depends_on and (postprocess.get('source_task_index') in task.depends_on or formula_ready)):
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
            for dep in task.depends_on:
                source = result[dep]
                if set(dependencies[name]).intersection(targets(source)):
                    source.grain = list(dict.fromkeys([*source.grain,*grain]))
                    source.component_input = True
                    if re.search(r'[<>]|大于|小于|排除|剔除|仅|有效',definition):
                        source.sample_requirements = list(dict.fromkeys([*source.sample_requirements,definition]))
    return result


def _whole_object_scope(definition: str, known_groups: set[str]):
    """Recognize an explicit registered whole-object aggregation, not a name guess."""
    match = re.search(r'(?:先)?按([^，。；、/÷]{1,24}?)(?:分别)?汇总该'
                      r'([^，。；、/÷]{1,12}?)下(?:的)?(?:全部|所有)'
                      r'([^，。；、/÷]{1,12}?)的', definition)
    if match and all(label in known_groups for label in (match[1], match[3])):
        return match[1], match[2], match[3]
    return None


def _complete_object_scopes(boundaries, *, spec, catalog, dependencies, source_question='', default_times=()):
    """Separate member eligibility from a registered whole-object denominator.

    This runs while only task boundaries exist. Ordinary formulas and unfiltered
    whole-object queries retain their existing plan. Scope comes from the current
    registered definition; physical keys/values still go through authorized ASL.
    """
    known = grouping_labels(spec)
    scopes = {}
    for index, task in enumerate(boundaries):
        if task.definition_only:
            continue
        for name in boundary_targets(task, catalog):
            if name not in dependencies:
                continue
            scope = _whole_object_scope(metric_definition(spec, name), known)
            if not scope:
                continue
            local = '不再查询数据库' in task.question
            sources = task.depends_on if local else [index]
            for source in sources:
                if (not local or set(boundary_targets(boundaries[source], catalog))
                        .intersection(dependencies[name])):
                    scopes[source] = scope
    result, indices = [], {}
    for old_index, original in enumerate(boundaries):
        task = original.model_copy(deep=True)
        task.depends_on = list(dict.fromkeys(indices[dep] for dep in task.depends_on))
        if task.postprocess and 'source_task_index' in task.postprocess:
            old_source = task.postprocess['source_task_index']
            if old_source in indices:
                task.postprocess['source_task_index'] = indices[old_source]
        scope = scopes.get(old_index)
        if scope:
            key, parent, member = scope
            # A declared key-list dependency is already the eligibility query.
            selectors = [dep for dep in task.depends_on if not boundary_targets(result[dep], catalog)
                         and key in (result[dep].question + (result[dep].expected_output or ''))]
            selected_members = re.search(r'采购|购买|包含|含有|涉及|销售过|买过|卖过', task.question)
            if selectors or selected_members:
                if not selectors:
                    selection_question = source_question or task.question
                    for default in dict.fromkeys(default_times):
                        if default not in selection_question:
                            selection_question += '，时间范围：' + default
                    selection = TaskBoundary(
                        question=f'筛选符合下述问题中入选条件的{parent}，仅查询去重{key}完整名单，不计算均价或分析分布：{selection_question}',
                        name=f'筛选入选{parent}完整名单', depends_on=list(task.depends_on),
                        metric_targets=[], grain=[key],
                        expected_output=f'符合原问题入选条件的完整去重{key}名单；不取预览或前N条')
                    selectors = [len(result)]
                    result.append(selection)
                for selector in selectors:
                    result[selector]._cohort_key = key
                task.depends_on = selectors
                targets = '、'.join(boundary_targets(task, catalog))
                task.question = (f'根据上述任务返回的{key}完整名单，按{key}汇总该{parent}下全部{member}的{targets}，'
                                 f'只沿用入选{key}名单，不继承用于入选的{member}筛选条件或另截取部分明细')
                task.expected_output = (f'按{key}返回整{parent}全部{member}的{targets}'
                    if task.component_input and not any(name in dependencies for name in boundary_targets(task,catalog))
                    else task.expected_output or original.question)
                task.grain = [key]
                task._whole_object_scope = scope
        indices[old_index] = len(result)
        result.append(task)
    return result


def complete_boundaries(boundaries: list[TaskBoundary], *, spec: str, catalog, dependencies,
                        source_question: str = '', default_times=()) -> list[TaskBoundary]:
    """Expand registered dependencies while no intent or extraction exists yet."""
    registered, aliases = catalog
    fixed = []
    for original in boundaries:
        task = original.model_copy(deep=True)
        references = {str(dep) for dep in task.depends_on} | {str(dep+1) for dep in task.depends_on}
        def reference(match):
            return '前序任务' if (match[1] or match[2]) in references else match[0]
        # Inserting a required eligibility/component task changes positions.
        # The typed edges are authoritative; do not retain obsolete text indices.
        task.question = re.sub(r'任务(\d+)|第(\d+)个任务',reference,task.question)
        if task.expected_output:
            task.expected_output = re.sub(r'任务(\d+)|第(\d+)个任务',reference,task.expected_output)
        fixed.append(task)
    boundaries = fixed
    boundaries = _bind_local_calculations(boundaries,spec=spec,catalog=catalog,dependencies=dependencies)
    boundaries = _attach_result_analysis(boundaries, catalog)
    boundaries = _complete_object_scopes(boundaries,spec=spec,catalog=catalog,dependencies=dependencies,
                                          source_question=source_question,default_times=default_times)
    known_groups = grouping_labels(spec)
    result = []
    terminal_by_old = {}
    external_edges = []
    for old_index, source in enumerate(boundaries):
        if any(type(dep) is not int or dep < 0 or dep >= old_index for dep in source.depends_on):
            raise ValueError('任务依赖必须引用排在其前面的任务')
        task = source.model_copy(deep=True)
        if task.definition_only:
            task.postprocess = None
            task.clarification = None
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
        metadata = task.definition_only or re.search(r'^(?:请)?(?:解释|说明|介绍|定义|查看口径|查询血缘)', task.question)
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
        # A registered whole-object formula shares membership, grouping and
        # valid-sample scope across its inputs. Return those inputs together.
        joint = (task._whole_object_scope and len(compound)==1 and not other
                 and len({(key[1],key[2]) for key in queries})==1)
        if len(compound)==1 and not other and len({(key[1],key[2]) for key in queries})==1:
            from app.planning.calculation_bindings import registered_arithmetic_binding
            formula = registered_arithmetic_binding(canonical_formula_spec(spec,catalog),
                                                    compound[0],dependencies[compound[0]])
            if formula and formula['operation'] != 'divide':
                # Components of this one request share scope and grain. Ask
                # the existing query service for them together, preserving its
                # common population instead of inventing missing groups/zeros
                # while combining two independently aggregated populations.
                joint = True
        query_groups = [queries] if joint else [[key] for key in queries]
        for query_group in query_groups:
            names = [key[0] for key in query_group]
            name, grouping, sample = '、'.join(names), query_group[0][1], query_group[0][2]
            grain = list(grouping)
            wording = task.question
            # Keep the final ranking as context. Replacing its measure with an
            # input would turn "average Top5" into "sales/count Top5".
            question = f'统计{name}，时间、筛选及关联展示范围参考原要求（最终指标排名不在本任务执行）：{wording}'
            if grain:
                question += '，按' + '、'.join(grain) + '汇总'
            question += '；本任务返回完整成分数据，不提前按原问题的最终指标排名或截断，最终计算及排名由后续任务完成'
            for key in query_group:
                component_indices[key] = len(result)
            external_edges.append(len(result))
            component = TaskBoundary(question=question, name=f'统计{name}', depends_on=list(task.depends_on),
                metric_targets=names, grain=grain, sample_requirements=[sample] if sample else [], component_input=True,
                expected_output='按' + '、'.join(grain) + '返回' + name if grain else name)
            component._whole_object_scope = task._whole_object_scope
            result.append(component)
        terminals = [component_indices[key] for key in other_keys]
        for name in compound:
            indices = list(dict.fromkeys(component_indices[key] for key in keys_by_metric[name]))
            terminals.append(len(result))
            goal = task.expected_output if task._whole_object_scope else task.question
            result.append(TaskBoundary(question=f'根据上述任务的查询结果计算{name}，完成原要求：{goal}，不再查询数据库',
                name=(f'计算{name}，完成{task.name or task.question}')[:60], depends_on=indices, metric_targets=[name], grain=grains[name],
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
