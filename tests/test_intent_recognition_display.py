from app.domain.models import (
    AnalysisOperator,
    CanonicalAnalysisRequest,
    ContextMode,
    MetricRef,
    PrimaryIntent,
    TrustedIdentity,
)
from app.intent import RuleBasedIntentClassifier
from app.presentation.intent_recognition import (
    build_intent_recognition_display_v2,
    render_intent_recognition_display_v2,
)


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")


def test_transaction_partner_list_displays_natural_completed_question():
    request = RuleBasedIntentClassifier().classify(
        "查询最近一年销售过空心纤维血液透析器产品的经销商名单",
        IDENTITY,
        "natural-completion",
    )
    # The internal canonical renderer remains suitable for ASL, but must not
    # leak into the public completed-question row.
    request.rewritten_question = (
        "查询明细；对象：经销商；返回字段：经销商名称；"
        "筛选：商品名称 EQ 空心纤维血液透析器"
    )

    view = build_intent_recognition_display_v2(request)

    assert view.completed_question == (
        "查询最近一年销售过空心纤维血液透析器产品的经销商名单"
    )


def test_contextual_metric_sort_displays_a_natural_completed_question():
    request = CanonicalAnalysisRequest(
        conversation_id="coverage-sort-display",
        tenant_id="tenant",
        user_id="user",
        original_question="区域医院覆盖率按从高到低排序",
        rewritten_question=(
            "查询指标；指标：区域医院覆盖率；对象：经销商；"
            "分析维度：dealer；筛选：dim_city.city_name EQ 上海市"
        ),
        primary_intent=PrimaryIntent.METRIC_QUERY,
        context_mode=ContextMode.CURRENT_THREAD,
        metrics=[MetricRef(
            input="区域医院覆盖率",
            canonical_name="区域医院覆盖率",
            metric_id="81:screening_area_hospital_coverage",
        )],
        entity="经销商",
        dimensions=["dealer"],
        filters=[{
            "field": "dim_city.city_name",
            "operator": "EQ",
            "value": "上海市",
        }],
        operators=[AnalysisOperator.AGGREGATE, AnalysisOperator.SORT],
        assumptions=["SORT_DIRECTION=DESC", "TIME_SCOPE=ALL_TIME"],
        semantic_display_slots={
            "metrics": ["区域医院覆盖率"],
            "dimensions": ["经销商"],
        },
    )

    view = build_intent_recognition_display_v2(request)

    assert view.completed_question == (
        "统计上海市各个经销商的区域医院覆盖率，"
        "按区域医院覆盖率从高到低排序"
    )
    assert "查询指标；" not in view.completed_question


def test_catalog_resolved_scope_value_is_displayed_as_entity_not_query_object():
    request = CanonicalAnalysisRequest(
        conversation_id="coverage-entity-display",
        tenant_id="tenant",
        user_id="user",
        original_question="统计上海市各个经销商的区域医院覆盖率",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(
            input="区域医院覆盖率",
            canonical_name="区域医院覆盖率",
        )],
        entity="经销商",
        dimensions=["dealer"],
        filters=[{
            "field": "dim_city.city_name",
            "operator": "EQ",
            "value": "上海市",
        }],
        semantic_display_slots={
            "metrics": ["区域医院覆盖率"],
            "entity": "经销商",
            "dimensions": ["经销商"],
            "filters": [{
                "field": "城市名称",
                "operator": "EQ",
                "value": "上海市",
            }],
        },
    )

    before = request.model_copy(deep=True)
    view = build_intent_recognition_display_v2(request)
    rendered = render_intent_recognition_display_v2(view)

    assert view.entity_values == ["上海市"]
    assert "实体：上海市（来源：当前语义模型向量库）" in rendered
    assert "实体：经销商" not in rendered
    # Presentation V2 must not rewrite the ASL/SQL-facing query object.
    assert request == before
    assert request.entity == "经销商"


def test_control_enum_filter_is_not_promoted_to_display_entity():
    request = CanonicalAnalysisRequest(
        conversation_id="control-filter-entity-display",
        tenant_id="tenant",
        user_id="user",
        original_question="查询TDC-3产品的主要适用科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="产品",
        semantic_display_slots={
            "entity": "产品",
            "filters": [{
                "field": "适用科室类型",
                "operator": "EQ",
                "value": "1",
            }],
        },
    )

    view = build_intent_recognition_display_v2(request)

    assert view.entity_values == []
    assert view.entity == "产品"
