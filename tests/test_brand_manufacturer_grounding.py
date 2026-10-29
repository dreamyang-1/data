"""SPRINT-20260917-C: exact catalog value must beat a same-family fuzzy hit.

Real production shape: the intent model emits a provisional ``厂家名称``
filter (manufacturer family) with value ``费森尤斯``. The scoped catalog
returns the unique exact hit ``母厂牌/parent_brand=费森尤斯`` (score 1.0)
plus a fuzzy ``厂家名称/manufacturer_name=Fresenius ... KGaA`` (0.75).
Binding must follow the unique exact catalog value, not the model's
provisional field family.
"""

import pytest

from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent
from app.services.question_rewriter import QuestionRewriter


def _fresenius_candidates():
    return [
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
            "attribute_value": "Fresenius Medical Care AG & Co. KGaA",
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
    ]


def _request(value="费森尤斯", field="厂家名称"):
    return CanonicalAnalysisRequest(
        conversation_id="brand-manufacturer-grounding",
        tenant_id="t1",
        user_id="u1",
        original_question="分析上海市费森尤斯产品最近一年的销售趋势。",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        entity="产品",
        filters=[
            {"field": "业务城市", "operator": "EQ", "value": "上海市"},
            {"field": field, "operator": "EQ", "value": value},
        ],
        semantic_entity_mentions=["上海市", "费森尤斯"],
    )


def test_unique_exact_catalog_value_beats_same_family_fuzzy_candidate():
    """Probe 1: production shape — model field already in manufacturer family."""
    request = _request()
    QuestionRewriter.ground_request_dimensions(request, _fresenius_candidates())

    bound = [binding for binding in request.semantic_filter_bindings
             if binding.filter_index == 1]
    assert len(bound) == 1
    assert bound[0].attribute_code == "parent_brand"
    assert bound[0].canonical_value == "费森尤斯"
    assert request.filters[1] == {
        "field": "母厂牌", "operator": "EQ", "value": "费森尤斯",
    }


def test_full_legal_manufacturer_exact_hit_still_binds_manufacturer_name():
    """A unique exact hit on the full legal name keeps the manufacturer field."""
    legal = "Fresenius Medical Care AG & Co. KGaA"
    request = _request(value=legal)
    QuestionRewriter.ground_request_dimensions(request, [
        {
            "record_id": "manufacturer-fresenius",
            "score": 1.0,
            "entity_name": "生产厂家",
            "attribute_name": "厂家名称",
            "attribute_code": "manufacturer_name",
            "attribute_value": legal,
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
        {
            "record_id": "parent-brand-fresenius",
            "score": 0.82,
            "entity_name": "生产厂家",
            "attribute_name": "母厂牌",
            "attribute_code": "parent_brand",
            "attribute_value": "费森尤斯",
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
    ])

    bound = [binding for binding in request.semantic_filter_bindings
             if binding.filter_index == 1]
    assert len(bound) == 1
    assert bound[0].attribute_code == "manufacturer_name"
    assert bound[0].canonical_value == legal
    assert request.filters[1]["field"] == "厂家名称"


def test_exact_literal_on_two_attributes_is_not_guessed():
    """The same literal exactly present on two attributes must fail closed."""
    request = _request()
    ambiguous = [
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
            "score": 1.0,
            "entity_name": "生产厂家",
            "attribute_name": "厂家名称",
            "attribute_code": "manufacturer_name",
            "attribute_value": "费森尤斯",
            "business_domain_id": 205,
            "semantic_model_version": "published-31",
        },
    ]

    class ValueSearcher:
        def __init__(self):
            self.calls = []

        async def search(self, query, *, semantic_model_id,
                         business_domain_id, business_domain_ids=None):
            self.calls.append((query, semantic_model_id, business_domain_id,
                               business_domain_ids))
            return ambiguous

    searcher = ValueSearcher()
    ambiguities = asyncio_run_ground(searcher, request)

    assert ambiguities != []
    # No silent pick by list order: the filter keeps the model's provisional
    # field and receives no authoritative binding.
    assert not any(
        binding.filter_index == 1 for binding in request.semantic_filter_bindings
    )


def test_duplicate_records_for_one_exact_attribute_do_not_create_false_ambiguity():
    """Repeated catalog rows for one semantic field still represent one choice."""
    request = _request()
    duplicated = [
        _fresenius_candidates()[0],
        {
            **_fresenius_candidates()[0],
            "record_id": "parent-brand-fresenius-duplicate",
            "score": 0.98,
        },
        _fresenius_candidates()[1],
    ]

    QuestionRewriter.ground_request_dimensions(request, duplicated)

    bound = [
        binding for binding in request.semantic_filter_bindings
        if binding.filter_index == 1
    ]
    assert len(bound) == 1
    assert bound[0].attribute_code == "parent_brand"
    assert request.filters[1]["field"] == "母厂牌"


def asyncio_run_ground(searcher, request):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        QuestionRewriter(searcher).ground_executable_filters(
            request,
            semantic_model_id=81,
            business_domain_id=None,
            business_domain_ids=[],
        )
    )


@pytest.mark.asyncio
async def test_three_layer_consistency_filter_binding_and_contract_agree():
    """Filter, SemanticFilterBinding, and intent-ASL contract see one binding."""
    request = _request()
    QuestionRewriter.ground_request_dimensions(request, _fresenius_candidates())

    from app.services.intent_asl_contract import build_intent_asl_contract
    contract = build_intent_asl_contract(request)
    filters = contract.get("filters") or []
    brand_filter = next(item for item in filters if item.get("value") == "费森尤斯")
    binding = next(binding for binding in request.semantic_filter_bindings
                   if binding.filter_index == 1)
    assert brand_filter["field"] == binding.canonical_name == "母厂牌"
    assert brand_filter["value"] == binding.canonical_value == "费森尤斯"
    assert binding.attribute_code == "parent_brand"
