from datetime import date

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    MetricRef,
    PrimaryIntent,
    SemanticFilterBinding,
    TimeRange,
)
from app.services.question_rewriter import QuestionRewriter
from app.services.entity_extraction import EntityCandidate


class FakeSearcher:
    def __init__(self, matches):
        self.matches = matches
        self.calls = []

    async def search(
        self, query, *, semantic_model_id, business_domain_id,
        business_domain_ids=None,
    ):
        self.calls.append(
            (query, semantic_model_id, business_domain_id, business_domain_ids)
        )
        return self.matches


class FakeDisplayResolver(FakeSearcher):
    def __init__(self, matches):
        super().__init__([])
        self.display_matches = matches

    async def resolve_display_slots(
        self, candidates, *, semantic_model_id, business_domain_ids=None,
    ):
        self.calls.append((candidates, semantic_model_id, business_domain_ids))
        return self.display_matches


class FakeCandidateExtractor:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error

    async def extract(self, text):
        if self.error:
            raise self.error
        return self.candidates


class CandidateFailingSearcher(FakeSearcher):
    async def search(
        self, query, *, semantic_model_id, business_domain_id,
        business_domain_ids=None,
    ):
        self.calls.append(
            (query, semantic_model_id, business_domain_id, business_domain_ids)
        )
        if query == "candidate-brand":
            raise TimeoutError("candidate lookup timed out")
        return self.matches


@pytest.mark.asyncio
async def test_display_slots_only_keep_vector_resolved_canonical_values():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-display",
        tenant_id="t1",
        user_id="u1",
        original_question="查询费森尤斯产品的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=81,
        entity="经销商",
        dimensions=["经销商", "产品"],
        fields=["经销商名称", "模型臆造字段"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "费森尤斯"}],
        semantic_entity_mentions=["费森尤斯", "产品"],
    )
    resolver = FakeDisplayResolver([
        {"candidate_id": "entity:0", "canonical_name": "经销商"},
        {"candidate_id": "dimension:0", "canonical_name": "经销商"},
        {"candidate_id": "field:0", "canonical_name": "经销商名称"},
        {
            "candidate_id": "filter:0:0",
            "canonical_name": "商品品牌",
            "canonical_value": "费森尤斯医疗用品股份有限公司",
        },
        {
            "candidate_id": "mention:0",
            "canonical_name": "商品品牌",
            "canonical_value": "费森尤斯医疗用品股份有限公司",
        },
    ])

    await QuestionRewriter(resolver).ground_display_slots(request)

    assert request.semantic_display_slots == {
        "entity": "经销商",
        "dimensions": ["经销商"],
        "fields": ["经销商名称"],
        "filters": [{
            "field": "商品品牌",
            "operator": "EQ",
            "value": "费森尤斯医疗用品股份有限公司",
        }],
        "entity_values": ["费森尤斯医疗用品股份有限公司"],
    }
    # Display grounding is not allowed to rewrite executable intent state.
    assert request.fields == ["经销商名称", "模型臆造字段"]
    assert request.filters[0]["field"] == "商品名称"
    assert "semantic_display_slots" not in request.model_dump(mode="json")
    candidates = resolver.calls[0][0]
    filter_candidate = next(
        item for item in candidates if item["candidate_id"] == "filter:0:0"
    )
    assert filter_candidate["field_name"] == "商品名称"


@pytest.mark.asyncio
async def test_display_metric_uses_registered_compound_name_from_question_context():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-metric-context",
        tenant_id="t1",
        user_id="u1",
        original_question="统计上海市各个经销商的区域医院覆盖率",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        semantic_model_id=81,
        business_domain_ids=[205],
        metrics=[MetricRef(input="覆盖率")],
    )
    resolver = FakeDisplayResolver([{
        "candidate_id": "metric:0:context",
        "canonical_name": "区域医院覆盖率",
        "canonical_code": "screening_area_hospital_coverage",
        "business_domain_id": 205,
    }])

    await QuestionRewriter(resolver).ground_display_slots(request)

    assert request.semantic_display_slots["metrics"] == ["区域医院覆盖率"]
    # Display grounding stays isolated from executable intent state.
    assert request.metrics == [MetricRef(input="覆盖率")]
    candidates = resolver.calls[0][0]
    assert {
        "candidate_id": "metric:0:context",
        "slot": "metric",
        "value": "统计上海市各个经销商的区域医院覆盖率",
    } in candidates


@pytest.mark.asyncio
async def test_control_filter_value_is_not_displayed_as_business_entity_value():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-enum-display",
        tenant_id="t1",
        user_id="u1",
        original_question="查询TDC-3产品的主要适用科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=81,
        filters=[{"field": "适用科室类型", "operator": "EQ", "value": 1}],
        semantic_entity_mentions=["TDC-3"],
    )
    resolver = FakeDisplayResolver([
        {
            "candidate_id": "filter:0:0",
            "canonical_name": "适用科室类型",
            "canonical_value": "1",
        },
        {
            "candidate_id": "mention:0",
            "canonical_name": "规格型号",
            "canonical_value": "TDC-3",
        },
    ])

    await QuestionRewriter(resolver).ground_display_slots(request)

    assert request.semantic_display_slots["entity_values"] == ["TDC-3"]
    assert request.semantic_display_slots["filters"] == [{
        "field": "适用科室类型", "operator": "EQ", "value": "1",
    }]


def test_department_followup_context_includes_semantic_entity_value():
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="department-context",
        tenant_id="t1",
        user_id="u1",
        original_question="查询 TDC-3 产品的主要适用科室",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="产品",
        fields=["商品名称", "适用科室"],
        semantic_entity_mentions=["TDC-3"],
        filters=[{"field": "适用科室类型", "operator": "EQ", "value": 1}],
    )

    rewritten, applied = QuestionRewriter(None)._apply_context(
        "查询次要科室",
        previous,
        semantic_model_id=81,
        business_domain_ids=[],
        force_context=True,
    )

    assert applied is True
    assert "业务实体值=TDC-3" in rewritten
    assert '"value":1' in rewritten


def test_current_semantic_matches_ground_filter_and_dimension_labels():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-dimension-grounding",
        tenant_id="t1",
        user_id="u1",
        original_question="查询上海市江苏苏云品牌低值耗材的经销商清单",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        dimensions=["经销商", "地区", "品牌", "品类"],
        filters=[
            {"field": "地区", "operator": "EQ", "value": "上海市"},
            {"field": "品牌名称", "operator": "EQ", "value": "江苏苏云"},
            {"field": "商品分类", "operator": "EQ", "value": "低值耗材"},
        ],
    )
    matches = [
        {
            "score": 0.98,
            "entity_name": "经销商主数据",
            "attribute_name": "城市",
            "attribute_code": "dealer_city",
            "attribute_value": "上海市",
        },
        {
            "score": 0.97,
            "entity_name": "商品主数据",
            "attribute_name": "商品品牌",
            "attribute_code": "product_brand",
            "attribute_value": "江苏苏云医疗器材有限公司",
        },
        {
            "score": 0.96,
            "entity_name": "商品主数据",
            "attribute_name": "商品品类",
            "attribute_code": "product_category",
            "attribute_value": "低值耗材",
        },
    ]

    grounded = QuestionRewriter.ground_request_dimensions(request, matches)

    assert grounded.dimensions == ["经销商", "城市", "商品品牌", "商品品类"]
    assert grounded.filters == [
        {"field": "城市", "operator": "EQ", "value": "上海市"},
        {
            "field": "商品品牌",
            "operator": "EQ",
            "value": "江苏苏云医疗器材有限公司",
        },
        {"field": "商品品类", "operator": "EQ", "value": "低值耗材"},
    ]
    assert "SEMANTIC_DIMENSIONS_GROUNDED_FROM_CURRENT_MODEL" in grounded.assumptions


def test_current_semantic_catalog_rebinds_provisional_product_filter_to_brand():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-family-rebind",
        tenant_id="t1",
        user_id="u1",
        original_question="查询最近一年销售过费森尤斯产品的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "费森尤斯"}],
    )
    matches = [{
        "score": 0.98,
        "entity_name": "商品主数据",
        "attribute_name": "商品品牌",
        "attribute_code": "product_brand",
        "attribute_value": "费森尤斯",
    }]

    grounded = QuestionRewriter.ground_request_dimensions(request, matches)

    assert grounded.filters == [
        {"field": "商品品牌", "operator": "EQ", "value": "费森尤斯"},
    ]
    assert (
        "SEMANTIC_FILTER_FAMILY_REBOUND_FROM_CURRENT_MODEL"
        in grounded.assumptions
    )


@pytest.mark.asyncio
async def test_isolated_filter_lookup_makes_parent_brand_binding_authoritative():
    request = CanonicalAnalysisRequest(
        conversation_id="fresenius-parent-brand",
        tenant_id="t1",
        user_id="u1",
        original_question="按月分析费森尤斯产品的销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        entity="产品",
        dimensions=["商品"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "费森尤斯"}],
        semantic_entity_mentions=["费森尤斯"],
    )
    searcher = FakeSearcher([
        {
            "record_id": "parent-brand-fresenius",
            "score": 1.0,
            "entity_name": "生产厂家",
            "attribute_name": "母厂牌",
            "attribute_code": "parent_brand",
            "attribute_value": "费森尤斯",
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
        {
            "record_id": "manufacturer-fresenius",
            "score": 0.75,
            "entity_name": "生产厂家",
            "attribute_name": "厂家名称",
            "attribute_code": "manufacturer_name",
            "attribute_value": "费森尤斯医疗用品股份有限公司",
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
    ])

    ambiguities = await QuestionRewriter(searcher).ground_executable_filters(
        request,
        semantic_model_id=81,
        business_domain_id=None,
        business_domain_ids=[],
    )

    assert ambiguities == []
    assert searcher.calls == [("费森尤斯", 81, None, [])]
    assert request.filters == [
        {"field": "母厂牌", "operator": "EQ", "value": "费森尤斯"},
    ]
    assert request.dimensions == []
    assert request.resolved_business_domain_ids == [205]
    assert len(request.semantic_filter_bindings) == 1
    binding = request.semantic_filter_bindings[0]
    assert binding.attribute_code == "parent_brand"
    assert binding.canonical_value == "费森尤斯"
    assert binding.score == 1.0
    assert "FILTER_SUBJECT_REMOVED_FROM_TREND_GROUPING" in request.assumptions


def test_explicit_per_product_trend_keeps_product_grouping_after_brand_binding():
    request = CanonicalAnalysisRequest(
        conversation_id="fresenius-product-series",
        tenant_id="t1",
        user_id="u1",
        original_question="按月分析费森尤斯各产品的销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        entity="产品",
        dimensions=["商品"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "费森尤斯"}],
    )

    QuestionRewriter.ground_request_dimensions(request, [{
        "record_id": "parent-brand-fresenius",
        "score": 1.0,
        "entity_name": "生产厂家",
        "attribute_name": "母厂牌",
        "attribute_code": "parent_brand",
        "attribute_value": "费森尤斯",
        "business_domain_id": 205,
    }])

    assert request.dimensions == ["商品"]


def test_current_semantic_catalog_rebinds_manufacturer_to_published_name_field():
    value = "B.Braun Surgical SA"
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-manufacturer-rebind",
        tenant_id="t1",
        user_id="u1",
        original_question=f"查询{value}的产品的含税销售总额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        entity="产品",
        filters=[{"field": "厂家名称", "operator": "EQ", "value": value}],
    )
    matches = [{
        "score": 0.99,
        "entity_name": "manufacturer",
        "attribute_name": "生产企业名称",
        "attribute_code": "manufacturer.manufacturer_name",
        "attribute_value": "B. Braun Surgical S.A.",
    }]

    grounded = QuestionRewriter.ground_request_dimensions(request, matches)

    assert grounded.filters == [{
        "field": "生产企业名称",
        "operator": "EQ",
        "value": "B. Braun Surgical S.A.",
    }]
    assert "SEMANTIC_DIMENSIONS_GROUNDED_FROM_CURRENT_MODEL" in grounded.assumptions


def test_vector_canonical_value_replaces_model_entity_span_everywhere():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-value-canonicalization",
        tenant_id="t1",
        user_id="u1",
        original_question="查询空心纤维血液透析器产品合作的经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        filters=[{
            "field": "商品名称",
            "operator": "EQ",
            "value": "空心纤维血液透析器产品",
        }],
        semantic_entity_mentions=[
            "空心纤维血液透析器",
            "空心纤维血液透析器产品",
        ],
    )
    matches = [{
        "score": 0.98,
        "entity_name": "产品",
        "attribute_name": "商品名称",
        "attribute_code": "product.product_name",
        "attribute_value": "空心纤维血液透析器",
    }]

    grounded = QuestionRewriter.ground_request_dimensions(request, matches)

    assert grounded.filters == [{
        "field": "商品名称",
        "operator": "EQ",
        "value": "空心纤维血液透析器",
    }]
    assert grounded.semantic_entity_mentions == ["空心纤维血液透析器"]
    assert (
        "SEMANTIC_ENTITY_VALUES_CANONICALIZED_FROM_CURRENT_MODEL"
        in grounded.assumptions
    )


def test_unmatched_model_entity_span_is_preserved_for_downstream_verification():
    request = CanonicalAnalysisRequest(
        conversation_id="semantic-value-no-hit",
        tenant_id="t1",
        user_id="u1",
        original_question="查询未入向量库的新产品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        filters=[{
            "field": "商品名称",
            "operator": "EQ",
            "value": "未入向量库的新产品",
        }],
        semantic_entity_mentions=["未入向量库的新产品"],
    )

    grounded = QuestionRewriter.ground_request_dimensions(request, [{
        "score": 0.98,
        "entity_name": "产品",
        "attribute_name": "商品名称",
        "attribute_code": "product.product_name",
        "attribute_value": "其他产品",
    }])

    assert grounded.filters[0]["value"] == "未入向量库的新产品"
    assert grounded.semantic_entity_mentions == ["未入向量库的新产品"]


@pytest.mark.asyncio
async def test_semantic_matches_are_retained_without_forcing_text_rewrite():
    matches = [{"business_domain_id": 205,
        "score": 0.98,
        "entity_name": "商品主数据",
        "attribute_name": "商品品类",
        "attribute_code": "product_category",
        "attribute_value": "低值耗材",
    }]
    result = await QuestionRewriter(FakeSearcher(matches)).rewrite(
        "查询低值耗材",
        previous=None,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert result.rewritten_question == "查询低值耗材"
    assert result.events == []
    assert result.semantic_matches == matches


@pytest.mark.asyncio
async def test_shadow_candidates_are_audited_but_do_not_expand_semantic_search():
    searcher = FakeSearcher([])
    extractor = FakeCandidateExtractor([
        EntityCandidate("振德医疗", "品牌", 0.96, 2, 6)
    ])
    result = await QuestionRewriter(
        searcher, candidate_extractor=extractor, candidate_mode="shadow"
    ).rewrite(
        "查询振德医疗产品", previous=None,
        semantic_model_id=81, business_domain_id=None,
    )
    assert [call[0] for call in searcher.calls] == ["查询振德医疗产品"]
    assert result.rewritten_question == "查询振德医疗产品"
    assert result.events[0].kind == "ENTITY_CANDIDATE_SHADOW:品牌"


@pytest.mark.asyncio
async def test_assist_candidates_only_add_scoped_semantic_catalog_queries():
    searcher = FakeSearcher([])
    extractor = FakeCandidateExtractor([
        EntityCandidate("振德医疗", "品牌", 0.96, 2, 6)
    ])
    await QuestionRewriter(
        searcher, candidate_extractor=extractor, candidate_mode="assist"
    ).rewrite(
        "查询振德医疗产品", previous=None,
        semantic_model_id=81, business_domain_id=7,
        business_domain_ids=[7],
    )
    assert [call[0] for call in searcher.calls] == [
        "查询振德医疗产品", "振德医疗",
    ]
    assert all(call[1:] == (81, 7, [7]) for call in searcher.calls)


@pytest.mark.asyncio
async def test_candidate_extractor_failure_never_blocks_existing_rewrite_path():
    searcher = FakeSearcher([])
    result = await QuestionRewriter(
        searcher,
        candidate_extractor=FakeCandidateExtractor(error=TimeoutError()),
        candidate_mode="assist",
    ).rewrite(
        "查询本月销售额", previous=None,
        semantic_model_id=81, business_domain_id=None,
    )
    assert result.rewritten_question == "查询本月销售额"
    assert result.degraded is False


@pytest.mark.asyncio
async def test_candidate_catalog_failure_never_degrades_primary_rewrite_path():
    searcher = CandidateFailingSearcher([])
    result = await QuestionRewriter(
        searcher,
        candidate_extractor=FakeCandidateExtractor([
            EntityCandidate("candidate-brand", "brand", 0.96, 0, 15)
        ]),
        candidate_mode="assist",
    ).rewrite(
        "candidate-brand sales", previous=None,
        semantic_model_id=81, business_domain_id=None,
    )
    assert result.rewritten_question == "candidate-brand sales"
    assert result.degraded is False


@pytest.mark.asyncio
async def test_context_is_added_before_intent_classification():
    previous = CanonicalAnalysisRequest(semantic_model_id=5, business_domain_ids=[9],
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(
            start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
        ),
    )
    result = await QuestionRewriter(None).rewrite(
        "那上海呢？",
        previous=previous,
        semantic_model_id=5,
        business_domain_id=9,
    )
    assert result.context_applied is True
    assert "指标=销售额" in result.rewritten_question
    assert "时间=2026-07-01至2026-08-01" in result.rewritten_question


@pytest.mark.asyncio
async def test_admitted_followup_can_force_context_for_model_completion():
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="c-force-model-context",
        tenant_id="t1",
        user_id="u1",
        original_question="按月分析A产品销售趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        metrics=[MetricRef(input="销售额")],
        entity="产品",
        dimensions=["产品"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "A产品"}],
        time_range=TimeRange(
            start=date(2025, 9, 1), end_exclusive=date(2026, 9, 2)
        ),
    )

    without_force = await QuestionRewriter(None).rewrite(
        "11月较10月下降多少",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
    )
    forced = await QuestionRewriter(None).rewrite(
        "11月较10月下降多少",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
        force_context=True,
    )

    assert without_force.context_applied is False
    assert forced.context_applied is True
    assert "指标=销售额" in forced.rewritten_question
    assert "实体=产品" in forced.rewritten_question
    assert '"value":"A产品"' in forced.rewritten_question


@pytest.mark.asyncio
async def test_granularity_only_followup_inherits_metric_and_period():
    previous = CanonicalAnalysisRequest(semantic_model_id=5, business_domain_ids=[9],
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询2026年7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(
            start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
        ),
    )

    result = await QuestionRewriter(None).rewrite(
        "按日统计", previous=previous, semantic_model_id=5, business_domain_id=9
    )

    assert result.context_applied is True
    assert "指标=销售额" in result.rewritten_question
    assert "时间=2026-07-01至2026-08-01" in result.rewritten_question


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["按月给我", "改成按季度", "不按月了，按季度"])
async def test_natural_granularity_followup_inherits_metric_and_period(question):
    previous = CanonicalAnalysisRequest(semantic_model_id=81, business_domain_ids=[205],
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询最近一年销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(
            start=date(2025, 8, 28), end_exclusive=date(2026, 8, 29)
        ),
    )

    result = await QuestionRewriter(None).rewrite(
        question, previous=previous, semantic_model_id=81, business_domain_id=205
    )

    assert result.context_applied is True
    assert "指标=销售额" in result.rewritten_question
    assert "时间=2025-08-28至2026-08-29" in result.rewritten_question


@pytest.mark.asyncio
async def test_explicit_followup_month_replaces_previous_time_range():
    previous = CanonicalAnalysisRequest(semantic_model_id=5, business_domain_ids=[9],
        conversation_id="c1",
        tenant_id="t1",
        user_id="u1",
        original_question="查询2026年7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(
            start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)
        ),
    )

    result = await QuestionRewriter(None).rewrite(
        "那6月呢", previous=previous, semantic_model_id=5, business_domain_id=9
    )

    assert result.context_applied is True
    assert result.rewritten_question == "查询2026年6月销售额"


@pytest.mark.asyncio
async def test_dimension_and_top_n_modifier_inherits_previous_metric_and_time():
    previous = CanonicalAnalysisRequest(semantic_model_id=6,
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询本月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1)),
    )

    result = await QuestionRewriter(None).rewrite(
        "按供应商拆分，并显示前5名",
        previous=previous,
        semantic_model_id=6,
        business_domain_id=None,
    )

    assert result.context_applied is True
    assert "指标=销售额" in result.rewritten_question
    assert "时间=2026-08-01至2026-09-01" in result.rewritten_question


@pytest.mark.asyncio
async def test_additive_metric_followup_keeps_previous_metric_and_grouping():
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="c-add-metric",
        tenant_id="t1",
        user_id="u1",
        original_question="按经销商查询整体业务规模",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="整体业务规模", canonical_name="整体业务规模")],
        entity="经销商",
        dimensions=["经销商"],
        filters=[{"field": "商品名称", "operator": "EQ", "value": "医用外科口罩"}],
    )

    result = await QuestionRewriter(None).rewrite(
        "再加上订单笔数，其他条件不变。",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
    )

    assert result.context_applied is True
    assert "指标=整体业务规模" in result.rewritten_question
    assert "维度=经销商" in result.rewritten_question
    assert '"value":"医用外科口罩"' in result.rewritten_question


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["前三名", "只显示前五名", "改成后三名"])
async def test_chinese_numeral_ranking_followup_inherits_context(question):
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询最近一年经销商销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        dimensions=["经销商"],
        time_range=TimeRange(start=date(2025, 8, 29), end_exclusive=date(2026, 8, 30)),
    )

    result = await QuestionRewriter(None).rewrite(
        question, previous=previous, semantic_model_id=81, business_domain_id=None
    )

    assert result.context_applied is True
    assert "指标=销售额" in result.rewritten_question
    assert "维度=经销商" in result.rewritten_question


@pytest.mark.asyncio
async def test_extrema_followup_uses_previous_detail_entity_as_dimension():
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询某经销商销售过的产品",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="商品",
        fields=["商品名称"],
        time_range=TimeRange(start=date(2025, 8, 29), end_exclusive=date(2026, 8, 30)),
    )

    result = await QuestionRewriter(None).rewrite(
        "其中销售额最高的是哪个？",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
    )

    assert result.context_applied is True
    assert "实体=商品" in result.rewritten_question
    assert "维度=商品" in result.rewritten_question


@pytest.mark.asyncio
async def test_relationship_followup_does_not_inherit_old_metric_or_dimension():
    previous = CanonicalAnalysisRequest(semantic_model_id=81,
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询商品销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        entity="商品",
        dimensions=["商品"],
        time_range=TimeRange(start=date(2025, 8, 29), end_exclusive=date(2026, 8, 30)),
    )

    result = await QuestionRewriter(None).rewrite(
        "它卖给了哪些医院？",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
    )

    assert result.context_applied is True
    assert "指标=销售额" not in result.rewritten_question
    assert "维度=商品" not in result.rewritten_question


@pytest.mark.asyncio
async def test_two_digit_year_followup_is_normalized_before_context_merge():
    previous = CanonicalAnalysisRequest(
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询最近半年经销商名单",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
        time_range=TimeRange(start=date(2026, 2, 28), end_exclusive=date(2026, 8, 30)),
    )

    result = await QuestionRewriter(None).rewrite(
        "那查询一下25年12月1号到31号的",
        previous=previous,
        semantic_model_id=81,
        business_domain_id=None,
    )

    assert "2025年12月1号到31号" in result.rewritten_question
    assert "时间=2026-02-28" not in result.rewritten_question


@pytest.mark.asyncio
async def test_closed_form_month_switch_skips_remote_entity_search():
    searcher = FakeSearcher([])
    previous = CanonicalAnalysisRequest(semantic_model_id=5, business_domain_ids=[9],
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询2026年7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)),
    )
    result = await QuestionRewriter(searcher).rewrite(
        "那6月呢", previous=previous, semantic_model_id=5, business_domain_id=9
    )
    assert result.rewritten_question == "查询2026年6月销售额"
    assert searcher.calls == []


def test_free_text_filter_is_not_misclassified_as_deterministic_slot_update():
    assert QuestionRewriter.is_deterministic_slot_update("那6月呢")
    assert QuestionRewriter.is_deterministic_slot_update(
        "2025年10月17日至2025年12月30日"
    )
    assert QuestionRewriter.is_deterministic_slot_update(
        "三个分析维度都按2025年10月17日至2025年12月30日"
    )
    assert QuestionRewriter.is_deterministic_slot_update("换成订单量")
    assert QuestionRewriter.is_deterministic_slot_update("不按区域，按渠道拆分")
    assert not QuestionRewriter.is_deterministic_slot_update("只看华南")
    assert not QuestionRewriter.is_deterministic_slot_update("换成儿童口罩")
    assert not QuestionRewriter.is_deterministic_slot_update(
        "换个问题，查询2025年销售额"
    )


@pytest.mark.asyncio
async def test_high_confidence_typo_is_normalized():
    searcher = FakeSearcher([
        {"business_domain_id": 9,
            "score": 0.95,
            "entity_name": "销售区域",
            "entity_alias": '["区域", "片区"]',
            "attribute_name": "地区名称",
            "attribute_code": "region_name",
            "attribute_value": "北京区域",
        }
    ])
    result = await QuestionRewriter(searcher).rewrite(
        "查询北经区域本月销售额",
        previous=None,
        semantic_model_id=5,
        business_domain_id=9,
    )
    assert result.rewritten_question == "查询北京区域本月销售额"
    assert result.events[0].kind == "TYPO"


@pytest.mark.asyncio
async def test_known_metric_typo_is_normalized_without_vector_service():
    result = await QuestionRewriter(None).rewrite(
        "查询2026年7月销受额", previous=None,
        semantic_model_id=6, business_domain_id=9,
    )

    assert result.rewritten_question == "查询2026年7月销售额"
    assert len(result.events) == 1
    assert result.events[0].kind == "TYPO_LOCAL"
    assert result.events[0].original == "销受额"


@pytest.mark.asyncio
async def test_search_failure_degrades_without_changing_question():
    class BrokenSearcher:
        async def search(self, *args, **kwargs):
            raise TimeoutError("timeout")

    result = await QuestionRewriter(BrokenSearcher()).rewrite(
        "查询销售额", previous=None, semantic_model_id=5, business_domain_id=9
    )
    assert result.rewritten_question == "查询销售额"
    assert result.degraded is True


@pytest.mark.asyncio
async def test_missing_scope_skips_remote_search():
    searcher = FakeSearcher([])
    result = await QuestionRewriter(searcher).rewrite(
        "查询销售额", previous=None, semantic_model_id=None, business_domain_id=9
    )
    assert result.rewritten_question == "查询销售额"
    assert searcher.calls == []


@pytest.mark.asyncio
async def test_auto_domain_still_searches_across_the_semantic_model():
    searcher = FakeSearcher([])
    result = await QuestionRewriter(searcher).rewrite(
        "查询销售额", previous=None, semantic_model_id=5, business_domain_id=None
    )
    assert result.rewritten_question == "查询销售额"
    assert searcher.calls == [("查询销售额", 5, None, [])]


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["这个周末天气怎么样", "为什么Python这么慢"])
async def test_unrelated_weak_pronouns_do_not_inherit_data_context(question):
    previous = CanonicalAnalysisRequest(
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询2026年7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1)),
        semantic_model_id=6,
    )
    result = await QuestionRewriter(None).rewrite(
        question, previous=previous, semantic_model_id=6,
        business_domain_id=None,
    )
    assert result.context_applied is False
    assert result.rewritten_question == question


@pytest.mark.asyncio
async def test_context_is_not_inherited_after_semantic_model_switch():
    previous = CanonicalAnalysisRequest(
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询2026年7月销售额",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        semantic_model_id=6,
    )
    result = await QuestionRewriter(None).rewrite(
        "那华东呢", previous=previous, semantic_model_id=8,
        business_domain_id=None,
    )
    assert result.context_applied is False


@pytest.mark.asyncio
async def test_explicit_current_metric_does_not_inherit_conflicting_previous_metric():
    previous = CanonicalAnalysisRequest(semantic_model_id=6, business_domain_ids=[9],
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询本月销售额", primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")],
        time_range=TimeRange(start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1)),
    )
    result = await QuestionRewriter(None).rewrite(
        "那订单量呢", previous=previous, semantic_model_id=6, business_domain_id=9
    )
    assert result.context_applied is True
    assert "指标=销售额" not in result.rewritten_question
    assert "时间=2026-08-01至2026-09-01" in result.rewritten_question


@pytest.mark.parametrize(
    "question",
    [
        "那再查询一下最近一个月的",
        "那最近半年呢",
        "那查询一下25年12月1号到31号的",
        "那2025年12月份的含税销售总额是多少",
    ],
)
def test_natural_time_query_wrappers_use_deterministic_slot_update(question):
    assert QuestionRewriter.is_deterministic_time_update(question)


@pytest.mark.asyncio
async def test_low_score_candidate_never_changes_question():
    searcher = FakeSearcher([{
        "score": 0.70, "entity_name": "北京区域", "entity_alias": '["北区"]',
        "attribute_code": "region", "attribute_value": "北京",
    }])
    result = await QuestionRewriter(searcher).rewrite(
        "查北区销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert result.rewritten_question == "查北区销售额"
    assert result.events == []


@pytest.mark.asyncio
async def test_ambiguous_same_surface_candidates_are_not_rewritten():
    searcher = FakeSearcher([
        {"business_domain_id": 9, "score": 0.95, "entity_name": "华北大区", "entity_alias": '["北区"]', "attribute_code": "r1"},
        {"business_domain_id": 9, "score": 0.93, "entity_name": "北京城区", "entity_alias": '["北区"]', "attribute_code": "r2"},
    ])
    result = await QuestionRewriter(searcher).rewrite(
        "查北区销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert result.rewritten_question == "查北区销售额"
    assert result.events == []
    assert len(result.semantic_ambiguities) == 1
    ambiguity = result.semantic_ambiguities[0]
    assert ambiguity.type == "entity_role"
    assert ambiguity.phrase == "北区"
    assert ambiguity.blocking is True
    assert ambiguity.semantic_model_id == 6
    assert len(ambiguity.candidates) == 2


@pytest.mark.asyncio
async def test_semantic_model_version_is_carried_into_live_ambiguity():
    searcher = FakeSearcher([
        {"business_domain_id": 205,
            "score": 0.96,
            "entity_name": "商品主数据",
            "attribute_name": "商品品牌",
            "attribute_code": "brand",
            "attribute_value": "费森尤斯",
            "semantic_model_version": "v2026-09-02",
        },
        {"business_domain_id": 205,
            "score": 0.94,
            "entity_name": "厂家主数据",
            "attribute_name": "厂家名称",
            "attribute_code": "manufacturer",
            "attribute_value": "费森尤斯",
            "semantic_model_version": "v2026-09-02",
        },
    ])

    result = await QuestionRewriter(searcher).rewrite(
        "查询费森尤斯的经销商",
        previous=None,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert result.semantic_model_version == "v2026-09-02"
    assert result.semantic_ambiguities[0].semantic_model_version == "v2026-09-02"
    assert result.semantic_ambiguities[0].affected_slots == ["filters"]


@pytest.mark.asyncio
async def test_confirmed_city_attribute_does_not_reopen_province_city_ambiguity():
    matches = [
        {
            "score": 1.0,
            "record_id": "province-shanghai",
            "entity_name": "省份",
            "attribute_name": "省份名称",
            "attribute_code": "province_name",
            "attribute_value": "上海市",
            "business_domain_id": 205,
        },
        {
            "score": 1.0,
            "record_id": "city-shanghai",
            "entity_name": "市",
            "attribute_name": "城市名称",
            "attribute_code": "city_name",
            "attribute_value": "上海市",
            "business_domain_id": 205,
        },
    ]
    request = CanonicalAnalysisRequest(
        conversation_id="confirmed-shanghai-city",
        tenant_id="t1",
        user_id="u1",
        original_question="统计上海市各个经销商的区域医院覆盖率",
        primary_intent=PrimaryIntent.METRIC_QUERY,
        filters=[{"field": "城市名称", "operator": "EQ", "value": "上海市"}],
        semantic_filter_bindings=[SemanticFilterBinding(
            filter_index=0,
            input_value="上海市",
            canonical_value="上海市",
            canonical_name="城市名称",
            attribute_code="city_name",
            record_id="city-shanghai",
            score=1.0,
            business_domain_id=205,
        )],
        assumptions=["SEMANTIC_AMBIGUITY_CONFIRMED_ATTRIBUTE=city_name"],
    )

    ambiguities = await QuestionRewriter(
        FakeSearcher(matches)
    ).ground_executable_filters(
        request,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert ambiguities == []
    assert request.filters[0]["field"] == "城市名称"
    assert request.semantic_filter_bindings[0].attribute_code == "city_name"


@pytest.mark.asyncio
async def test_unique_full_hospital_name_suppresses_nested_alias_ambiguity():
    searcher = FakeSearcher([
        {
            "score": 0.99,
            "entity_name": "上海市口腔医院",
            "entity_alias": '["口腔医院"]',
            "attribute_name": "医院名称",
            "attribute_code": "hospital.hospital_name",
            "attribute_value": "上海市口腔医院",
        },
        {
            "score": 0.97,
            "entity_name": "浦东新区口腔医院",
            "entity_alias": '["口腔医院"]',
            "attribute_name": "医院名称",
            "attribute_code": "hospital.hospital_name",
            "attribute_value": "浦东新区口腔医院",
        },
    ])

    result = await QuestionRewriter(searcher).rewrite(
        "查询上海市口腔医院的含税销售总额。",
        previous=None,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert result.semantic_ambiguities == []


@pytest.mark.asyncio
async def test_legal_manufacturer_surface_is_disambiguated_by_business_role():
    value = "B.Braun Surgical SA"
    searcher = FakeSearcher([
        {
            "score": 0.96,
            "entity_name": "厂家主数据",
            "attribute_name": "厂家名称",
            "attribute_code": "manufacturer.manufacturer_name",
            "attribute_value": value,
        },
        {
            "score": 0.95,
            "entity_name": "产品主数据",
            "attribute_name": "商品名称",
            "attribute_code": "product.product_name",
            "attribute_value": value,
        },
    ])

    result = await QuestionRewriter(searcher).rewrite(
        f"查询{value}的产品的含税销售总额。",
        previous=None,
        semantic_model_id=81,
        business_domain_id=205,
    )

    assert result.semantic_ambiguities == []


@pytest.mark.asyncio
async def test_rewrite_that_destroys_intent_term_is_rejected():
    searcher = FakeSearcher([{"business_domain_id": 9,
        "score": 0.99, "entity_name": "一号门店", "entity_alias": '["销售额趋势"]',
        "attribute_code": "shop", "attribute_value": "一号门店",
    }])
    result = await QuestionRewriter(searcher).rewrite(
        "分析销售额趋势", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert result.rewritten_question == "分析销售额趋势"
    assert result.degraded is True


@pytest.mark.asyncio
async def test_one_character_alias_is_never_auto_rewritten():
    searcher = FakeSearcher([{
        "score": 0.99, "entity_name": "销售区域", "entity_alias": '["北"]',
        "attribute_code": "region", "attribute_value": "北京",
    }])
    result = await QuestionRewriter(searcher).rewrite(
        "查询华北销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert result.rewritten_question == "查询华北销售额"
    assert result.events == []


@pytest.mark.asyncio
async def test_ambiguity_is_checked_for_each_surface():
    searcher = FakeSearcher([
        {"business_domain_id": 9, "score": 0.99, "entity_name": "北京", "entity_alias": '["京城"]', "attribute_code": "r0"},
        {"business_domain_id": 9, "score": 0.95, "entity_name": "华北大区", "entity_alias": '["北区"]', "attribute_code": "r1"},
        {"business_domain_id": 9, "score": 0.93, "entity_name": "北京城区", "entity_alias": '["北区"]', "attribute_code": "r2"},
    ])
    result = await QuestionRewriter(searcher).rewrite(
        "查京城和北区销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert "北京" in result.rewritten_question
    assert "北区" in result.rewritten_question


@pytest.mark.asyncio
async def test_alias_does_not_rewrite_appended_confirmed_context():
    previous = CanonicalAnalysisRequest(semantic_model_id=6, business_domain_ids=[9],
        conversation_id="c1", tenant_id="t1", user_id="u1",
        original_question="查询北京销售额", primary_intent=PrimaryIntent.METRIC_QUERY,
        metrics=[MetricRef(input="销售额")], entity="销售区域",
    )
    searcher = FakeSearcher([{
        "score": 0.99, "entity_name": "销售大区", "entity_alias": '["销售区域"]',
        "attribute_code": "region",
    }])
    result = await QuestionRewriter(searcher).rewrite(
        "继续看趋势", previous=previous, semantic_model_id=6, business_domain_id=9
    )
    assert "实体=销售区域" in result.rewritten_question
    assert "实体=销售大区" not in result.rewritten_question


@pytest.mark.asyncio
async def test_typo_rewrite_cannot_change_numeric_identifier():
    searcher = FakeSearcher([{
        "score": 0.99, "entity_name": "供应商2025", "entity_alias": '[]',
        "attribute_code": "supplier", "attribute_value": "供应商2025",
    }])
    result = await QuestionRewriter(searcher).rewrite(
        "查询供应商2026销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert "2026" in result.rewritten_question
    assert result.events == []
@pytest.mark.asyncio
async def test_temporal_scope_before_polite_verb_is_normalized_without_losing_terms():
    result = await QuestionRewriter(None).rewrite(
        "按月请计算空心纤维血液透析器产品的含税销售总额。",
        previous=None,
        semantic_model_id=None,
        business_domain_id=None,
    )

    assert result.rewritten_question == "按月统计空心纤维血液透析器产品的含税销售总额。"
    assert any(event.kind == "POLITE_WORD_ORDER" for event in result.events)
    assert any(
        event.kind == "GROUPED_CALCULATION_WORDING" for event in result.events
    )
