"""SPRINT-20260917-C-ASL-OVERRIDE-PROBES: override-boundary probes.

Offline probes proving where app/adapters/http.py rewrites the original
question text or overrides generated ASL (metrics/time/dimensions). They
call the real adapter and its real helper functions (no copied logic) and
capture outbound payloads with the existing StubClient pattern from
tests/test_http_adapters.py. Failure here is a valid delivery when the
observed behavior diverges from the audit expectation.
"""

import json
from datetime import date

import pytest

from app.adapters.base import AdapterError
from app.adapters.http import HttpDataRetrievalAdapter
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    PrimaryIntent,
    TimeRange,
    TrustedIdentity,
)

IDENTITY = TrustedIdentity(tenant_id="t1", user_id="u1")


class _CapturingClient:
    """Same capture contract as tests/test_http_adapters.py StubClient."""

    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    async def post(self, base_url, path, payload, **kwargs):
        self.calls.append((base_url, path, payload, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_detail_coverage_wording_is_rewritten_before_asl_generation():
    """Probe 1: original text is rewritten by a local rule, not the model."""
    request = CanonicalAnalysisRequest(
        conversation_id="asl-probe-detail-rewrite",
        tenant_id="t1",
        user_id="u1",
        original_question="查询经销商覆盖的明细",
        primary_intent=PrimaryIntent.DETAIL_QUERY,
        entity="经销商",
        fields=["经销商名称"],
    )
    semantic_query = "查询经销商覆盖的明细"

    rewritten = HttpDataRetrievalAdapter._detail_semantic_query(
        request, semantic_query
    )

    assert rewritten != semantic_query
    assert "覆盖到的经销商" in rewritten
    assert "明细必须返回字段" in rewritten


@pytest.mark.asyncio
async def test_trend_default_month_granularity_is_injected_as_mandatory():
    """Probe 2: a local default (no user wording, >=60d range) becomes a
    mandatory execution instruction inside the ASL request text."""
    request = CanonicalAnalysisRequest(
        conversation_id="asl-probe-default-granularity",
        tenant_id="t1",
        user_id="u1",
        original_question="分析销售额趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        time_range=TimeRange(
            start=date(2025, 1, 1), end_exclusive=date(2025, 4, 1)
        ),
    )
    client = _CapturingClient([
        {"success": True, "result": json.dumps({
            "version": "2.0",
            "metrics": [],
            "dimensions": [],
            "filters": [],
        })},
        {"success": True, "sql": "SELECT 1"},
        {"success": True, "sql": "SELECT 1", "data": [], "columns": [], "row_count": 0},
    ])

    try:
        await HttpDataRetrievalAdapter(
            Settings(adapter_mode="http"), client
        ).query(request, IDENTITY, semantic_model_id=81, business_domain_id=205)
    except AdapterError:
        # Downstream validators may reject the minimal ASL; the captured
        # outbound payload is the audit evidence regardless.
        pass

    assert client.calls, "ASL generation request was never sent"
    outbound_query = client.calls[0][2]["query"]
    assert "分析销售额趋势" in outbound_query
    assert "默认按月分组" in outbound_query


def test_model81_trend_repair_overrides_asl_time_with_default_granularity():
    """Probe 3: generated ASL dimensions/time/sort are overridden locally,
    including a default 'month' granularity inferred without user wording."""
    request = CanonicalAnalysisRequest(
        conversation_id="asl-probe-trend-repair",
        tenant_id="t1",
        user_id="u1",
        original_question="分析销售额趋势",
        primary_intent=PrimaryIntent.TREND_ANALYSIS,
        time_range=TimeRange(
            start=date(2025, 1, 1), end_exclusive=date(2025, 4, 1)
        ),
    )
    asl = {
        "metrics": [{"name": "sales_amount"}],
        "dimensions": [],
        "filters": [],
        "time_context": None,
        "sort": {"field": "sales_amount", "direction": "DESC"},
    }

    HttpDataRetrievalAdapter._repair_model81_trend_time(asl, request, 81)

    temporal = next(
        item for item in asl["dimensions"]
        if str(item.get("name") or "") == "transaction_date"
    )
    # No explicit grain wording exists in the question: 'month' is inferred.
    assert temporal["granularity"] == "month"
    assert asl["time_context"]["anchor"] == "sales_order.created_date"
    assert asl["sort"]["field"] == "transaction_date"
