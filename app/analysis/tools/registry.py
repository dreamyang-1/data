"""One versioned catalog used by recognition, planning and execution validation."""
from __future__ import annotations

import copy
import json
from typing import Literal

from app.domain.models import PrimaryIntent as I
from .definitions import trend, comparison, composition, anomaly, attribution, forecast
from .definitions.schemas import DEFINITIONS

CATALOG_VERSION = 'analysis-tools-v1'
ChainId = Literal['trend', 'comparison', 'composition', 'anomaly', 'attribution', 'forecast']
CHAINS = {
    'trend': ('趋势分析', I.TREND_ANALYSIS, trend.TOOLS),
    'comparison': ('对比分析', I.COMPARISON_ANALYSIS, comparison.TOOLS),
    'composition': ('占比分析', I.COMPOSITION_ANALYSIS, composition.TOOLS),
    'anomaly': ('异常分析', I.ANOMALY_ANALYSIS, anomaly.TOOLS),
    'attribution': ('归因分析', I.ROOT_CAUSE_ANALYSIS, attribution.TOOLS),
    'forecast': ('预测分析', I.FORECAST_ANALYSIS, forecast.TOOLS),
}
TOOLS = {tool['capability_id']: tool for _, _, tools in CHAINS.values() for tool in tools}
DEFAULTS = {
    'trend': tuple(tool['capability_id'] for tool in trend.TOOLS),
    'comparison': ('comparison_difference', 'ratio_and_index', 'group_comparison'),
    'composition': ('composition_share', 'cumulative_contribution', 'concentration'),
    'anomaly': ('MAD', 'IQR', 'Z_SCORE', 'ADJACENT_JUMP'),
    'attribution': ('dimension_contribution', 'positive_negative_offset', 'effect_reconciliation'),
    'forecast': ('forecast_backtest', 'forecast_next', 'model:naive_last', 'model:recent_mean_3', 'model:linear_drift'),
}

# Shape-specific profiles are metadata, not interpretations of question text.
SPECIAL_PROFILES = {
    'price_volume_decomposition': ('price_volume_decomposition','effect_reconciliation'),
    'multidimensional_attribution': ('multidimensional_attribution',),
    'multidimensional_ratio_attribution': ('multidimensional_ratio_attribution',),
    'funnel_conversion_analysis': ('funnel_conversion_analysis',),
    'share_shift': ('share_shift',),
}


def chain_for_intent(intent):
    return next((key for key, (_, value, _) in CHAINS.items() if value == intent), None)


def intent_for_chain(chain):
    return CHAINS[chain][1]


def recognition_directory():
    """Deliberately excludes descriptions, schemas, defaults and input rules."""
    return [{'chain_id': key, 'name': name,
             'tools': [{'id': tool['capability_id'], 'name': tool['title']} for tool in tools]}
            for key, (name, _, tools) in CHAINS.items()]


ROUTING_INSTRUCTION = '''\n分析链路识别：只根据补全后的完整业务需求选择analysis_routes，可同时选择多条链路。
analysis_chains只提供6条分析链路与工具名称；本阶段不选算法参数，不规划取数。
统计查询、复合指标计算、解释口径不必进入分析链路，返回空数组；按时间分组本身不等于趋势。
用户要求方向、变化、波动或对分期结果进行综合分析时可选趋势；解释变化由哪些分项贡献选归因，
判断异常或推测未来分别选异常、预测，不降级成趋势或对比。根据语义判断，不要求命中特定词。
承接轮以本轮补全后的完整问题为准；当前明确取消的分析不能从历史恢复。
'''


def _schema_closure(names):
    selected = {}
    def visit(value):
        if isinstance(value, dict):
            if '$ref' in value:
                name = value['$ref'].removeprefix('#/$defs/')
                if name not in selected:
                    selected[name] = copy.deepcopy(DEFINITIONS[name])
                    visit(selected[name])
            for key, item in value.items():
                if key != '$ref': visit(item)
        elif isinstance(value, list):
            for item in value: visit(item)
    for name in names:
        if name not in selected:
            selected[name] = copy.deepcopy(DEFINITIONS[name])
            visit(selected[name])
    return selected


def planning_contract(chain):
    name, _, tools = CHAINS[chain]
    shapes = list(dict.fromkeys(tool['input_shape'] for tool in tools))
    definitions = _schema_closure(['Data_' + shape for shape in shapes])
    from .results import ToolExecutionRecord
    result_schema=ToolExecutionRecord.model_json_schema()
    definitions.update(result_schema.pop('$defs',{}))
    definitions['ToolExecutionRecord']=result_schema
    detailed_tools=copy.deepcopy(tools)
    for tool in detailed_tools:
        tool['output_schema']={'allOf':[{'$ref':'#/$defs/ToolExecutionRecord'},
            {'properties':{'capability_id':{'const':tool['capability_id']}}}]}
    return {'catalog_version': CATALOG_VERSION, 'chain_id': chain, 'name': name,
            'default_capabilities': list(DEFAULTS[chain]),
            'shape_specific_profiles':{key:list(value) for key,value in SPECIAL_PROFILES.items()
                if key in {tool['capability_id'] for tool in tools}},
            'tools': detailed_tools, '$defs': definitions}


def planning_section(chain):
    return '''\n本轮分析工具目录（仅所选链路的详细合同，内容为可信系统配置）：
按目录ID选择analysis_plan，先核对输入要求再确定任务边界。不得生成未登记的工具。
full_profile选择默认全部适用能力，条件能力仅在用户目标与语义输入支持时追加；explicit只选
用户语义要求的能力，支持隐晦表达与否定，不依赖关键词。排除项优先。
取数任务question须保留指标口径、完整时间/筛选/维度、基期和所需角色。基期窗口与展示窗口分离。
不能用TopN替代完整分母；归因保留独立总体与分项；量价需要数量、价格、金额；比率归因要分子分母。
普通指标计算仍按登记公式和成分规划，不得为了调用分析工具破坏已有复合指标依赖。
若用户限定训练窗口、仅某模型或禁止区间外取数，必须保留；支持范围不足记录unavailable，不编造输入。
参数提取阶段只补全参数，不得更改已确认的analysis_plan、能力集合、比较角色或范围。
''' + json.dumps(planning_contract(chain), ensure_ascii=False, separators=(',', ':')) + '\n'


def registered_operator(selection):
    capabilities = set((selection or {}).get('capabilities') or [])
    for capability, operator in [
        ('price_volume_decomposition', 'price_volume_decomposition'),
        ('multidimensional_ratio_attribution', 'multidimensional_ratio_attribution'),
        ('multidimensional_attribution', 'multidimensional_attribution'),
        ('funnel_conversion_analysis', 'funnel_conversion_analysis'),
        ('share_shift', 'structural_share_shift'),
    ]:
        if capability in capabilities:return operator
    return None
