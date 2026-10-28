"""Registered analytical outputs are not source-table field requirements."""
import re
from app.domain.models import PrimaryIntent as I

OUTPUTS = {
    I.TREND_ANALYSIS: {'环比', '同比', '环比变化率', '同比变化率', '稳健斜率', '方向一致性', '拐点', '波动系数'},
    I.COMPARISON_ANALYSIS: {'差额', '增减额', '变化率', '倍数', '基期100指数'},
    I.COMPOSITION_ANALYSIS: {'占比', '累计贡献', '累计占比', '集中度', 'HHI', '等效分组数', '分布熵', '份额变化'},
    I.ANOMALY_ANALYSIS: {'异常分析', '异常分析方法', '异常检测方法', '命中方法', '检验值', '阈值',
        '判定依据', '异常月份', '异常日期', '异常点', 'MAD', 'IQR', 'Z分数'},
    I.ROOT_CAUSE_ANALYSIS: {'归因分析', '增减贡献', '净变化贡献率', '绝对贡献份额', '拉升', '拖累', '抵消', '未解释残差'},
    I.FORECAST_ANALYSIS: {'预测分析', '回测依据', '适用范围', '预测值', '预测方法', '预测模型',
        '回测误差', 'MAE', 'MAPE', 'RMSE', '平均偏差', '残差参考区间'},
}
GUIDANCE = '''
分析能力与源数据字段必须分层判断：趋势、对比、占比、异常、归因、预测均为已登记分析链路。
覆盖度只检查取数必需的业务实体、指标、属性、维度、筛选与关系。
分析工具根据已绑定数据生成的阈值、检验值、判定依据、回测依据、适用范围、预测值、贡献等，
不要求数据库或语义模型存在同名字段，不得将这些输出要求列为未定义字段。
例如含税销售总额与业务日期已定义时，异常检测的阈值／检验值、预测回测依据不是目录缺口。
若还要求未定义的业务指标（如患者疗效、授信额度），这些真实源数据缺口仍必须明确列出，
不能因出现“预测”“异常”就放行。样本不足、算法适用性由后续分析工具检查。
'''


# 交付形态和聚合说法不是目录字段：前者是“要明细/名单”，后者是
# “某对象有多少、共多少钱”，都不是规范里新登记的业务字段
_SHAPE_CORE = r'(?:明细|列表|名单|清单|记录|数据|信息|内容|条目)'
_QUERY_SHAPE = re.compile(
    rf'^(?:全部|所有|完整|具体|当前|各|每)?(?:{_SHAPE_CORE})+'
    rf'(?:数|量|数量|条数|总数|总量|个数|条|项)?$'
)
_METRIC_SUFFIX = re.compile(r'(?:数量|总数|总量|个数|笔数|金额|总额)$')
# 具体对象名称是查询范围，不是规范里登记的字段
_INSTANCE_SUFFIX = re.compile(
    r'(?:有限公司|有限责任公司|股份有限公司|集团公司|集团|公司|医院|卫生院|诊所|'
    r'医疗中心|研究院|研究所|学院|大学|工厂|厂|门店|药店)$'
)
# 以信息/数据/明细等结尾的是输出形态，去掉后缀后要能对应回问题里的对象
_OUTPUT_SUFFIX = re.compile(r'(?:信息|数据|明细|列表|名单|清单|记录|内容)$')


def _is_output_wording(field: str, question: str) -> bool:
    """名称只是交付形态、聚合口径或对象实例，不足以证明目录缺字段。"""
    if _QUERY_SHAPE.match(field):
        return True
    for pattern in (_METRIC_SUFFIX, _OUTPUT_SUFFIX):
        match = pattern.search(field)
        # 去掉后缀后仍要在原问题里指同一个对象，才按输出说法处理
        if match and match.start() >= 2 and field[:match.start()] in question:
            return True
    if field in question and _INSTANCE_SUFFIX.search(field):
        return True
    return False


def source_field_gaps(question, fields, *, intents=()):
    """Remove exact registered outputs only; never substring-match source names."""
    allowed = set().union(*(OUTPUTS.get(intent, set()) for intent in intents))
    allowed = {name.casefold() for name in allowed}
    result = []
    for field in fields:
        field = str(field).strip()
        parts = [part.strip() for part in re.split(r'[、，,；;]', field) if part.strip()]
        if parts and all(
            part.casefold() in allowed or _is_output_wording(part, question)
            for part in parts
        ):
            continue
        if field and field not in result:
            result.append(field)
    return tuple(result)
