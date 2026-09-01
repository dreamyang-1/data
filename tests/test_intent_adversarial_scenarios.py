import pytest

from app.domain.models import PrimaryIntent, TrustedIdentity
from app.intent.classifier import RuleBasedIntentClassifier


IDENTITY = TrustedIdentity(tenant_id="test", user_id="user")


CASES = [
    # 口语化指标查询
    ("哥们儿帮我瞅瞅这个月销售额有多少", PrimaryIntent.METRIC_QUERY),
    ("本月到底卖了多少钱啊？？？", PrimaryIntent.METRIC_QUERY),
    ("查 下 本 月 销 售 额", PrimaryIntent.METRIC_QUERY),
    ("2026年8月订单量给个数", PrimaryIntent.METRIC_QUERY),
    ("本月销售额TOP10供应商", PrimaryIntent.METRIC_QUERY),
    ("医用口罩科室匹配度前十供应商", PrimaryIntent.METRIC_QUERY),
    # 明细
    ("把昨天每一笔订单摊开给我", PrimaryIntent.DETAIL_QUERY),
    ("本周退款记录有哪些", PrimaryIntent.DETAIL_QUERY),
    ("列出今天订单名单", PrimaryIntent.DETAIL_QUERY),
    ("列出昨天退款失败的订单号和原因", PrimaryIntent.DETAIL_QUERY),
    ("我要原始数据，不要汇总", PrimaryIntent.DETAIL_QUERY),
    # 趋势
    ("近半年销售额是涨是跌", PrimaryIntent.TREND_ANALYSIS),
    ("按月看看今年销售额变化", PrimaryIntent.TREND_ANALYSIS),
    ("销售额走势给我拉一下", PrimaryIntent.TREND_ANALYSIS),
    ("我不是要预测，只看过去12个月趋势", PrimaryIntent.TREND_ANALYSIS),
    # 对比
    ("本月和上月销售额差多少", PrimaryIntent.COMPARISON_ANALYSIS),
    ("华东华南谁卖得更多", PrimaryIntent.COMPARISON_ANALYSIS),
    ("今年销售额同比怎么样", PrimaryIntent.COMPARISON_ANALYSIS),
    ("比较A店和B店本周订单量", PrimaryIntent.COMPARISON_ANALYSIS),
    # 占比
    ("各渠道销售额占多少", PrimaryIntent.COMPOSITION_ANALYSIS),
    ("本月品类销售构成", PrimaryIntent.COMPOSITION_ANALYSIS),
    ("退款金额按原因的比例", PrimaryIntent.COMPOSITION_ANALYSIS),
    # 异常与归因的边界
    ("找出最近突然暴跌的门店", PrimaryIntent.ANOMALY_ANALYSIS),
    ("本月订单量有没有不正常", PrimaryIntent.ANOMALY_ANALYSIS),
    ("销售额为什么突然下降", PrimaryIntent.ROOT_CAUSE_ANALYSIS),
    ("哪些因素导致退款率变高", PrimaryIntent.ROOT_CAUSE_ANALYSIS),
    ("查出异常订单明细", PrimaryIntent.DETAIL_QUERY),
    # 预测及否定
    ("基于过去两年预测未来3个月销售额", PrimaryIntent.FORECAST_ANALYSIS),
    ("估一下下季度订单量", PrimaryIntent.FORECAST_ANALYSIS),
    ("下月销售额会不会超过100万", PrimaryIntent.FORECAST_ANALYSIS),
    ("下月预算值是多少", PrimaryIntent.METRIC_QUERY),
    ("不要预测，展示历史走势", PrimaryIntent.TREND_ANALYSIS),
    ("不预测未来，只看历史毛利率走势", PrimaryIntent.TREND_ANALYSIS),
    ("按历史走势估算四季度销售额", PrimaryIntent.FORECAST_ANALYSIS),
    # 元数据、质量、报告
    ("客单价究竟怎么算", PrimaryIntent.METRIC_DEFINITION),
    ("退款率的口径解释一下", PrimaryIntent.METRIC_DEFINITION),
    ("销售额来自哪张表哪个字段", PrimaryIntent.DATA_LINEAGE),
    ("这个指标的数据链路是什么", PrimaryIntent.DATA_LINEAGE),
    ("这个报表中的客户数数据源是什么", PrimaryIntent.DATA_LINEAGE),
    ("为什么今天的数据还没更新", PrimaryIntent.DATA_QUALITY),
    ("检查订单数据是否完整", PrimaryIntent.DATA_QUALITY),
    ("客户数在报表和财务侧对不上", PrimaryIntent.DATA_QUALITY),
    ("检测销售明细中的空值和脏数据", PrimaryIntent.DATA_QUALITY),
    ("生成一份本月经营月报", PrimaryIntent.REPORT_GENERATION),
    ("给领导出个季度销售报告", PrimaryIntent.REPORT_GENERATION),
    # 闲聊、帮助、越界
    ("你好呀", PrimaryIntent.CHAT),
    ("谢谢，辛苦了", PrimaryIntent.CHAT),
    ("你能分析哪些数据", PrimaryIntent.CAPABILITY_HELP),
    ("这种问题应该怎么问你", PrimaryIntent.CAPABILITY_HELP),
    ("你会做归因分析吗", PrimaryIntent.CAPABILITY_HELP),
    ("怎么让你生成销售报表", PrimaryIntent.CAPABILITY_HELP),
    ("删除数据库里的全部订单", PrimaryIntent.OUT_OF_SCOPE),
    ("帮我把销售额改成一百万", PrimaryIntent.OUT_OF_SCOPE),
    ("执行drop table order_info", PrimaryIntent.OUT_OF_SCOPE),
    # 标点、混合语言、噪声
    ("【急】查一下！！！本月销售额？？", PrimaryIntent.METRIC_QUERY),
    ("sales report：生成本月销售报告", PrimaryIntent.REPORT_GENERATION),
    ("同比 YoY 看一下今年销售额", PrimaryIntent.COMPARISON_ANALYSIS),
    ("top 10 供应商，本月销售额", PrimaryIntent.METRIC_QUERY),
    ("嗯……那个……最近半年销售额趋势吧", PrimaryIntent.TREND_ANALYSIS),
    # 容易误判的反事实/引用表达
    ("解释一下‘销售额’的定义，不是查数", PrimaryIntent.METRIC_DEFINITION),
    ("不要给明细，只告诉我本月订单量", PrimaryIntent.METRIC_QUERY),
    ("不是分析下降原因，我只想看下降月份", PrimaryIntent.TREND_ANALYSIS),
    ("预测模型的数据质量怎么样", PrimaryIntent.DATA_QUALITY),
    ("昨天销售额突然变高正常吗", PrimaryIntent.ANOMALY_ANALYSIS),
    ("毛利突然下降到底受什么因素影响", PrimaryIntent.ROOT_CAUSE_ANALYSIS),
]


@pytest.mark.parametrize("question,expected", CASES)
def test_adversarial_real_world_intent(question, expected):
    actual = RuleBasedIntentClassifier().classify(question, IDENTITY, "case").primary_intent
    assert actual is expected, f"{question!r}: expected {expected}, got {actual}"


def test_object_scoped_detail_negation_does_not_trigger_detail_query():
    result = RuleBasedIntentClassifier().classify(
        "查2026年7月销售额，不要订单明细", IDENTITY, "negated-detail-object"
    )

    assert result.primary_intent is PrimaryIntent.METRIC_QUERY
    assert result.entity is None
    assert "fields" not in result.missing_slots


def test_data_quality_accepts_a_dataset_object_without_metric():
    result = RuleBasedIntentClassifier().classify(
        "检查2025年1月订单数据的空值和重复记录", IDENTITY, "quality-object"
    )

    assert result.primary_intent is PrimaryIntent.DATA_QUALITY
    assert "metric" not in result.missing_slots
    assert result.time_range is not None
