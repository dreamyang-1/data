"""Finalize calculator contracts inside planning, before query extraction."""
from __future__ import annotations

import copy
import json


CALCULATION_PLAN_PROMPT = '''你是任务规划器的计算规划阶段。任务及依赖已经展开完毕，在查询参数提取之前确定每个计算任务的完整执行方案。
输入包含完整问题、已识别意图、当前模型语义文件、全部任务及交付物，以及已按登记公式绑定的计算方案。
只为calculation_task_indices中的任务输出JSON：{"tasks":[{"task_index":2,"calculation":{"计算绑定":{...},"排序":[],"限制":null}}]}。
不得增加、删除、合并任务或改变依赖。每个输入指标、来源任务、操作数顺序、输出列、筛选及排名都在本阶段确定，参数提取不会补算式或补依赖。
registered_bindings是当前语义文件的确定公式，必须使用；未给出时依据本任务、前序交付和语义文件规划。只查询所属公司等新属性的任务不重新计算前序排名。
引用前序的登记指标原名或计划明确返回的展示字段；每份参与逐对象计算的数据必须保留该对象唯一编码，不能按姓名、行位置或相同编码值混用不同实体。数据集ID及实际业务值等执行后才知道的信息不能编造。
计算结果需要排名时，calculation.排序固定为[{"field":"最终结果列","order":"desc或asc"}]，calculation.限制是整数或null。例如前五名写"排序":[{"field":"平均每单销售金额","order":"desc"}],"限制":5。不要把sort_limit、descending、count写入排序数组；这些是结果操作内部的格式。保留来源结果中的对象编码和展示属性供后续任务使用。
只输出计算绑定、结果操作、排序、限制；计算绑定与结果操作二选一。已有analysis_plan的专门分析由其工具执行，不重造同比、预测等操作；已登记复合指标的算式仍须完整计算。
'''


async def plan_calculations(boundaries, *, question, system_content, spec, catalog,
                            dependencies, request_model):
    """All index references use the final, expanded task table."""
    from app.planning.calculation_bindings import (
        arithmetic_roles, normalize_result_operation, registered_arithmetic_binding,
    )
    from app.planning.task_plan_stages import canonical_formula_spec

    indices = [i for i, task in enumerate(boundaries)
               if not task.definition_only and '不再查询数据库' in task.question]
    if not indices:
        return {}
    required = list(dict.fromkeys(name for i in indices for name in boundaries[i].metric_targets
                                  if name in dependencies))
    # Exact published formulas are compiled here. Multi-step or prose formulas
    # are planned with the complete task table below, never by extraction.
    canonical_spec = canonical_formula_spec(spec, catalog)
    formulas = {name: binding for name in required
                if (binding := registered_arithmetic_binding(canonical_spec, name, dependencies[name]))}
    registered = {}
    for index in indices:
        task = boundaries[index]
        for name in task.metric_targets:
            if name not in formulas:
                continue
            binding = copy.deepcopy(formulas[name])
            for role in arithmetic_roles(binding):
                operand = binding[role]
                if set(operand) == {'value'}:
                    continue
                sources = [dep for dep in task.depends_on
                           if operand['column'] in boundaries[dep].metric_targets]
                if len(sources) != 1:
                    break
                operand['source_task_index'] = sources[0]
            else:
                registered[index] = binding

    request = {'original_question': question,
               'tasks': [dict(task_index=i, **task.model_dump(exclude_none=True))
                         for i, task in enumerate(boundaries)],
               'calculation_task_indices': indices,
               'registered_bindings': registered}
    response = await request_model('single-question-calculation-plan',
        CALCULATION_PLAN_PROMPT + CALCULATOR_CONTRACT_PROMPT + system_content
        + '\n计算规划输入JSON：\n' + json.dumps(request, ensure_ascii=False), question)
    result = {}
    for item in response.get('tasks', []):
        index = item.get('task_index')
        if type(index) is not int or index not in indices or index in result:
            raise ValueError('计算规划任务下标与已规划任务不一致')
        value = item.get('calculation')
        if not isinstance(value, dict):
            raise ValueError('计算规划未提供执行方案')
        value = {key: copy.deepcopy(value[key]) for key in
                 ('计算绑定', '结果操作', '排序', '限制') if key in value}
        if index in registered and not value.get('结果操作'):
            value.pop('结果操作', None)
            value['计算绑定'] = copy.deepcopy(registered[index])
            value['计算绑定来源'] = 'REGISTERED_COMPOUND'
        if isinstance(value.get('结果操作'), dict):
            operation = value['结果操作']
            operation['operation'] = normalize_result_operation(operation.get('operation'))
        # Translate calculator sort syntax into the existing execution slots.
        # The rank/count was chosen by planning, never inferred from extraction.
        for sort in value.get('排序') or []:
            if isinstance(sort, dict) and sort.get('type') in {'sort', 'sort_limit'}:
                if sort.get('order') is None and 'descending' in sort:
                    descending = sort.pop('descending')
                    if descending in (True, 'true', 'True'):
                        sort['order'] = 'desc'
                    elif descending in (False, 'false', 'False'):
                        sort['order'] = 'asc'
                if sort.get('type') == 'sort_limit' and value.get('限制') is None:
                    value['限制'] = sort.get('count')
                sort.pop('type', None)
                sort.pop('count', None)
        result[index] = value
    if set(result) != set(indices):
        raise ValueError('计算规划遗漏已确定的计算任务')
    return result


def planned_calculation_parameters(task, calculation):
    """Adapt the fixed plan to the existing execution API without an LLM."""
    names = list(task.metric_targets)
    binding = calculation.get('计算绑定') or {}
    if binding.get('result_field') and binding['result_field'] not in names:
        names.append(binding['result_field'])
    return {'意图': '指标计算', '实体': [], '指标': [{'name': name} for name in names],
            '维度': list(task.grain), '展示字段': [], '过滤条件': [],
            '时间粒度': {'unit': None, 'time_range': None, 'anchor': None},
            '排序': [], '限制': None, '是否查询数据库': '否',
            '输出要求': task.expected_output or task.question, **copy.deepcopy(calculation)}


CALCULATOR_CONTRACT_PROMPT = """计算器执行合同（由规划器确定，仅引用前序结果，不改变数据库字段绑定）：
每个纯计算任务必须明确实际运算，不能只写“根据结果计算”。
两个指标加减乘除使用calculation.计算绑定：{"operation":"add或subtract或multiply或divide","left":{"source_task_index":0,"column":"左操作数指标登记名"},"right":{"source_task_index":1,"column":"右操作数指标登记名"},"result_field":"结果指标名","format":"number或percent"}。原有除法也兼容operation=divide配合numerator和denominator操作数。
同一查询结果逐行运算时，两操作数引用同一个前序任务；跨任务运算时分别引用各自来源。source_task_index为本问题从0开始的任务下标，必须在depends_on中；不能引用自身或猜测数据集ID。左右操作数或分子、分母顺序严格依据当前规范中的公式，不能按依赖指标列的排列顺序猜测。多步公式按已规划依赖引用前一步结果，或在同一任务内使用完整pipeline，不新增任务。
其他计算可使用calculation.结果操作：{"source_task_index":0,"operation":{"type":"pipeline","operations":[...]}}。支持derive、aggregate、filter、select、sort、sort_limit、limit；不得输出SQL、Python或任意表达式。
derive双操作数为left_field/right_field，也可用left_value/right_value显式数值常量，字段与常量每侧只能选一种。常量不得冒充业务字段。单操作数使用field，或明确的value数值常量；结果字段为result_field。
单操作数支持square平方、cube立方、sqrt平方根、cbrt立方根、abs绝对值、negate取负、reciprocal倒数、sign符号、exp/exp2/expm1指数、ln自然对数、log10/log2/log1p、floor向下取整、ceil向上取整、trunc截断、round四舍五入（digits默认0，可为-15至15）、factorial阶乘、sin/cos/tan/asin/acos/atan/sinh/cosh/tanh/asinh/acosh/atanh、degrees/radians角度转换、bit_not按位取反。三角函数默认弧度，角度须先radians转换。
双操作数除四则运算外支持power乘方、root开右侧指定次数的根、modulo取余、floor_divide向下整除、log指定右侧底数、hypot、atan2、minimum/maximum逐行较小/较大值、gcd/lcm、combination组合数、permutation排列数、bit_and/bit_or/bit_xor/shift_left/shift_right整数位运算。
计算绑定也支持以上运算，双操作数沿用left/right；单操作数用input:{"source_task_index":0,"column":"实际结果列"}，或input:{"value":9}；二元常量也是{"value":数值}。数据必须引用当前依赖任务结果；纯数字计算可以没有查询依赖。
aggregate按group_by分组、aggregations逐项计算。每项field/function/alias；支持count/sum/avg/min/max、count_distinct、median、quantile（q为0至1，默认0.5）、percentile（percentile为0至100，默认50）、variance_population/variance_sample、stddev_population/stddev_sample、range、product、sum_squares、rms、geometric_mean、harmonic_mean、covariance_population/covariance_sample/correlation（成对字段另填other_field）。未指定的variance/stddev默认总体口径（除以n），样本口径除以n-1，输出中说明所用口径；分位数用(n-1)*q线性插值。
方差、标准差等样本统计必须先取得完整所需粒度的样本（例如逐订单金额），再aggregate；不能先查询总金额再把单个总数当样本。多步按依赖或最多5步pipeline组合，例如平方→分组平均→开根得到均方根，禁止把表达式写在字段名里。
排序操作格式为{"type":"sort","field":"实际结果列名","descending":true}；取前N格式为{"type":"limit","count":N}；合并排序取前N格式为{"type":"sort_limit","field":"实际结果列名","descending":true,"count":N}。最终按计算结果排名时，成分取数任务不得先按其他指标取前N；只在用户明确先选定对象范围时保留该范围限制。
排名必须保留最终计算字段的排序方向和限制人数；按人或组织计算时，所有相应取数任务都保留对象分组及必要展示字段，不能变成全局总计；共用分母按规范保留原有范围。公式或字段依据不明时不要编造计算绑定。
同比、环比、趋势、占比、异常、归因、预测等分析类计算不属于“结果操作”，也不是“计算绑定”：这类问题沿用已规划的取数任务与analysis_plan，不得为分析计算输出任何结果操作类型（包括自造的“同比”“环比”“增长率”等），分析由系统专门的分析工具完成。
"""
