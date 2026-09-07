from datetime import date

import pytest

from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ConversationControl,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.intent.classifier import render_execution_question
from app.services.intent_asl_contract import build_intent_asl_contract


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


@pytest.mark.parametrize(
    "question",
    [
        "请列出上海市某产品的含税销售总额。",
        "请列出某产品的含税销售总额和销售总数量。",
        "请列出含税销售总额排名前10的医院，并显示医院等级和订单笔数。",
    ],
)
def test_list_verb_with_explicit_metrics_is_not_misclassified_as_detail(question):
    request = RuleBasedIntentClassifier().classify(question, IDENTITY, "c-list-metric")

    assert request.primary_intent != PrimaryIntent.DETAIL_QUERY
    assert request.metrics


def test_ranked_hospital_profile_keeps_all_explicit_metrics():
    request = RuleBasedIntentClassifier().classify(
        "查询含税销售总额排名前10的医院，并显示医院名称、医院等级和订单笔数。",
        IDENTITY,
        "ranked-hospital-profile",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert {metric.input for metric in request.metrics} == {
        "含税销售总额", "订单笔数",
    }
    assert request.ranking_limit == 10
    assert "医院" in request.dimensions
    assert "医院等级" in request.dimensions
    assert "NULL_DIMENSION_BUCKET=医院等级:未填写" not in request.assumptions


def test_additive_metric_clarification_preserves_existing_metrics():
    classifier = RuleBasedIntentClassifier()
    pending = classifier.classify(
        "查询各经销商的含税销售总额并排序", IDENTITY, "c-additive-metric"
    )

    merged = classifier.merge_clarification(
        pending, "再加上订单笔数，其他条件不变。"
    )

    assert {metric.input for metric in merged.metrics} == {
        "含税销售总额", "订单笔数",
    }


def test_additive_metric_clarification_preserves_descending_sort_semantics():
    classifier = RuleBasedIntentClassifier()
    pending = classifier.classify(
        "查询医用外科口罩产品的经销商，按含税销售总额从高到低排序。",
        IDENTITY,
        "c-additive-sort",
    )

    merged = classifier.merge_clarification(
        pending, "再加上订单笔数，其他条件不变。"
    )
    execution_question = render_execution_question(merged)

    assert AnalysisOperator.SORT in merged.operators
    assert "SORT_DIRECTION=DESC" in merged.assumptions
    assert "排序要求：按含税销售总额从高到低" in execution_question


def test_monthly_statistics_is_a_grouped_metric_table_not_trend_analysis():
    request = RuleBasedIntentClassifier().classify(
        "按月统计某产品的含税销售总额。", IDENTITY, "c-monthly-stat"
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert "DEFAULT_TIME_GRANULARITY=month" in request.assumptions


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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
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


def test_transaction_partner_scope_separates_activity_words_from_catalog_value():
    request = RuleBasedIntentClassifier().classify(
        "查询最近一年销售过费森尤斯产品的经销商名单",
        IDENTITY,
        "transaction-brand-partners",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称"]
    assert request.filters == [
        {"field": "商品名称", "operator": "EQ", "value": "费森尤斯"},
    ]
    assert request.time_range is not None
    assert "SET_RELATIONSHIP_PROJECTION" in request.assumptions
    assert (
        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
        in request.assumptions
    )


@pytest.mark.parametrize(
    "question",
    (
        "空心纤维血液透析器产品的经销商有哪些",
        "空心纤维血液透析器产品有哪些经销商",
        "空心纤维血液透析器产品由哪些经销商销售",
        "哪些经销商销售空心纤维血液透析器产品",
        "空心纤维血液透析器都有哪些经销商在卖？",
        "我想知道空心纤维血液透析器这个产品都通过哪些经销商渠道销售的，给我一份名单。",
    ),
)
def test_interrogative_product_partner_word_orders_are_relationship_details(question):
    request = RuleBasedIntentClassifier().classify(
        question,
        IDENTITY,
        "interrogative-product-partners",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.metrics == []
    assert request.fields == ["经销商名称"]
    assert request.dimensions == ["经销商"]
    assert request.filters == [
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "空心纤维血液透析器",
        },
    ]
    assert "metric" not in request.missing_slots
    assert "SET_RELATIONSHIP_PROJECTION" in request.assumptions


@pytest.mark.parametrize(
    ("question", "target"),
    (
        ("医用外科口罩商品的供应商有哪些", "供应商"),
        ("医用外科口罩产品由哪些医院使用", "医院"),
        ("哪些医院采购医用外科口罩产品", "医院"),
        ("医用外科口罩商品有哪些客户", "客户"),
        ("医用外科口罩商品对应的门店都有谁", "门店"),
        ("查询医用外科口罩产品合作经销商", "经销商"),
        ("查询医用外科口罩产品合作医院", "医院"),
        ("查询医用外科口罩产品厂家", "厂家"),
        ("医用外科口罩产品经销商", "经销商"),
    ),
)
def test_interrogative_product_relationship_targets_share_one_detail_shape(
    question, target
):
    request = RuleBasedIntentClassifier().classify(
        question,
        IDENTITY,
        f"interrogative-product-{target}",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == target
    assert request.fields == [f"{target}名称"]
    assert request.filters == [
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]
    assert request.missing_slots == []


def test_bare_regional_product_partner_shape_preserves_region_and_exclusion_filters():
    regional = RuleBasedIntentClassifier().classify(
        "上海紫杉醇释放冠脉球囊导管产品经销商",
        IDENTITY,
        "bare-regional-product-partner",
    )
    excluded = RuleBasedIntentClassifier().classify(
        "排除振德医疗厂家的医用外科口罩产品经销商",
        IDENTITY,
        "bare-excluded-product-partner",
    )

    assert regional.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert regional.entity == "经销商"
    assert regional.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "紫杉醇释放冠脉球囊导管",
        },
    ]
    assert excluded.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert excluded.entity == "经销商"
    assert excluded.filters == [
        {"field": "厂家名称", "operator": "NE", "value": "振德医疗"},
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]


def test_interrogative_relationship_shape_does_not_replace_ranked_metric_query():
    request = RuleBasedIntentClassifier().classify(
        "空心纤维血液透析器产品的哪些经销商销售额最高",
        IDENTITY,
        "ranked-product-partners",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == ["销售额"]
    assert request.ranking_limit == 1


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
    assert request.dimensions == ["经销商"]
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.AGGREGATE in request.operators
    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]
    assert all(item["field"] != "商品名称" for item in request.filters)


def test_filter_only_roles_do_not_leak_into_metric_grouping_dimensions():
    request = RuleBasedIntentClassifier().classify(
        "查询上海市江苏苏云品牌低值耗材的经销商清单，并附带含税销售总额",
        IDENTITY,
        "c-filter-only-roles",
    )

    assert request.dimensions == ["经销商"]
    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "江苏苏云"},
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]


def test_same_city_role_can_be_both_filter_and_explicit_dimension():
    request = RuleBasedIntentClassifier().classify(
        "只看上海市，按城市统计含税销售总额",
        IDENTITY,
        "c-city-filter-and-dimension",
    )

    assert request.dimensions == ["城市"]
    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
    ]


def test_grouping_phrase_is_not_invented_as_a_product_filter():
    request = RuleBasedIntentClassifier().classify(
        "统计上海市各个经销商的含税销售总额",
        IDENTITY,
        "c-group-role-not-product-value",
    )

    assert request.dimensions == ["经销商"]
    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
    ]
    assert request.entity != "产品"


def test_branded_category_scope_is_generic_not_vendor_specific():
    request = RuleBasedIntentClassifier().classify(
        "查询浙江省华美医疗品牌高值医用耗材的供应商清单",
        IDENTITY,
        "c-generic-branded-category",
    )

    assert request.filters == [
        {"field": "业务省份", "operator": "EQ", "value": "浙江省"},
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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {"field": "商品品牌", "operator": "EQ", "value": "BD"},
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "超声血管导引穿刺套件",
        },
    ]


def test_brand_product_interrogative_tail_is_not_part_of_product_value():
    request = RuleBasedIntentClassifier().classify(
        "振德医疗品牌的医用外科口罩有哪些经销商？",
        IDENTITY,
        "c-brand-product-interrogative",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == "经销商"
    assert request.fields == ["经销商名称"]
    assert request.filters == [
        {"field": "商品品牌", "operator": "EQ", "value": "振德医疗"},
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]


def test_medical_product_metric_without_product_suffix_keeps_exact_entity_scope():
    request = RuleBasedIntentClassifier().classify(
        "查询上海市紫杉醇释放冠脉球囊导管的含税销售总额。",
        IDENTITY,
        "c-device-metric-without-product-suffix",
    )

    assert request.entity == "产品"
    assert request.filters == [
        {
            "field": "商品名称",
            "operator": "EQ",
            "value": "紫杉醇释放冠脉球囊导管",
        },
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
    ]
    assert "GEOGRAPHIC_ROLE=SALES_BUSINESS_CITY" in request.assumptions


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
        "GEOGRAPHIC_ROLE=SALES_BUSINESS_CITY",
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

    assert {item.input for item in quantity.metrics} == {"含税销售总额", "销售总数量"}
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


@pytest.mark.parametrize(
    ("question", "entity", "field"),
    [
        (
            "查询外周插管中心静脉导管合作的医院名单。",
            "医院",
            "医院名称",
        ),
        (
            "查询空心纤维血液透析器合作的经销商名单。",
            "经销商",
            "经销商名称",
        ),
        ("查询医用外科口罩的供应商清单。", "供应商", "供应商名称"),
    ],
)
def test_master_name_lists_require_non_null_members(
    question: str, entity: str, field: str
):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, f"non-null-{entity}"
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == entity
    assert field in request.fields
    assert f"REQUIRED_NAME_NON_NULL={field}" in request.assumptions


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


@pytest.mark.parametrize("dash", ("-", "‑"))
def test_specification_identifier_is_left_untyped_for_catalog_grounding(dash):
    request = RuleBasedIntentClassifier().classify(
        f"查询 TDC{dash}3 产品的适用科室",
        IDENTITY,
        "c-specification-department",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.fields == ["商品名称", "适用科室"]
    assert request.semantic_entity_mentions == ["TDC-3"]
    assert not any(
        item.get("field") == "商品名称" and item.get("value") == "TDC-3"
        for item in request.filters
    )


@pytest.mark.parametrize(
    ("scope", "expected_value"),
    (("主要适用科室", 1), ("次要适用科室", 2)),
)
def test_applicable_department_class_is_a_bridge_filter(scope, expected_value):
    request = RuleBasedIntentClassifier().classify(
        f"查询 TDC-3 产品的{scope}",
        IDENTITY,
        "c-department-class",
    )

    assert request.fields == ["商品名称", "适用科室"]
    assert {
        "field": "适用科室类型", "operator": "EQ", "value": expected_value,
    } in request.filters
    assert request.semantic_entity_mentions == ["TDC-3"]
    assert request.time_range is None


@pytest.mark.parametrize(
    ("scope", "expected_value"),
    (("主要科室", 1), ("主科室", 1), ("次要科室", 2), ("次科室", 2)),
)
def test_short_applicable_department_qualifier_is_detail_lookup(
    scope, expected_value
):
    request = RuleBasedIntentClassifier().classify(
        f"查询{scope}", IDENTITY, "c-short-department-class"
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.metrics == []
    assert request.entity == "产品"
    assert request.fields == ["商品名称", "适用科室"]
    assert request.filters == [{
        "field": "适用科室类型", "operator": "EQ", "value": expected_value,
    }]
    assert request.time_range is None
    assert request.missing_slots == []


def test_short_department_followup_replaces_relation_slot_and_keeps_specification():
    classifier = RuleBasedIntentClassifier()
    previous = classifier.classify(
        "查询 TDC-3 产品的主要适用科室", IDENTITY, "c-department-followup"
    )
    previous.asl_template = {"subject": "product"}
    previous.source_dataset_id = "old-main-departments"

    merged = classifier.merge_clarification(previous, "查询次要科室")

    assert merged.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert merged.metrics == []
    assert merged.semantic_entity_mentions == ["TDC-3"]
    assert merged.filters == [{
        "field": "适用科室类型", "operator": "EQ", "value": 2,
    }]
    assert merged.time_range is None
    assert merged.asl_template is None
    assert merged.source_dataset_id is None
    assert "APPLICABLE_DEPARTMENT_RELATION_TYPE=PRIMARY" not in merged.assumptions
    assert "APPLICABLE_DEPARTMENT_RELATION_TYPE=SECONDARY" in merged.assumptions
    assert merged.rewritten_question == "查询 TDC-3 产品的次要适用科室"


def test_parallel_applicable_department_classes_preserve_both_values() -> None:
    request = RuleBasedIntentClassifier().classify(
        "查询 TDC-3 产品的主要适用科室、次要适用科室",
        IDENTITY,
        "c-department-classes",
    )

    assert {
        "field": "适用科室类型", "operator": "IN", "value": [1, 2],
    } in request.filters
    assert request.semantic_entity_mentions == ["TDC-3"]
    assert request.time_range is None
    assert "APPLICABLE_DEPARTMENT_RELATION_TYPE=BOTH" in request.assumptions


@pytest.mark.parametrize("scope", ("适用科室", "所有适用科室"))
def test_all_applicable_departments_do_not_add_relation_type_filter(scope):
    request = RuleBasedIntentClassifier().classify(
        f"查询 TDC-3 产品的{scope}",
        IDENTITY,
        "c-all-departments",
    )

    assert not any(
        item.get("field") in {"适用类型", "适用科室类型", "关系类型"}
        for item in request.filters
    )
    assert request.time_range is None


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
    assert (
        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE"
        in request.assumptions
    )
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
    assert {"field": "业务城市", "operator": "EQ", "value": "上海市"} in request.filters
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


def test_period_free_grouped_relationship_count_uses_all_available_history():
    request = RuleBasedIntentClassifier().classify(
        "统计上海市各个经销商的已合作医院数",
        IDENTITY,
        "c-grouped-relationship-count-all-time",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [item.input for item in request.metrics] == ["已合作医院数"]
    assert request.dimensions == ["经销商"]
    assert {"field": "业务城市", "operator": "EQ", "value": "上海市"} in request.filters
    assert not any(
        item.get("field") == "商品名称" for item in request.filters
    )
    assert request.entity == "经销商"
    assert request.time_range is None
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions
    assert (
        "TIME_SCOPE_SOURCE=BUSINESS_DEFAULT_ALL_AVAILABLE_HISTORY"
        in request.assumptions
    )
    assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" not in request.assumptions


def test_dealer_quantity_is_normalized_to_relationship_count_metric():
    request = RuleBasedIntentClassifier().classify(
        "统计上海地区销售BD品牌超声血管导引穿刺套件的经销商数量。",
        IDENTITY,
        "c-bd-dealer-count",
    )
    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [item.input for item in request.metrics] == ["已合作经销商数"]
    assert "经销商" not in request.dimensions
    assert {"field": "业务城市", "operator": "EQ", "value": "上海市"} in request.filters
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
        {"field": "业务省份", "operator": "EQ", "value": "江苏省"}
    ]


@pytest.mark.parametrize(
    ("question", "field", "value"),
    [
        ("查询四川省的含税销售总额", "业务省份", "四川省"),
        ("查询国外的含税销售总额", "业务省份", "国外"),
        ("查询上海市的含税销售总额", "业务城市", "上海市"),
        ("查询位于四川省的经销商", "经销商省份", "四川省"),
    ],
)
def test_region_scope_distinguishes_transaction_geography_from_entity_location(
    question, field, value,
):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "c-region-role"
    )

    assert {"field": field, "operator": "EQ", "value": value} in request.filters


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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
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
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
        {"field": "厂家名称", "operator": "NE", "value": "上海洁安"},
        {"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"},
    ]


def test_ranked_partner_result_without_list_word_keeps_partner_grouping():
    request = RuleBasedIntentClassifier().classify(
        "查询上海市医用外科口罩产品的经销商，并按整体业务规模从高到低排序。",
        IDENTITY,
        "ranked-partner-without-list-word",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.entity == "经销商"
    assert [metric.input for metric in request.metrics] == ["整体业务规模"]
    assert "经销商" in request.dimensions
    assert AnalysisOperator.GROUP_BY in request.operators
    assert AnalysisOperator.SORT in request.operators
    assert request.missing_slots == []


def test_ranked_partner_result_with_intermediate_exclusion_keeps_grouping():
    request = RuleBasedIntentClassifier().classify(
        "查询上海市医用外科口罩产品的经销商，排除上海洁安厂家，"
        "并按整体业务规模排序。",
        IDENTITY,
        "ranked-partner-with-exclusion",
    )

    assert request.entity == "经销商"
    assert "经销商" in request.dimensions
    assert {"field": "厂家名称", "operator": "NE", "value": "上海洁安"} in request.filters


def test_hospital_level_rollup_does_not_group_by_individual_hospital():
    request = RuleBasedIntentClassifier().classify(
        "按医院等级汇总含税销售总额、销售总数量和订单笔数。",
        IDENTITY,
        "hospital-level-rollup",
    )

    assert request.dimensions == ["医院等级"]
    assert {metric.input for metric in request.metrics} == {
        "含税销售总额", "销售总数量", "订单笔数",
    }


def test_plain_department_order_ranking_uses_product_combination_grain():
    request = RuleBasedIntentClassifier().classify(
        "统计各科室的订单笔数排名。", IDENTITY, "department-combination-grain"
    )

    assert [metric.input for metric in request.metrics] == ["订单笔数"]
    assert request.entity == "商品"
    assert request.dimensions == ["主要适用科室"]
    assert (
        "DEPARTMENT_GRAIN=PRODUCT_MAIN_DEPARTMENT_COMBINATION"
        in request.assumptions
    )
    assert build_intent_asl_contract(request)["query_object"] == "商品"


def test_explicit_standard_department_split_keeps_bridge_grain():
    request = RuleBasedIntentClassifier().classify(
        "按单个标准科室拆分统计订单笔数排名。",
        IDENTITY,
        "standard-department-grain",
    )

    assert [metric.input for metric in request.metrics] == ["订单笔数"]
    assert "科室" in request.dimensions
    assert "主要适用科室" not in request.dimensions
    assert "DEPARTMENT_GRAIN=STANDARD_DEPARTMENT" in request.assumptions


def test_hospital_level_count_is_grouped_metric_not_hospital_detail():
    request = RuleBasedIntentClassifier().classify(
        "各医院等级对应的医院数量是多少？",
        IDENTITY,
        "hospital-level-count",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == ["医院数量"]
    assert request.entity is None
    assert request.fields == []
    assert request.dimensions == ["医院等级"]
    assert "NULL_DIMENSION_BUCKET=医院等级:未填写" in request.assumptions
    assert "STRICT_GROUPING_DIMENSIONS" in request.assumptions


def test_all_product_monthly_trend_is_not_misclassified_as_comparison():
    request = RuleBasedIntentClassifier().classify(
        "对比一下全部产品的月度含税销售总额趋势。",
        IDENTITY,
        "all-product-monthly-trend",
    )

    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert [metric.input for metric in request.metrics] == ["含税销售总额"]
    assert "产品" not in request.dimensions
    assert request.comparison_type is None
    assert request.missing_slots == []


def test_generic_province_order_count_uses_sales_business_geography():
    request = RuleBasedIntentClassifier().classify(
        "统计各省份的订单笔数。",
        IDENTITY,
        "business-province-orders",
    )

    assert request.dimensions == ["业务省份"]
    assert "GEOGRAPHIC_ROLE=SALES_ORDER_BUSINESS_PROVINCE" in request.assumptions
    assert [metric.input for metric in request.metrics] == ["订单笔数"]


def test_product_order_distribution_infers_count_without_clarification():
    request = RuleBasedIntentClassifier().classify(
        "统计胸腹腔内窥镜手术系统用手术器械在各省份的销售订单分布。",
        IDENTITY,
        "product-order-distribution",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == ["订单笔数"]
    assert request.dimensions == ["业务省份"]
    assert request.filters == [{
        "field": "商品名称",
        "operator": "EQ",
        "value": "胸腹腔内窥镜手术系统用手术器械",
    }]
    assert request.time_range is None
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions
    assert request.missing_slots == []


@pytest.mark.parametrize(
    "question",
    (
        "查询复旦大学附属华山医院的订单笔数。",
        "查询上海交通大学医学院附属新华医院的订单笔数。",
        "查询上海市第十人民医院的订单笔数。",
        "查询上海市宝山区高境镇社区卫生服务中心的订单笔数。",
    ),
)
def test_period_free_named_hospital_metric_uses_all_available_history(question):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "hospital-lifetime-order-count"
    )

    assert [metric.input for metric in request.metrics] == ["订单笔数"]
    assert request.time_range is None
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions
    assert (
        "TIME_SCOPE_SOURCE=BUSINESS_DEFAULT_ALL_AVAILABLE_HISTORY"
        in request.assumptions
    )
    assert request.missing_slots == []


@pytest.mark.parametrize(
    ("question", "manufacturer"),
    (
        (
            "查询Intuitive Surgical, Inc直观医疗公司的产品的含税销售总额。",
            "Intuitive Surgical, Inc直观医疗公司",
        ),
        (
            "查询理诺珐德国有限责任公司LivaNova Deutschland GmbH的产品的含税销售总额。",
            "理诺珐德国有限责任公司LivaNova Deutschland GmbH",
        ),
        (
            "查询碧迪生物科学 Becton, Dickinson and Company, BD Biosciences的产品的含税销售总额。",
            "碧迪生物科学 Becton, Dickinson and Company, BD Biosciences",
        ),
        (
            "查询B.Braun Surgical SA的产品的含税销售总额。",
            "B.Braun Surgical SA",
        ),
    ),
)
def test_legal_manufacturer_metric_uses_name_role_and_preserves_punctuation(
    question, manufacturer
):
    request = RuleBasedIntentClassifier().classify(
        question, IDENTITY, "manufacturer-lifetime-total"
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert request.entity == "产品"
    assert [metric.input for metric in request.metrics] == ["含税销售总额"]
    assert request.dimensions == []
    assert request.filters == [{
        "field": "厂家名称",
        "operator": "EQ",
        "value": manufacturer,
    }]
    assert request.semantic_entity_mentions == [manufacturer]
    assert request.time_range is None
    assert "SEMANTIC_ENTITY_ROLE=MANUFACTURER_NAME" in request.assumptions
    assert "TIME_SCOPE=ALL_TIME" in request.assumptions
    assert request.missing_slots == []


def test_named_organization_drops_geographic_token_split_from_its_full_name():
    request = CanonicalAnalysisRequest(
        conversation_id="named-hospital-geographic-subspan",
        tenant_id="t1",
        user_id="u1",
        original_question="查询上海市皮肤病医院的含税销售总额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[
            {"field": "地区", "operator": "EQ", "value": "上海市"},
            {
                "field": "医院名称",
                "operator": "EQ",
                "value": "上海市皮肤病医院",
            },
        ],
        semantic_entity_mentions=["上海市皮肤病医院", "上海市"],
    )

    RuleBasedIntentClassifier._drop_geographic_subspan_filters(request)

    assert request.filters == [{
        "field": "医院名称",
        "operator": "EQ",
        "value": "上海市皮肤病医院",
    }]
    assert request.semantic_entity_mentions == ["上海市皮肤病医院"]
    assert "REDUNDANT_GEOGRAPHIC_SUBSPAN_FILTER_DROPPED" in request.assumptions


def test_product_name_place_token_does_not_erase_explicit_sales_region():
    request = CanonicalAnalysisRequest(
        conversation_id="product-place-token-keeps-region",
        tenant_id="t1",
        user_id="u1",
        original_question="查询上海牌产品在上海市的销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[
            {"field": "地区", "operator": "EQ", "value": "上海市"},
            {"field": "商品名称", "operator": "EQ", "value": "上海牌产品"},
        ],
        semantic_entity_mentions=["上海牌产品", "上海市"],
    )

    RuleBasedIntentClassifier._drop_geographic_subspan_filters(request)

    assert len(request.filters) == 2
    assert request.semantic_entity_mentions == ["上海牌产品", "上海市"]


def test_grouped_trend_keeps_only_business_noun_as_semantic_entity_mention():
    request = RuleBasedIntentClassifier().classify(
        "按月分析外周插管中心静脉导管的销售趋势",
        IDENTITY,
        "grouped-trend-semantic-mention",
    )

    assert request.primary_intent == PrimaryIntent.TREND_ANALYSIS
    assert request.semantic_entity_mentions == ["外周插管中心静脉导管"]
    assert all("按月分析" not in value for value in request.semantic_entity_mentions)


@pytest.mark.parametrize(
    "question",
    [
        "那销售数量是多少？",
        "订单笔数呢？",
        "按月看销售额。",
        "统计各省份的含税销售总额。",
        "第一名的销售额是多少？",
        "那全国整体呢？",
    ],
)
def test_followup_scaffolding_is_never_emitted_as_semantic_entity(question):
    request = RuleBasedIntentClassifier().classify(
        question,
        IDENTITY,
        f"structural-entity-{question}",
    )

    assert request.semantic_entity_mentions == []


@pytest.mark.parametrize(
    "question",
    [
        "按产品统计含税销售总额排名前5。",
        "按月统计该省份的含税销售总额。",
    ],
)
def test_followup_scaffolding_is_never_emitted_as_catalog_filter(question):
    request = RuleBasedIntentClassifier().classify(
        question,
        IDENTITY,
        f"structural-filter-{question}",
    )

    assert all(
        str(item.get("value") or "") not in {"含税", "该省", "该省份"}
        for item in request.filters
    )
    assert request.semantic_entity_mentions == []


def test_typed_entity_filter_deduplicates_product_suffix_model_span():
    request = CanonicalAnalysisRequest(
        conversation_id="entity-span-dedup",
        tenant_id="t1",
        user_id="u1",
        original_question="查询空心纤维血液透析器产品合作的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        filters=[{
            "field": "商品名称",
            "operator": "EQ",
            "value": "空心纤维血液透析器",
        }],
        semantic_entity_mentions=[
            "空心纤维血液透析器",
            "空心纤维血液透析器产品",
        ],
    )

    RuleBasedIntentClassifier.sanitize_semantic_entity_mentions(request)

    assert request.semantic_entity_mentions == ["空心纤维血液透析器"]


@pytest.mark.parametrize(
    ("question", "entity", "field", "filter_field", "filter_value"),
    [
        (
            "上海市儿童医院采购了哪些产品？",
            "商品",
            "商品名称",
            "医院名称",
            "上海市儿童医院",
        ),
        (
            "杭州琅骏医疗科技有限公司销售了哪些产品？",
            "商品",
            "商品名称",
            "经销商名称",
            "杭州琅骏医疗科技有限公司",
        ),
        ("四川省的经销商有哪些？", "经销商", "经销商名称", None, None),
        ("合作的经销商有哪些？", "经销商", "经销商名称", None, None),
        ("合作的厂家有哪些？", "厂家", "厂家名称", None, None),
        ("主要向哪些医院供货？", "医院", "医院名称", None, None),
    ],
)
def test_natural_relationship_questions_have_complete_detail_projection(
    question, entity, field, filter_field, filter_value
):
    request = RuleBasedIntentClassifier().classify(
        question,
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "relationship-shape",
    )

    assert request.primary_intent == PrimaryIntent.DETAIL_QUERY
    assert request.entity == entity
    assert request.fields == [field]
    assert request.metrics == []
    assert "metric" not in request.missing_slots
    assert "fields" not in request.missing_slots
    if filter_field:
        assert {
            "field": filter_field,
            "operator": "EQ",
            "value": filter_value,
        } in request.filters


def test_relationship_count_question_uses_distinct_count_metric_without_grouping():
    request = RuleBasedIntentClassifier().classify(
        "一共有多少家经销商？",
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "relationship-count-shape",
    )

    assert request.primary_intent == PrimaryIntent.METRIC_QUERY
    assert [metric.input for metric in request.metrics] == ["已合作经销商数"]
    assert "经销商" not in request.dimensions
    assert request.missing_slots == []


def test_metric_subject_boundary_extracts_new_product_without_suffix_dictionary():
    request = RuleBasedIntentClassifier().classify(
        "统计绝对计数管整体的含税销售总额和订单笔数。",
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "open-product-subject",
    )

    assert {
        "field": "商品名称",
        "operator": "EQ",
        "value": "绝对计数管",
    } in request.filters
    assert request.semantic_entity_mentions == ["绝对计数管"]


def test_metric_subject_boundary_preserves_company_role():
    request = RuleBasedIntentClassifier().classify(
        "查询杭州琅骏医疗科技有限公司的含税销售总额。",
        TrustedIdentity(tenant_id="tenant", user_id="user"),
        "company-metric-subject",
    )

    assert {
        "field": "经销商名称",
        "operator": "EQ",
        "value": "杭州琅骏医疗科技有限公司",
    } in request.filters
    assert request.semantic_entity_mentions == ["杭州琅骏医疗科技有限公司"]


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
    assert request.dimensions == ["经销商"]
    assert request.filters == [
        {"field": "业务城市", "operator": "EQ", "value": "上海市"},
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
    assert "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE" in (
        request.assumptions
    )
    assert "REQUIRED_NAME_NON_NULL=经销商名称" in request.assumptions


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
    assert "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR" in request.assumptions
    assert "REQUIRED_NAME_NON_NULL=经销商名称" in request.assumptions
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
