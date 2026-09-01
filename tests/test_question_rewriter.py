from datetime import date

import pytest

from app.domain.models import (
    CanonicalAnalysisRequest,
    MetricRef,
    PrimaryIntent,
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
@pytest.mark.parametrize("question", ["前三名", "只显示前五名", "改成后三名"])
async def test_chinese_numeral_ranking_followup_inherits_context(question):
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
    previous = CanonicalAnalysisRequest(
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
        {
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
    assert searcher.calls == [("查询销售额", 5, None, None)]


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
    previous = CanonicalAnalysisRequest(
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
        {"score": 0.95, "entity_name": "华北大区", "entity_alias": '["北区"]', "attribute_code": "r1"},
        {"score": 0.93, "entity_name": "北京城区", "entity_alias": '["北区"]', "attribute_code": "r2"},
    ])
    result = await QuestionRewriter(searcher).rewrite(
        "查北区销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert result.rewritten_question == "查北区销售额"
    assert result.events == []


@pytest.mark.asyncio
async def test_rewrite_that_destroys_intent_term_is_rejected():
    searcher = FakeSearcher([{
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
        {"score": 0.99, "entity_name": "北京", "entity_alias": '["京城"]', "attribute_code": "r0"},
        {"score": 0.95, "entity_name": "华北大区", "entity_alias": '["北区"]', "attribute_code": "r1"},
        {"score": 0.93, "entity_name": "北京城区", "entity_alias": '["北区"]', "attribute_code": "r2"},
    ])
    result = await QuestionRewriter(searcher).rewrite(
        "查京城和北区销售额", previous=None, semantic_model_id=6, business_domain_id=9
    )
    assert "北京" in result.rewritten_question
    assert "北区" in result.rewritten_question


@pytest.mark.asyncio
async def test_alias_does_not_rewrite_appended_confirmed_context():
    previous = CanonicalAnalysisRequest(
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
