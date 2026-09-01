from datetime import date

import pytest

from app.domain.models import AnalysisOperator, ConversationControl, PrimaryIntent, TrustedIdentity
from app.intent import RuleBasedIntentClassifier


IDENTITY = TrustedIdentity(tenant_id="tenant-a", user_id="user-a")


def test_forecast_is_not_historical_trend():
    classifier = RuleBasedIntentClassifier()
    forecast = classifier.classify("预测下个月销售额", IDENTITY, "c1")
    trend = classifier.classify("分析本月销售额趋势", IDENTITY, "c2")
    assert forecast.primary_intent == PrimaryIntent.FORECAST_ANALYSIS
    assert trend.primary_intent == PrimaryIntent.TREND_ANALYSIS


def test_detail_uses_high_risk_path():
    request = RuleBasedIntentClassifier().classify(
        "查询昨天退款订单明细，显示订单号和金额", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.risk_level == "HIGH"


def test_future_plan_value_is_query_not_forecast():
    request = RuleBasedIntentClassifier().classify(
        "查询下个月已经录入的计划值销售额", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.time_range is not None


def test_implicit_future_value_is_forecast():
    request = RuleBasedIntentClassifier().classify("下个月销售额是多少", IDENTITY, "c1")
    assert request.primary_intent == PrimaryIntent.FORECAST_ANALYSIS


def test_growth_reason_is_root_cause():
    request = RuleBasedIntentClassifier().classify(
        "分析本月销售额为什么增长", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS


def test_rising_reason_is_root_cause():
    request = RuleBasedIntentClassifier().classify(
        "为什么2025年11月到12月销售额明显上升？", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS


def test_metric_how_calculated_is_definition():
    request = RuleBasedIntentClassifier().classify(
        "含税销售总额这个指标是怎么计算的？", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.METRIC_DEFINITION


def test_chinese_forecast_horizon_is_extracted():
    request = RuleBasedIntentClassifier().classify(
        "预测未来三个月销售额。", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.FORECAST_ANALYSIS
    assert request.forecast_horizon_periods == 3
    assert request.forecast_granularity == "month"


def test_order_dataset_lineage_has_semantic_anchor():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "销售订单数据来自哪些表和字段？", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.DATA_LINEAGE
    assert [metric.input for metric in request.metrics] == ["订单笔数"]
    assert "metric" not in classifier.required_missing_slots(request)


def test_partner_hospital_count_alias_is_deterministic():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "统计最近一年合作医院数量。", IDENTITY, "c1"
    )
    assert [metric.input for metric in request.metrics] == ["已合作医院数"]
    assert "metric" not in classifier.required_missing_slots(request)


def test_hospital_master_list_does_not_get_default_sales_period():
    request = RuleBasedIntentClassifier().classify(
        "查询上海地区三级医院名单。", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.time_range is None
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions
    assert {
        "field": "医院等级", "operator": "EQ", "value": "三级医院",
    } in request.filters


def test_sales_detail_has_default_business_fields():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "查询2026年第二季度的销售明细。", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "订单"
    assert request.fields == ["订单号", "金额"]
    assert "fields" not in classifier.required_missing_slots(request)


def test_report_is_primary_and_root_cause_is_secondary():
    request = RuleBasedIntentClassifier().classify(
        "生成本月销售额下降原因报告", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.REPORT_GENERATION
    assert PrimaryIntent.ROOT_CAUSE_ANALYSIS in request.secondary_intents


@pytest.mark.parametrize(
    "question",
    (
        "查询本月销售额并导出Excel",
        "把本周订单下载为xlsx",
        "生成本季度销售PDF",
        "查询昨天明细并导出Word",
    ),
)
def test_explicit_file_delivery_is_report_generation(question):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "c-export")
    assert request.primary_intent == PrimaryIntent.REPORT_GENERATION


def test_cancel_is_conversation_control_not_a_new_query():
    request = RuleBasedIntentClassifier().classify("算了，不用了", IDENTITY, "c1")
    assert request.conversation_control == ConversationControl.CANCEL


def test_explicit_date_range_and_recent_days_are_parsed():
    classifier = RuleBasedIntentClassifier()
    explicit = classifier.classify("查询2026-07-01至2026-07-31销售额", IDENTITY, "c1")
    recent = classifier.classify("查看最近30天销售额", IDENTITY, "c2")
    assert explicit.time_range.start.isoformat() == "2026-07-01"
    assert explicit.time_range.end_exclusive.isoformat() == "2026-08-01"
    assert (recent.time_range.end_exclusive - recent.time_range.start).days == 30


def test_ranking_is_operator_not_new_intent():
    request = RuleBasedIntentClassifier().classify(
        "查询本月销售额前10名门店", IDENTITY, "c1"
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert AnalysisOperator.TOP_N in request.operators


def test_supplier_contact_list_is_detail_without_metric_or_time_clarification():
    request = RuleBasedIntentClassifier().classify(
        "提供上海地区供应漫步者品牌耳机的供应商及联系方式",
        IDENTITY,
        "c-contact",
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "供应商"
    assert request.fields == ["供应商名称", "联系人姓名", "联系人手机号"]
    assert request.missing_slots == []
    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "漫步者"},
        {"field": "商品名称", "operator": "EQ", "value": "耳机"},
    ]


def test_partner_list_shape_removes_non_physical_list_placeholder_field():
    question = "查询最近一年销售过费森尤斯产品的经销商名单"
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "partner-list-placeholder"
    )
    request.fields = ["经销商名单", "经销商名称"]

    RuleBasedIntentClassifier.apply_business_query_shapes(request, question)

    assert request.fields == ["经销商名称"]


def test_branded_consumable_partner_scope_is_not_one_product_name():
    request = RuleBasedIntentClassifier().classify(
        "查询上海市江苏苏云品牌低值耗材的经销商清单，"
        "并附带经销商对应销售总额",
        IDENTITY,
        "c-branded-category",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.entity == "经销商"
    assert request.fields == []
    assert [metric.input for metric in request.metrics] == ["销售总额"]
    assert request.dimensions == ["经销商", "城市", "商品品牌", "商品品类"]
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.AGGREGATE in request.operators
    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]
    assert all(item["field"] != "商品名称" for item in request.filters)


def test_branded_category_scope_is_generic_not_vendor_specific():
    request = RuleBasedIntentClassifier().classify(
        "查询浙江省华美医疗品牌高值医用耗材的供应商清单",
        IDENTITY,
        "c-generic-branded-category",
    )

    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "浙江省"},
        {"field": "商品品牌", "operator": "EQ", "value": "华美医疗"},
        {"field": "商品品类", "operator": "EQ", "value": "高值医用耗材"},
    ]


def test_concrete_product_after_brand_remains_exact_product_scope():
    request = RuleBasedIntentClassifier().classify(
        "提供上海地区做BD品牌超声血管导引穿刺套件产品的经销商及联系方式",
        IDENTITY,
        "c-branded-product",
    )

    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "BD"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "超声血管导引穿刺套件",
        },
    ]


def test_supplier_contact_shape_replaces_generic_model_fields():
    question = "提供供应SoundPro品牌无线蓝牙耳机的供应商及联系方式"
    request = RuleBasedIntentClassifier().classify(
        question,
        IDENTITY,
        "c-contact-generic",
    )
    request.fields = ["供应商", "联系方式"]

    RuleBasedIntentClassifier.apply_business_query_shapes(request, question)

    assert request.fields == ["供应商名称", "联系人姓名", "联系人手机号"]


def test_product_category_lookup_is_master_data_detail_query():
    request = RuleBasedIntentClassifier().classify(
        "无线蓝牙耳机属于哪些商品分类", IDENTITY, "c-category"
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "商品"
    assert "商品分类" in request.fields
    assert "metric" not in request.missing_slots
    assert "time_range" not in request.missing_slots


def test_decomposed_product_category_lookup_keeps_detail_shape():
    request = RuleBasedIntentClassifier().classify(
        "匹配无线蓝牙耳机所属商品分类", IDENTITY, "c-category-task"
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "商品"
    assert request.fields == ["商品名称", "商品分类"]
    assert "metric" not in request.missing_slots


def test_explicit_sales_total_is_not_lost_in_supplier_list():
    request = RuleBasedIntentClassifier().classify(
        "查询近一年供应商清单，并附带供应商对应销售总额",
        IDENTITY,
        "c-sales-list",
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.entity == "供应商"
    assert request.dimensions == ["供应商"]
    assert [metric.input for metric in request.metrics] == ["销售总额"]
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.AGGREGATE in request.operators
    assert "metric" not in request.missing_slots


def test_product_sales_situation_is_a_concrete_metric_request():
    request = RuleBasedIntentClassifier().classify(
        "分析近一年无线蓝牙耳机商品的销售情况", IDENTITY, "c-sales-situation"
    )
    assert [metric.input for metric in request.metrics] == ["含税销售总额"]
    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert "metric" not in request.missing_slots


def test_recommendation_routes_to_ranked_object_comparison():
    request = RuleBasedIntentClassifier().classify(
        "帮我推荐上海合适的供应商", IDENTITY, "c-recommend"
    )
    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert AnalysisOperator.TOP_N in request.operators
    assert request.missing_slots == ["metric"]


def test_dealer_recommendation_uses_verified_profile_default():
    request = RuleBasedIntentClassifier().classify(
        "我的产品适用于泌尿外科、肾脏内科，帮我推荐上海合适的经销商",
        IDENTITY,
        "c-dealer-recommend",
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.dimensions == ["经销商"]
    assert request.ranking_limit == 10
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.SORT in request.operators
    assert AnalysisOperator.TOP_N in request.operators
    assert request.missing_slots == []
    assert request.assumptions == [
        "DEALER_RECOMMENDATION_DEFAULT_RANKING=经销商近一年销售额",
        "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR",
    ]


def test_multi_object_comparison_infers_object_comparison_type():
    request = RuleBasedIntentClassifier().classify(
        "对比3家供应商近3个月业绩增长率与合作时长",
        IDENTITY,
        "c-object-compare",
    )
    assert request.comparison_type == "对象间比较"
    assert "comparison_type" not in request.missing_slots


@pytest.mark.parametrize(
    "question",
    (
        "比较上海和江苏2025年与2026年的销售额、订单量和客户数。",
        "比较费森尤斯与贝朗产品最近一年销售额、数量和覆盖医院数。",
    ),
)
def test_named_object_comparison_does_not_request_comparison_type(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-named-object-compare"
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert "comparison_type" not in request.missing_slots


def test_hospital_metrics_with_profile_dimensions_is_not_detail_lookup():
    request = RuleBasedIntentClassifier().classify(
        "统计每家医院的订单总金额、订单数，并显示医院等级和所在城市。",
        IDENTITY,
        "c-hospital-metrics",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.metrics
    assert "医院" in request.dimensions
    assert "fields" not in request.missing_slots


def test_region_object_comparison_preserves_both_region_filters():
    request = RuleBasedIntentClassifier().classify(
        "比较上海和江苏2025年与2026年的销售额、订单量和客户数。",
        IDENTITY,
        "c-region-object-compare",
    )

    assert {"field": "地区", "operator": "IN", "value": ["上海市", "江苏省"]} in request.filters
    assert "地区" in request.dimensions


@pytest.mark.parametrize(
    ("question", "metric"),
    (
        ("查询销量最高的三个产品", "销售量"),
        ("查询下单次数最多的三个客户", "订单量"),
        ("查询合作医院最多的三家经销商", "已合作医院数"),
    ),
)
def test_business_metric_aliases_are_canonicalized(question, metric):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-metric-alias"
    )

    assert [item.input for item in request.metrics] == [metric]
    assert "metric" not in request.missing_slots
    assert request.dimensions
    assert "dimension" not in request.missing_slots
    assert request.ranking_limit == 3


def test_sales_total_quantity_and_taxed_order_amount_aliases_are_preserved():
    classifier = RuleBasedIntentClassifier()
    quantity = classifier.classify(
        "查询外周插管中心静脉导管的含税销售总额和销售总数量。",
        IDENTITY,
        "c-total-quantity",
    )
    dealer = classifier.classify(
        "统计每家经销商承接的订单总额（含税）及订单笔数。",
        IDENTITY,
        "c-taxed-order-amount",
    )

    assert {item.input for item in quantity.metrics} == {"含税销售总额", "销售量"}
    assert [item.input for item in dealer.metrics] == ["含税销售总额", "订单笔数"]
    assert quantity.missing_slots == []
    assert dealer.missing_slots == []


def test_multidimensional_trend_separates_product_from_dimensions():
    request = RuleBasedIntentClassifier().classify(
        "分析空心纤维血液透析器在各省市、各医院等级、各月份的销量趋势。",
        IDENTITY,
        "c-multidimensional-product",
    )

    assert {"field": "商品名称", "operator": "EQ", "value": "空心纤维血液透析器"} in request.filters
    assert {"省份", "城市", "医院等级"}.issubset(request.dimensions)
    assert [item.input for item in request.metrics] == ["销售量"]
    assert "DEFAULT_TIME_GRANULARITY=month" in request.assumptions


def test_brand_comparison_uses_business_names_not_internal_codes():
    request = RuleBasedIntentClassifier().classify(
        "比较费森尤斯与贝朗产品最近一年销售额、数量和覆盖医院数。",
        IDENTITY,
        "c-brand-comparison",
    )

    assert {"field": "母品牌", "operator": "IN", "value": ["费森尤斯", "贝朗"]} in request.filters
    assert "品牌" in request.dimensions
    assert all("编码" not in str(item) for item in request.filters)
    assert request.missing_slots == []


def test_largest_sales_decline_has_a_deterministic_comparison_shape():
    request = RuleBasedIntentClassifier().classify(
        "找出销售额下降最大的五个产品",
        IDENTITY,
        "c-largest-decline",
    )

    assert request.comparison_type == "指定时段对比"
    assert request.ranking_limit == 5
    assert "产品" in request.dimensions
    assert "comparison_type" not in request.missing_slots


def test_brand_value_catalog_is_master_data_detail_without_metric():
    request = RuleBasedIntentClassifier().classify(
        "查询母品牌有哪些，展示前100个。",
        IDENTITY,
        "c-brand-values",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "母品牌"
    assert request.fields == ["母品牌"]
    assert request.time_range is None
    assert request.missing_slots == []


def test_product_applicable_department_wording_is_detail_lookup():
    request = RuleBasedIntentClassifier().classify(
        "查询超声血管导引穿刺套件适用的科室。",
        IDENTITY,
        "c-applicable-department",
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.fields == ["商品名称", "适用科室"]
    assert request.missing_slots == []


@pytest.mark.parametrize(
    ("question", "product"),
    (
        ("查询上海市紫杉醇释放冠脉球囊导管合作医院名单。", "紫杉醇释放冠脉球囊导管"),
        ("查询外周插管中心静脉导管合作的医院名单。", "外周插管中心静脉导管"),
        ("查询上海市紫杉醇释放冠脉球囊导管合作经销商名单。", "紫杉醇释放冠脉球囊导管"),
    ),
)
def test_relationship_lists_keep_exact_product_filter(question, product):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-relationship-product"
    )
    assert {"field": "商品名称", "operator": "EQ", "value": product} in request.filters
    if "上海" in question:
        assert {"field": "业务城市", "operator": "EQ", "value": "上海市"} in request.filters


def test_all_dealers_followup_is_a_deduplicated_catalog_list():
    request = RuleBasedIntentClassifier().classify(
        "展示上海市江苏苏云品牌低值耗材的所有经销商。",
        IDENTITY,
        "c-all-brand-category-dealers",
    )
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.metrics == []
    assert {"field": "城市", "operator": "EQ", "value": "上海市"} in request.filters
    assert {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"} in request.filters
    assert {"field": "商品品类", "operator": "EQ", "value": "低值耗材"} in request.filters
    assert not any("所有" in str(item.get("value")) for item in request.filters)


@pytest.mark.parametrize(
    ("question", "metric", "product"),
    (
        ("统计上海市紫杉醇释放冠脉球囊导管已合作医院数。", "已合作医院数", "紫杉醇释放冠脉球囊导管"),
        ("统计上海市紫杉醇释放冠脉球囊导管已合作经销商数量。", "已合作经销商数", "紫杉醇释放冠脉球囊导管"),
    ),
)
def test_relationship_counts_share_the_exact_product_scope_with_lists(question, metric, product):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-relationship-count-product"
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [item.input for item in request.metrics] == [metric]
    assert request.dimensions == []
    assert {"field": "商品名称", "operator": "EQ", "value": product} in request.filters
    assert {"field": "业务城市", "operator": "EQ", "value": "上海市"} in request.filters


def test_dealer_quantity_is_normalized_to_relationship_count_metric():
    request = RuleBasedIntentClassifier().classify(
        "统计上海地区销售BD品牌超声血管导引穿刺套件的经销商数量。",
        IDENTITY,
        "c-bd-dealer-count",
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [item.input for item in request.metrics] == ["已合作经销商数"]
    assert "经销商" not in request.dimensions
    assert {"field": "城市", "operator": "EQ", "value": "上海市"} in request.filters
    assert {"field": "商品品牌", "operator": "EQ", "value": "BD"} in request.filters
    assert {
        "field": "商品名称", "operator": "EQ", "value": "超声血管导引穿刺套件"
    } in request.filters
    assert request.missing_slots == []


def test_region_brand_product_trend_uses_parent_brand_not_product_name():
    request = RuleBasedIntentClassifier().classify(
        "分析上海地区费森尤斯产品最近一年的销售趋势。",
        IDENTITY,
        "c-brand-trend",
    )
    assert not any(item.get("field") == "商品名称" for item in request.filters)


@pytest.mark.parametrize(
    "question",
    (
        "超声血管导引穿刺套件适用于哪些科室",
        "匹配血液净化管路适用科室",
    ),
)
def test_product_department_lookup_is_complete_master_data_detail(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-product-department"
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "产品"
    assert request.fields == ["商品名称", "适用科室"]
    assert request.metrics == []
    assert request.missing_slots == []
    assert AnalysisOperator.AGGREGATE not in request.operators


def test_department_dependent_dealer_lookup_keeps_relation_fields():
    request = RuleBasedIntentClassifier().classify(
        "根据适用科室筛选出经销商", IDENTITY, "c-department-dealer"
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称", "适用科室"]
    assert request.missing_slots == []


@pytest.mark.parametrize(
    "question",
    (
        "针对一次性使用医用外科口罩产品及对应科室，上海仅保留三级医院渠道，输出 TOP3 经销商",
        "限定地区上海、适用科室心血管内科，自动筛选经销商并展示 TOP3 画像",
    ),
)
def test_ranked_dealer_recommendation_has_fixed_dimension_and_limit(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-dealer-recommendation"
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert request.dimensions == ["经销商"]
    assert request.ranking_limit == 3
    assert [metric.input for metric in request.metrics] == ["经销商近一年销售额"]
    assert request.missing_slots == []
    assert AnalysisOperator.TOP_N in request.operators
    assert "dimension" not in request.missing_slots
    assert "time_range" not in request.missing_slots


@pytest.mark.parametrize(
    ("question", "expected_limit"),
    (
        ("查询2025年10月至12月下单次数最多的三个经销商", 3),
        ("查询含税销售额最少的五家经销商", 5),
        ("查询订单量最多的二十个商品", 20),
        ("查询销量前二十三名商品", 23),
    ),
)
def test_ranking_limit_supports_natural_chinese_quantity(question, expected_limit):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-natural-ranking-limit"
    )

    assert request.ranking_limit == expected_limit
    assert AnalysisOperator.TOP_N in request.operators


def test_province_shorthand_is_normalized_and_replaces_prior_region_in_rewrite():
    request = RuleBasedIntentClassifier().classify(
        "不是上海，是江苏，查询最近一年订单量",
        IDENTITY,
        "c-region-replacement",
    )

    assert request.filters == [
        {"field": "地区", "operator": "EQ", "value": "江苏省"}
    ]


def test_monthly_named_product_sales_total_keeps_exact_product_filter():
    request = RuleBasedIntentClassifier().classify(
        "按月统计空心纤维血液透析器产品的含税销售总额",
        IDENTITY,
        "c-product-sales-trend",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == ["含税销售总额"]
    assert "DEFAULT_TIME_GRANULARITY=month" in request.assumptions
    assert AnalysisOperator.TIME_BUCKET in request.operators
    assert request.entity == "产品"
    assert request.filters == [{
        "field": "商品名称",
        "operator": "EQ",
        "value": "空心纤维血液透析器",
    }]


def test_time_grouped_sales_followups_stay_metric_queries_and_replace_grain():
    classifier = RuleBasedIntentClassifier()
    base = classifier.classify(
        "查询最近一年上海地区的含税销售总额",
        IDENTITY,
        "time-grouped-sales",
    )
    monthly = classifier.merge_clarification(base, "按月统计")

    assert monthly.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in monthly.metrics] == ["含税销售总额"]
    assert "DEFAULT_TIME_GRANULARITY=month" in monthly.assumptions
    assert AnalysisOperator.TIME_BUCKET in monthly.operators

    quarterly = classifier.merge_clarification(monthly, "改成按季度")
    assert quarterly.primary_intent == PrimaryIntent.METRIC_QUERY
    assert "DEFAULT_TIME_GRANULARITY=quarter" in quarterly.assumptions
    assert "DEFAULT_TIME_GRANULARITY=month" not in quarterly.assumptions
    assert "时间粒度：按季度" in quarterly.rewritten_question


def test_dealer_contact_lookup_requests_only_available_contact_concepts():
    request = RuleBasedIntentClassifier().classify(
        "提供上海地区做BD品牌产品的经销商及联系方式",
        IDENTITY,
        "c-dealer-contact",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称", "邮箱", "地址"]
    assert request.missing_slots == []


def test_precomputed_growth_window_is_not_a_calendar_range_and_asks_for_names():
    request = RuleBasedIntentClassifier().classify(
        "对比3家经销商3个月业绩增长率与合作时长",
        IDENTITY,
        "c-generic-dealer-comparison",
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert request.dimensions == ["经销商"]
    assert [metric.input for metric in request.metrics] == [
        "近三月业绩增长率", "合作时长",
    ]
    assert request.time_range is None
    assert request.assumptions == [
        "METRIC_WINDOW_IS_DEFINITION=近三月业绩增长率"
    ]
    assert request.missing_slots == ["comparison_objects"]


def test_explicit_reporting_period_is_kept_outside_precomputed_metric_window():
    request = RuleBasedIntentClassifier().classify(
        "查询2025年10月至12月的近三月业绩增长率",
        IDENTITY,
        "c-windowed-metric-in-reporting-period",
    )

    assert [metric.input for metric in request.metrics] == ["近三月业绩增长率"]
    assert request.time_range is not None
    assert request.time_range.start.isoformat() == "2025-10-01"
    assert request.time_range.end_exclusive.isoformat() == "2026-01-01"
    assert not any(
        item.startswith("METRIC_WINDOW_IS_DEFINITION=")
        for item in request.assumptions
    )


def test_ordinary_recent_sales_remains_a_calendar_range():
    request = RuleBasedIntentClassifier().classify(
        "查询最近三个月内的销售额",
        IDENTITY,
        "c-ordinary-reporting-period",
    )

    assert [metric.input for metric in request.metrics] == ["销售额"]
    assert request.time_range is not None
    assert not any(
        item.startswith("METRIC_WINDOW_IS_DEFINITION=")
        for item in request.assumptions
    )


def test_registered_30_day_snapshot_metric_does_not_create_time_range():
    class ClassifierWithActivitySnapshot(RuleBasedIntentClassifier):
        _known_metrics = (*RuleBasedIntentClassifier._known_metrics, "近30日活跃度")

    request = ClassifierWithActivitySnapshot().classify(
        "查询近30日活跃度",
        IDENTITY,
        "c-activity-snapshot",
    )

    assert [metric.input for metric in request.metrics] == ["近30日活跃度"]
    assert request.time_range is None
    assert request.missing_slots == []
    assert request.assumptions == ["METRIC_WINDOW_IS_DEFINITION=近30日活跃度"]


def test_multi_dealer_comparison_names_close_clarification_into_filter():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "对比3家经销商3个月业绩增长率与合作时长",
        IDENTITY,
        "c-generic-dealer-comparison",
    )

    merged = classifier.merge_clarification(
        request, "甲经销商、乙经销商、丙经销商"
    )

    assert merged.missing_slots == []
    assert merged.filters == [{
        "field": "经销商名称",
        "operator": "IN",
        "value": ["甲经销商", "乙经销商", "丙经销商"],
    }]
    assert [metric.input for metric in merged.metrics] == [
        "近三月业绩增长率", "合作时长",
    ]
    assert merged.time_range is None
    assert "METRIC_WINDOW_IS_DEFINITION=近三月业绩增长率" in merged.assumptions
    assert "甲经销商" in merged.rewritten_question


def test_multi_dealer_comparison_rejects_wrong_object_count():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "对比3家经销商3个月业绩增长率与合作时长",
        IDENTITY,
        "c-generic-dealer-comparison",
    )

    merged = classifier.merge_clarification(request, "甲经销商、乙经销商")

    assert merged.missing_slots == ["comparison_objects"]
    assert merged.filters == []


def test_named_dealer_comparison_without_period_defaults_to_latest_year():
    request = RuleBasedIntentClassifier().classify(
        "对比甲经销商、乙经销商和丙经销商业绩增长率与合作时长",
        IDENTITY,
        "c-named-dealer-comparison",
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert request.dimensions == ["经销商"]
    assert request.missing_slots == []
    assert request.time_range is not None


def test_cumulative_dealer_ranking_uses_all_history_and_keeps_all_metrics():
    request = RuleBasedIntentClassifier().classify(
        "查询累计销售额排名前5的经销商，并显示其合作时长和合作次数。",
        IDENTITY,
        "c-cumulative-dealer-ranking",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == [
        "累计销售额", "合作时长", "合作次数",
    ]
    assert request.dimensions == ["经销商"]
    assert AnalysisOperator.TOP_N in request.operators
    assert request.ranking_limit == 5
    assert request.time_range is None
    assert request.missing_slots == []
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions


@pytest.mark.parametrize(
    "question",
    (
        "按供应商统计历史累计销售额和订单总金额",
        "按供应商统计销售额和订单金额合计",
    ),
)
def test_explicit_total_metrics_do_not_require_an_arbitrary_period(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-all-time-total-metrics"
    )

    assert request.time_range is None
    assert "time_range" not in request.missing_slots
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions


def test_plain_sales_amount_ranking_defaults_to_latest_year():
    request = RuleBasedIntentClassifier().classify(
        "查询销售额排名前5的经销商",
        IDENTITY,
        "c-period-required-dealer-ranking",
    )

    assert request.dimensions == ["经销商"]
    assert request.ranking_limit == 5
    assert request.missing_slots == []
    assert request.time_range is not None
    assert "TIME_SCOPE=ALL_TIME" not in request.assumptions


def test_cumulative_ranking_detail_wording_also_uses_all_history():
    request = RuleBasedIntentClassifier().classify(
        "列出累计销售额排名前5的经销商名单，并显示合作次数",
        IDENTITY,
        "c-all-time-ranking-list",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.dimensions == ["经销商"]
    assert [metric.input for metric in request.metrics] == ["累计销售额", "合作次数"]
    assert request.missing_slots == []
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions


@pytest.mark.parametrize("question", ("瑞金医院地址在哪里？", "瑞金医院地址在那里？"))
def test_hospital_address_is_master_data_detail_without_metric_or_time(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-hospital-address"
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "医院"
    assert request.fields == ["医院名称", "地址"]
    assert request.missing_slots == []


def test_report_without_time_defaults_to_latest_year_and_preserves_scope():
    classifier = RuleBasedIntentClassifier()
    request = classifier.classify(
        "我在上海卖外周插管中心静脉导管，给我生成分析报告",
        IDENTITY,
        "report-scope",
    )

    assert request.primary_intent == PrimaryIntent.REPORT_GENERATION
    assert request.entity == "产品"
    assert request.filters == [
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "外周插管中心静脉导管",
        },
    ]
    assert request.missing_slots == []
    assert request.time_range is not None
    assert (request.time_range.end_exclusive - request.time_range.start).days in {365, 366}
    assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions


def test_department_led_dealer_recommendation_uses_department_names():
    request = RuleBasedIntentClassifier().classify(
        "我的产品适用于泌尿外科、肾脏内科，帮我推荐上海合适的经销商",
        IDENTITY,
        "department-dealer",
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.entity == "经销商"
    assert request.dimensions == ["经销商"]
    assert request.filters == [
        {
            "field": "科室名称",
            "operator": "IN",
            "value": ["泌尿外科", "肾脏内科"],
        },
        {"field": "地区", "operator": "EQ", "value": "上海市"},
    ]
    assert request.missing_slots == []
    assert [metric.input for metric in request.metrics] == ["经销商近一年销售额"]


def test_product_sales_overview_uses_auditable_defaults():
    request = RuleBasedIntentClassifier().classify(
        "分析下空心纤维血液透析器产品的销售情况",
        IDENTITY,
        "sales-overview",
    )

    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert [metric.input for metric in request.metrics] == ["含税销售总额"]
    assert request.missing_slots == []
    assert request.time_range is not None
    assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
    assert "DEFAULT_TIME_GRANULARITY=month" in request.assumptions


def test_report_coverage_facets_infer_concrete_row_entities_and_fields():
    classifier = RuleBasedIntentClassifier()
    hospital = classifier.classify(
        "列出上海市紫杉醇释放冠脉球囊导管医院覆盖明细",
        IDENTITY,
        "hospital-coverage",
    )
    dealer = classifier.classify(
        "列出上海市紫杉醇释放冠脉球囊导管合作经销商数据明细",
        IDENTITY,
        "dealer-coverage",
    )

    assert hospital.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert hospital.entity == "医院"
    assert hospital.fields == ["医院名称"]
    assert hospital.missing_slots == []
    assert hospital.time_range is not None
    assert dealer.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert dealer.entity == "经销商"
    assert dealer.fields == ["经销商名称"]
    assert dealer.missing_slots == []
    assert dealer.time_range is not None
    assert hospital.filters == [
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "紫杉醇释放冠脉球囊导管",
        },
    ]
    assert dealer.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "紫杉醇释放冠脉球囊导管",
        },
    ]


def test_named_dealer_metric_followup_extracts_and_merges_dealer_filter():
    classifier = RuleBasedIntentClassifier()
    previous = classifier.classify(
        "查询上海市江苏苏云品牌低值耗材的经销商清单，并显示各经销商含税销售总额。",
        IDENTITY,
        "named-dealer-followup",
    )

    merged = classifier.merge_clarification(
        previous,
        "那上海米财灵商贸有限公司的最近一个月的含税销售总额是多少",
    )

    dealer_filters = [
        item for item in merged.filters
        if item.get("field") == "经销商名称"
    ]
    assert dealer_filters == [{
        "field": "经销商名称",
        "operator": "EQ",
        "value": "上海米财灵商贸有限公司",
    }]
    assert merged.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in merged.metrics] == ["含税销售总额"]

    changed_month = classifier.merge_clarification(
        merged,
        "那2025年12月份的含税销售总额是多少",
    )
    assert changed_month.time_range is not None
    assert changed_month.time_range.start == date(2025, 12, 1)
    assert changed_month.time_range.end_exclusive == date(2026, 1, 1)
    assert [
        item for item in changed_month.filters
        if item.get("field") == "经销商名称"
    ] == [{
        "field": "经销商名称",
        "operator": "EQ",
        "value": "上海米财灵商贸有限公司",
    }]

    parsed_time_followup = classifier.classify(
        "那2025年12月份的含税销售总额是多少",
        IDENTITY,
        "named-dealer-followup",
    )
    assert parsed_time_followup.conversation_control == ConversationControl.FOLLOW_UP


def test_sales_trend_facet_uses_auditable_sales_amount_metric():
    request = RuleBasedIntentClassifier().classify(
        "分析上海市紫杉醇释放冠脉球囊导管整体销售趋势",
        IDENTITY,
        "report-trend",
    )

    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert [metric.input for metric in request.metrics] == ["销售额"]
    assert request.missing_slots == []
    assert request.time_range is not None
    assert "SALES_TREND_METRIC=销售额" in request.assumptions


def test_partner_list_ranked_by_snapshot_metric_is_complete_comparison():
    question = (
        "请列出上海市医用外科口罩产品的经销商名单，排除上海洁安厂家，"
        "并按他们现有的整体业务规模排序"
    )
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "ranked-partner-list"
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert request.comparison_type == "对象间比较"
    assert [metric.input for metric in request.metrics] == ["整体业务规模"]
    assert "经销商" in request.dimensions
    assert AnalysisOperator.COMPARE in request.operators
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.SORT in request.operators
    assert request.missing_slots == []
    assert request.filters == [
        {"field": "地区", "operator": "EQ", "value": "上海市"},
        {"field": "厂家名称", "operator": "NE", "value": "上海洁安"},
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]


def test_partner_activity_filter_defaults_to_latest_year_for_current_sales():
    request = RuleBasedIntentClassifier().classify(
        "帮我找出上海地区正在销售振德医疗品牌的医用外科口罩产品的"
        "经销商名单，并按他们现有的整体业务规模排序。",
        IDENTITY,
        "ranked-active-partner-list",
    )

    assert request.primary_intent == PrimaryIntent.COMPARISON_ANALYSIS
    assert [metric.input for metric in request.metrics] == ["整体业务规模"]
    assert AnalysisOperator.SORT in request.operators
    assert request.missing_slots == []
    assert request.time_range is not None
    assert (
        "ACTIVE_TIME_DEFAULT=LATEST_ONE_YEAR_FROM_REQUEST_DATE"
        in request.assumptions
    )
    assert (
        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
        in request.assumptions
    )
    assert request.dimensions == ["经销商", "城市", "商品品牌", "商品名称"]
    assert request.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "振德医疗"},
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]

def test_dated_sales_record_activity_does_not_request_partner_status_or_threshold():
    request = RuleBasedIntentClassifier().classify(
        "近一年内，哪些活跃经销商在销售费森尤斯产品。",
        IDENTITY,
        "sales-record-active-partners",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称"]
    assert request.time_range is not None
    assert request.missing_slots == []
    assert request.assumptions == [
        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
    ]


def test_named_dealer_product_lookup_is_relationship_detail_without_metric():
    request = RuleBasedIntentClassifier().classify(
        "上海东松医疗科技股份有限公司都销售什么产品",
        IDENTITY,
        "dealer-products",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "商品"
    assert request.fields == ["商品名称"]
    assert request.metrics == []
    assert request.filters == [{
        "field": "经销商名称",
        "operator": "EQ",
        "value": "上海东松医疗科技股份有限公司",
    }]
    assert request.time_range is not None
    assert request.missing_slots == []


def test_named_dealer_sold_product_lookup_is_relationship_detail_without_metric():
    request = RuleBasedIntentClassifier().classify(
        "查询最近一年上海东松医疗科技股份有限公司销售过的产品。",
        IDENTITY,
        "dealer-sold-products",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "商品"
    assert request.fields == ["商品名称"]
    assert request.metrics == []
    assert request.filters == [{
        "field": "经销商名称",
        "operator": "EQ",
        "value": "上海东松医疗科技股份有限公司",
    }]
    assert request.missing_slots == []


@pytest.mark.parametrize(
    ("question", "operator"),
    [
        ("前三名", AnalysisOperator.TOP_N),
        ("只显示前五名", AnalysisOperator.TOP_N),
        ("改成后三名", AnalysisOperator.BOTTOM_N),
    ],
)
def test_chinese_numeral_ranking_followup(question, operator):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "ranking-followup")

    assert request.ranking_limit in {3, 5}
    assert operator in request.operators
    assert request.conversation_control in {
        ConversationControl.FOLLOW_UP,
        ConversationControl.CORRECTION,
    }


def test_pronoun_hospital_relationship_followup_stays_detail_query():
    request = RuleBasedIntentClassifier().classify(
        "它卖给了哪些医院？\n已确认的上一轮上下文（当前问题明确内容优先）：实体=商品；维度=产品",
        IDENTITY,
        "relationship-followup",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.metrics == []


def test_product_to_hospital_followup_has_concrete_detail_shape():
    request = RuleBasedIntentClassifier().classify(
        "它卖给了哪些医院？\n已确认的上一轮上下文（当前问题明确内容优先）：实体=商品",
        IDENTITY,
        "product-hospitals",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "医院"
    assert request.fields == ["医院名称"]
    assert request.metrics == []
    assert request.missing_slots == []


@pytest.mark.parametrize(
    "question",
    ["那再查询一下最近一个月的", "那查询一下2025年12月1号到31号的"],
)
def test_natural_requery_wording_is_followup(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "natural-requery-followup"
    )
    assert request.conversation_control == ConversationControl.FOLLOW_UP


def test_singular_extrema_over_dimension_is_ranked_metric_query():
    request = RuleBasedIntentClassifier().classify(
        "其中销售额最高的是哪个？\n实体=商品；维度=商品",
        IDENTITY,
        "singular-extrema",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.ranking_limit == 1
    assert request.dimensions == ["商品"]
    assert [metric.input for metric in request.metrics] == ["销售额"]
    assert AnalysisOperator.TOP_N in request.operators


def test_cancel_phrase_with_replacement_task_is_a_new_request():
    request = RuleBasedIntentClassifier().classify(
        "算了，不想问这个了，查询下医院等级有哪些。",
        IDENTITY,
        "replacement-task",
    )

    assert request.conversation_control == ConversationControl.NEW_REQUEST
    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "医院"
    assert request.fields == ["医院名称", "医院等级"]


def test_plain_active_partner_status_still_requires_an_explicit_activity_period():
    request = RuleBasedIntentClassifier().classify(
        "查询活跃经销商名单",
        IDENTITY,
        "profile-active-partners",
    )

    assert request.fields == ["经销商名称", "合作状态"]
    assert request.assumptions == ["DEFAULT_TIME_RANGE=LATEST_ONE_YEAR"]
    assert request.missing_slots == []


def test_plain_partner_list_without_ranking_remains_detail_query():
    request = RuleBasedIntentClassifier().classify(
        "列出上海市医用外科口罩产品的经销商名单",
        IDENTITY,
        "plain-partner-list",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称"]


def test_relationship_followup_switches_prior_metric_result_to_detail_query():
    classifier = RuleBasedIntentClassifier()
    previous = classifier.classify(
        "查询产品销售额最高的是哪个", IDENTITY, "relationship-switch"
    )
    previous.asl_template = {"version": "2.0", "metrics": [{"name": "sales"}]}

    merged = classifier.merge_clarification(previous, "它卖给了哪些医院？")

    assert merged.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert merged.metrics == []
    assert merged.entity == "医院"
    assert merged.fields == ["医院名称"]
    assert merged.asl_template is None
