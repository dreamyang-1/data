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


def source_field_gaps(question, fields, *, intents=()):
    """Remove exact registered outputs only; never substring-match source names."""
    allowed = set().union(*(OUTPUTS.get(intent, set()) for intent in intents))
    allowed = {name.casefold() for name in allowed}
    result = []
    for field in fields:
        field = str(field).strip()
        parts = [part.strip() for part in re.split(r'[、，,；;]', field) if part.strip()]
        if parts and all(part.casefold() in allowed for part in parts):
            continue
        if field and field not in result:
            result.append(field)
    return tuple(result)
