import pytest

from app.analysis.visualization import build_chart_specs
from app.domain.models import ChatRequest, PrimaryIntent, TrustedIdentity
from test_analysis_orchestration import RetrievalStub, service


ROWS = [
    {"月份": month, "城市": city, "销售额": amount}
    for month, city, amount in [
        ("2026-01", "甲市", 100), ("2026-01", "乙市", 200),
        ("2026-02", "甲市", 120), ("2026-02", "乙市", 230),
    ]
]


def test_city_month_chart_preserves_series_and_rejects_missing_group():
    chart = build_chart_specs(PrimaryIntent.TREND_ANALYSIS,
        ["月份", "城市", "销售额"], ROWS, {"metric_column": "销售额"})[0]
    assert chart.series_field == "城市"
    assert len(chart.data) == 4
    assert build_chart_specs(PrimaryIntent.TREND_ANALYSIS,
        ["月份", "销售额"], ROWS, {"metric_column": "销售额"}) == []


@pytest.mark.asyncio
async def test_grouped_trend_keeps_city_month_table(monkeypatch):
    original = RetrievalStub.query

    async def query(self, request, identity, **kwargs):
        result = await original(self, request, identity, **kwargs)
        result.dataset.columns = ["月份", "城市", "销售额"]
        result.dataset.rows = ROWS
        result.dataset.row_count = result.dataset.total_row_count = len(ROWS)
        return result

    monkeypatch.setattr(RetrievalStub, "query", query)
    result = await service().handle(ChatRequest(
        application_id="app", conversation_id="city-month", message_id="m1",
        question="分析2026年1月到2月各城市销售额趋势", semantic_model_id=1,
        business_domain_id=1,
    ), TrustedIdentity(tenant_id="tenant", user_id="user"))
    assert result.status == "COMPLETED"
    assert "| 月份 | 城市 | 销售额 |" in result.answer
    for row in ROWS:
        assert f"| {row['月份']} | {row['城市']} | {row['销售额']} |" in result.answer
