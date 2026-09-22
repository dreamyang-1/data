import json

from app.domain.models import (
    AnalysisOperator,
    AtomicTask,
    CanonicalAnalysisRequest,
    ContextMode,
    MetricRef,
    PrimaryIntent,
    TaskPlan,
    TrustedIdentity,
    TurnRelation,
)
from app.intent import RuleBasedIntentClassifier
from app.presentation.intent_recognition import (
    build_composite_intent_recognition_display_v2,
    build_intent_recognition_display_v2,
    render_composite_intent_recognition_display_v2,
    render_asl_extraction_json,
    render_intent_recognition_display_v2,
    render_resolved_intent_context_v2,
)


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")


def test_completed_question_displays_classifier_structure_before_catalog_binding():
    request = CanonicalAnalysisRequest(
        conversation_id="completed-structure-display",
        tenant_id="tenant",
        user_id="user",
        original_question="请提供南京哪些医院使用费森尤斯产品。",
        rewritten_question="请提供南京哪些医院使用费森尤斯产品。",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="医院",
        fields=["医院名称"],
        filters=[
            {"field": "城市", "operator": "EQ", "value": "南京"},
            {"field": "产品", "operator": "EQ", "value": "费森尤斯"},
        ],
    )

    rendered = render_intent_recognition_display_v2(
        build_intent_recognition_display_v2(request, semantic_extractions=())
    )

    parameters = build_intent_recognition_display_v2(request).structured_parameters
    assert "结构化参数提取：" not in rendered
    assert "南京（筛选值）" in parameters
    assert "费森尤斯产品（筛选值）" in parameters
    assert "医院（业务对象）" in parameters


def test_all_time_partner_list_preserves_region_brand_product_and_output():
    question = (
        "帮我找出上海地区正在销售竞争品牌万益特的血液净化管路的"
        "经销商名单"
    )
    request = CanonicalAnalysisRequest(
        conversation_id="all-time-partner-list-display",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        rewritten_question=question + "，已确认不限时间（全部历史）。",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        dimensions=["经销商"],
        filters=[
            {"field": "业务城市", "operator": "EQ", "value": "上海市"},
            {"field": "母品牌", "operator": "EQ", "value": "万益特"},
            {"field": "商品名称", "operator": "EQ", "value": "血液净化管路"},
        ],
        assumptions=[
            "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE",
            "TIME_SCOPE=ALL_TIME",
        ],
        turn_relation=TurnRelation.CLARIFICATION_RESPONSE,
        context_mode=ContextMode.CLARIFICATION_RESUME,
        # Catalog display grounding may be partial; it must not remove words
        # already extracted from the completed question.
        semantic_display_slots={
            "entity": "经销商",
            "dimensions": ["经销商"],
            "fields": ["经销商名称"],
            "filters": [
                {"field": "城市", "operator": "EQ", "value": "上海市"},
                {"field": "商品名称", "operator": "EQ", "value": "连续性血液净化管路"},
            ],
        },
    )

    semantic_extractions = (
        {"surface": "上海地区", "labels": ("城市", "地区")},
        {"surface": "正在销售", "labels": ("关系词", "业务状态")},
        {"surface": "竞争品牌", "labels": ("关系词", "品牌类型")},
        {"surface": "万益特", "labels": ("品牌",)},
        {"surface": "血液净化管路", "labels": ("产品名", "产品类型")},
        {"surface": "经销商名单", "labels": ("请求输出", "经销商")},
    )
    rendered = render_intent_recognition_display_v2(
        build_intent_recognition_display_v2(
            request, semantic_extractions=semantic_extractions
        )
    )

    assert "补全后的问题：查询不限时间（全部历史）内上海市" in rendered
    assert "竞争品牌万益特的血液净化管路产品的经销商名单" in rendered
    assert "最近一年" not in rendered
    view = build_intent_recognition_display_v2(request, semantic_extractions=semantic_extractions)
    assert view.structured_parameters == [
        "上海地区（城市/地区）", "正在销售（关系词/业务状态）", "竞争品牌（关系词/品牌类型）",
        "万益特（厂牌）", "血液净化管路（产品名/产品类型）", "经销商名单（请求输出/经销商）",
    ]
    assert "结构化参数提取：" not in rendered


def test_partner_list_model_mentions_have_stable_public_labels():
    question = "帮我找出上海地区正在销售竞争品牌万益特的血液净化管路的经销商名单"
    request = CanonicalAnalysisRequest(
        conversation_id="partner-list-model-display",
        tenant_id="tenant",
        user_id="user",
        original_question=question,
        rewritten_question=question,
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        dimensions=["经销商"],
        filters=[
            {"field": "业务城市", "operator": "EQ", "value": "上海市"},
            {"field": "母品牌", "operator": "EQ", "value": "万益特"},
            {"field": "商品名称", "operator": "EQ", "value": "血液净化管路"},
        ],
    )
    semantic_extractions = (
        {"surface": "上海地区", "labels": ("城市", "地区")},
        {"surface": "正在销售", "labels": ("业务状态", "关系词")},
        {"surface": "竞争品牌", "labels": ("品牌类型", "关系词")},
        {"surface": "万益特", "labels": ("品牌",)},
        {"surface": "血液净化管路", "labels": ("产品名", "泛化对象类型")},
        {"surface": "经销商", "labels": ("经销商", "实体类型")},
        {"surface": "名单", "labels": ("请求输出",)},
    )

    rendered = render_intent_recognition_display_v2(
        build_intent_recognition_display_v2(
            request, semantic_extractions=semantic_extractions
        )
    )

    view = build_intent_recognition_display_v2(request, semantic_extractions=semantic_extractions)
    assert view.structured_parameters == [
        "上海地区（城市/地区）", "正在销售（关系词/业务状态）", "竞争品牌（关系词/品牌类型）",
        "万益特（厂牌）", "血液净化管路（产品名/产品类型）", "经销商名单（请求输出/经销商）",
    ]
    assert "结构化参数提取：" not in rendered


def test_pending_merged_display_renders_final_slots_not_stale_rewrite():
    # The rewritten question was produced before the clarification answer
    # merged and still carries the pre-choice default one-year range. The
    # display must render the final slots instead of replaying that stale text.
    request = CanonicalAnalysisRequest(
        conversation_id="pending-merged-display",
        tenant_id="tenant",
        user_id="user",
        original_question="4",
        rewritten_question="查询最近一年上海、万益特经销商的经销商名称明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        filters=[
            {"field": "城市", "operator": "EQ", "value": "上海"},
            {"field": "产品名称", "operator": "EQ", "value": "万益特"},
        ],
        turn_relation=TurnRelation.CLARIFICATION_RESPONSE,
        context_mode=ContextMode.CURRENT_THREAD,
    )

    view = build_intent_recognition_display_v2(request, semantic_extractions=())

    assert "最近一年" not in view.completed_question
    assert "上海" in view.completed_question
    assert "万益特" in view.completed_question
    assert "经销商" in view.completed_question


def test_standalone_rewrite_without_pending_merge_is_kept_verbatim():
    request = CanonicalAnalysisRequest(
        conversation_id="standalone-rewrite-display",
        tenant_id="tenant",
        user_id="user",
        original_question="帮我找出上海地区血液净化管路的经销商名单",
        rewritten_question="查询最近一年上海销售过血液净化管路的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        filters=[{"field": "城市", "operator": "EQ", "value": "上海"}],
    )

    view = build_intent_recognition_display_v2(request, semantic_extractions=())

    assert view.completed_question == (
        "查询最近一年上海销售过血液净化管路的经销商名单"
    )


def test_resolved_context_and_intent_decision_render_as_nonduplicated_steps():
    semantic_extractions = [{
        "surface": "上海",
        "normalized_surface": "上海",
        "labels": ("筛选值",),
    }]
    context = render_resolved_intent_context_v2(
        original_question="分析上海销售趋势",
        completed_question="分析上海最近一年的销售趋势",
        business_domains=["医药销售域"],
        semantic_extractions=semantic_extractions,
    )
    request = RuleBasedIntentClassifier().classify(
        "分析上海最近一年的销售趋势", IDENTITY, "incremental-intent-display"
    )
    decision = render_intent_recognition_display_v2(
        build_intent_recognition_display_v2(
            request,
            business_domain_labels=["医药销售域"],
            semantic_extractions=semantic_extractions,
        ),
        include_resolved_context=False,
    )

    assert "用户原始问题：分析上海销售趋势" in context
    assert "补全后的问题：分析上海最近一年的销售趋势" in context
    assert "结构化参数提取：" not in context
    assert "业务域：" not in context
    assert "用户原始问题：" not in decision
    assert "补全后的问题：" not in decision
    assert "结构化参数提取：" not in decision
    assert "业务域：" not in decision
    assert "任务意图：" not in decision
    view = build_intent_recognition_display_v2(
        request, business_domain_labels=["医药销售域"], semantic_extractions=semantic_extractions,
    )
    assert "上海（筛选值）" in view.structured_parameters
    assert view.business_domains == ["医药销售域"]


def test_missing_parameter_display_names_the_exact_missing_slots():
    request = CanonicalAnalysisRequest(
        conversation_id="missing-parameter-display",
        tenant_id="tenant",
        user_id="user",
        original_question="请查询明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        missing_slots=["entity", "fields"],
    )

    view = build_intent_recognition_display_v2(request)
    rendered = render_intent_recognition_display_v2(view)

    assert "参数规范化：" not in rendered
    assert "任务意图：" not in rendered
    assert view.task_intent.startswith("明细查询")
    assert "缺少必要参数" not in rendered
    assert "返回的业务对象（例如经销商、医院、产品或科室）" in view.clarification_reason
    assert "返回字段" in view.clarification_reason
    assert "尚未提供返回的业务对象" in view.clarification_reason


def test_composite_display_lists_real_unique_child_intents_only():
    plan = TaskPlan(planner="DETERMINISTIC_RULE", tasks=[
        AtomicTask(task_id="task-1", question="查询产品明细"),
        AtomicTask(task_id="task-2", question="分析销售趋势"),
    ])

    rendered = render_composite_intent_recognition_display_v2(
        build_composite_intent_recognition_display_v2(
            "查询产品明细并分析销售趋势",
            plan,
            task_intents=[
                PrimaryIntent.DETAIL_QUERY,
                PrimaryIntent.TREND_ANALYSIS,
            ],
        )
    )

    assert "任务意图：" not in rendered
    view = build_composite_intent_recognition_display_v2(
        "查询产品明细并分析销售趋势", plan,
        task_intents=[PrimaryIntent.DETAIL_QUERY, PrimaryIntent.TREND_ANALYSIS],
    )
    assert [task.intent for task in view.tasks] == ["明细查询", "趋势分析"]
    assert "复合查询" not in rendered
    assert "共享业务标识" not in rendered
    assert "结构化拆分" not in rendered
    assert "参数规范化：已识别" not in rendered


def test_composite_display_groups_typed_parameters_under_each_completed_question():
    first = RuleBasedIntentClassifier().classify(
        "查询空心纤维血液透析器产品合作的经销商名单",
        IDENTITY,
        "typed-composite-display",
    )
    second = RuleBasedIntentClassifier().classify(
        "查询外周插管中心静脉导管合作的医院名单",
        IDENTITY,
        "typed-composite-display",
    )
    plan = TaskPlan(planner="STRUCTURED_MODEL", tasks=[
        AtomicTask(
            task_id="task-1",
            question="查询空心纤维血液透析器产品合作的经销商名单",
        ),
        AtomicTask(
            task_id="task-2",
            question="查询外周插管中心静脉导管合作的医院名单",
        ),
    ])
    semantic_extractions = (
        {
            "surface": "空心纤维血液透析器产品",
            "normalized_surface": "空心纤维血液透析器产品",
            "labels": ("筛选值",),
            "start_char": 2,
            "clause_id": "clause-1",
        },
        {
            "surface": "经销商",
            "normalized_surface": "经销商",
            "labels": ("业务对象", "分组维度"),
            "start_char": 17,
            "clause_id": "clause-1",
        },
        {
            "surface": "外周插管中心静脉导管",
            "normalized_surface": "外周插管中心静脉导管",
            "labels": ("筛选值",),
            "start_char": 25,
            "clause_id": "clause-2",
        },
        {
            "surface": "医院",
            "normalized_surface": "医院",
            "labels": ("业务对象", "分组维度"),
            "start_char": 39,
            "clause_id": "clause-2",
        },
    )

    rendered = render_composite_intent_recognition_display_v2(
        build_composite_intent_recognition_display_v2(
            "查询空心纤维血液透析器产品合作的经销商名单。"
            "查询外周插管中心静脉导管合作的医院名单。",
            plan,
            task_intents=[
                PrimaryIntent.DETAIL_QUERY,
                PrimaryIntent.DETAIL_QUERY,
            ],
            task_requests=[first, second],
            semantic_extractions=semantic_extractions,
        )
    )

    assert "补全后的问题：\n\n1. 查询空心纤维血液透析器产品合作的经销商名单" in rendered
    view = build_composite_intent_recognition_display_v2(
        "查询产品的合作机构", plan,
        task_intents=[PrimaryIntent.DETAIL_QUERY, PrimaryIntent.DETAIL_QUERY],
        task_requests=[first, second], semantic_extractions=semantic_extractions,
    )
    assert view.tasks[0].structured_parameters == [
        "空心纤维血液透析器产品（筛选值）", "经销商（业务对象/分组维度）",
    ]
    assert view.tasks[1].structured_parameters == [
        "外周插管中心静脉导管（筛选值）", "医院（业务对象/分组维度）",
    ]
    assert "2. 查询外周插管中心静脉导管合作的医院名单" in rendered
    assert "结构化参数提取：" not in rendered
    assert "任务意图：" not in rendered
    assert "语义提取字段：" not in rendered
    assert "参数规范化：已识别" not in rendered


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


def test_catalog_selected_project_is_kept_in_completed_question():
    request = RuleBasedIntentClassifier().classify(
        "查询最近一年浙江省销售过巴德血透产品的经销商名单",
        IDENTITY,
        "catalog-project-completion",
    )
    request.filters = [
        {"field": "省份名称", "operator": "EQ", "value": "浙江省"},
        {
            "field": "project.project_name",
            "operator": "EQ",
            "value": "巴德血透产品",
        },
    ]
    request.semantic_entity_mentions = ["浙江省"]
    request.assumptions = list(dict.fromkeys([
        *request.assumptions,
        "ACTIVE_DEFINITION=HAS_SALES_RECORD_IN_REQUESTED_TIME_RANGE",
        "DEFAULT_TIME_RANGE=LATEST_ONE_YEAR",
    ]))
    request.rewritten_question = (
        "查询明细；对象：经销商；返回字段：经销商名称；"
        "筛选：省份名称 EQ 浙江省、project.project_name EQ 巴德血透产品"
    )

    view = build_intent_recognition_display_v2(request)

    assert view.completed_question == (
        "查询最近一年浙江省销售过巴德血透产品的经销商名单"
    )
    assert "浙江省产品" not in view.completed_question


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


def test_catalog_resolved_scope_value_is_not_reconstructed_before_asl():
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
    assert "结构化提取" not in rendered
    assert "实体：上海市" not in rendered
    assert "实体：经销商" not in rendered
    # Presentation V2 must not rewrite the ASL/SQL-facing query object.
    assert request == before
    assert request.entity == "经销商"


def test_validated_asl_is_rendered_as_direct_json_without_field_reformatting():
    asl = {
        "version": "2.0",
        "subject": {"entity": "sales_order"},
        "metrics": [{"name": "sales_amount", "alias": "含税销售总额"}],
        "dimensions": [{"name": "city_name"}],
        "filters": [{"field": "city_name", "operator": "EQ", "value": "上海市"}],
        "ambiguity": [],
    }

    rendered = render_asl_extraction_json(asl)
    json_text = rendered.split("```json\n", 1)[1].rsplit("\n```", 1)[0]

    assert rendered.startswith("结构化提取（ASL）：\n```json\n")
    assert json.loads(json_text) == asl
    assert "`filters` 仅记录本次查询显式提出的筛选" in rendered
    assert "指标定义自带的固定口径由 SQL 翻译服务合并" in rendered
    assert "指标：" not in rendered
    assert "筛选条件：" not in rendered
    assert "字段说明：" not in rendered
    assert "subject.entity" not in rendered


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
