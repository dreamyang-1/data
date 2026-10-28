from copy import deepcopy
import json

import pytest

from app.presentation.intent_recognition import render_asl_extraction_json


def case():
    original = {"field": "product.product_name", "operator": "=", "value": "空心纤维血液透析器"}
    bound = {"field": "sales_order.product_code", "operator": "IN", "value": ["P001", "P002"]}
    ast = {"version": "2.0", "intent": "query", "subject": {"entity": "dealer"},
           "metrics": [], "dimensions": [{"name": "dealer.dealer_name"}],
           "filters": [deepcopy(bound)], "time_context": None, "ambiguity": []}
    repair = {"type": "RESOLVE_FILTER_BUSINESS_OWNER", "source": "SCOPED_RELATION_AND_DICTIONARY",
              "previous_filter": original, "resolved_filter": bound}
    return ast, repair


def displayed(text):
    return json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])


@pytest.mark.parametrize("predicate", [
    {"field": "hospital.province_id", "operator": "=", "value": "310000"},
    {"field": "sales_order.product_code", "operator": "IN", "value": ["P001", "P002"]},
    {"field": "sales_order.product_code", "operator": "NOT IN", "value": ["P001"]},
    {"field": "dealer.code", "operator": "!=", "value": "00123"},
    {"field": "sales_order.amount_with_tax", "operator": ">", "value": 1000},
])
def test_display_preserves_exact_execution_asl(predicate):
    ast, _ = case()
    ast["filters"] = [deepcopy(predicate), deepcopy(predicate)]
    before = deepcopy(ast)
    text = render_asl_extraction_json(ast)
    assert displayed(text) == before
    assert ast == before
    assert "????????" not in text
    assert "??????" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [True, False])
async def test_both_adapter_paths_display_exact_asl_sent_to_sql(surface):
    from app.adapters.http import HttpDataRetrievalAdapter
    from app.config import Settings
    from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, TrustedIdentity
    from app.domain.semantic_scope import AuthorizedSemanticScope
    from app.services.progress import progress_scope
    from test_http_adapters import StubClient
    from test_surface_asl_client import response

    ast, repair = case()
    generated = response()
    generated.update(result=ast, asl_repair=[repair])
    scope = AuthorizedSemanticScope(semantic_model_id=81, business_domain_ids=(205,), scope_mode="EXPLICIT_DOMAINS")
    request = CanonicalAnalysisRequest(conversation_id="name-display", tenant_id="t1", user_id="u1",
        original_question="查询空心纤维血液透析器产品合作的经销商名单", primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=81, business_domain_ids=[205], authorized_semantic_scope=scope,
        entity="dealer", fields=["dealer.dealer_name"])
    class ScopedClient(StubClient):
        async def post(self, base_url, path, payload, **kwargs):
            result = deepcopy(await super().post(base_url, path, payload, **kwargs))
            if path == "/agent/query":
                result["asl_contract"] = payload.get("intent_asl_contract")
            else:
                result.update(scope_contract_version="single-domain-v1", semantic_model_id=81,
                              business_domain_ids=[205], authorized_scope_fingerprint=scope.fingerprint())
            return result

    client = ScopedClient([generated, {"success": True, "sql": "SELECT dealer_name FROM dealer"},
                           {"success": True, "columns": ["dealer_name"], "data": [{"dealer_name": "示例经销商"}], "row_count": 1}])
    adapter = HttpDataRetrievalAdapter(Settings(adapter_mode="http"), client)
    identity = TrustedIdentity(tenant_id="t1", user_id="u1")
    events = []

    async def capture(event):
        events.append(deepcopy(event))

    with progress_scope(capture):
        if surface:
            result = await adapter.query_surface(request, identity, mentions=[], structured_extraction={})
        else:
            result = await adapter.query(request, identity, semantic_model_id=81, business_domain_id=205)
    parsing = next(e for e in events if e["stage"] == "ASL_GENERATION" and "```json" in e["message"])
    translated = json.loads(client.calls[1][2]["asl"])
    assert translated == displayed(parsing["message"]) == result.asl
    assert displayed(parsing["message"])["dimensions"] == ast["dimensions"]
    assert translated["dimensions"] == result.asl["dimensions"] == ast["dimensions"]
    assert "dimensions / display_fields" not in translated
    assert next(i for i, e in enumerate(events) if e == parsing) < next(
        i for i, e in enumerate(events) if e["stage"] == "SEMANTIC_QUERY_PLANNING")
