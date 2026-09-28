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


def test_canonical_name_view_preserves_execution_ast_and_discloses_binding():
    ast, repair = case()
    before = deepcopy((ast, repair))
    text = render_asl_extraction_json(ast, [repair])
    result = displayed(text)
    assert result["filters"] == [repair["previous_filter"]]
    assert "P001" not in text and "P002" not in text
    assert "标准名称展示视图" in text and "实际 SQL 保留已绑定的编码条件" in text
    assert "`product.product_name` → `sales_order.product_code`" in text
    assert "display_fields（展示字段）" in text
    assert (ast, repair) == before
    result["filters"][0]["value"] = "changed"
    assert (ast, repair) == before


@pytest.mark.parametrize("operator,values,bound_operator", [
    ("IN", ["标准商品甲", "标准商品乙"], "IN"),
    ("!=", "标准商品甲", "NOT IN"),
    ("NOT IN", ["标准商品甲", "标准商品乙"], "NOT IN"),
])
def test_names_preserve_original_set_and_exclusion_semantics(operator, values, bound_operator):
    ast, repair = case()
    repair["previous_filter"].update(operator=operator, value=values)
    ast["filters"][0]["operator"] = repair["resolved_filter"]["operator"] = bound_operator
    assert displayed(render_asl_extraction_json(ast, [repair]))["filters"] == [repair["previous_filter"]]


@pytest.mark.parametrize("problem", ["missing", "stale", "untrusted", "conflicting", "malformed", "bad_operator", "null_label"])
def test_missing_or_nonmatching_mapping_keeps_actual_codes_without_guessing(problem):
    ast, repair = case()
    repairs = [repair]
    if problem == "missing":
        repairs = None
    elif problem == "stale":
        repair["resolved_filter"]["value"] = ["DIFFERENT"]
    elif problem == "untrusted":
        repair["source"] = "USER_QUESTION"
    elif problem == "conflicting":
        other = deepcopy(repair)
        other["previous_filter"]["value"] = "不同的商品"
        repairs.append(other)
    elif problem == "bad_operator":
        repair["previous_filter"]["operator"] = []
    elif problem == "null_label":
        repair["previous_filter"]["value"] = None
    else:
        repair["previous_filter"] = {"value": "没有字段身份"}
    text = render_asl_extraction_json(ast, repairs)
    assert displayed(text) == ast
    assert "标准名称展示视图" not in text


def test_other_literals_and_filters_are_not_rewritten_or_deduplicated():
    ast, repair = case()
    literals = [{"field": "sales_order.amount_with_tax", "operator": ">", "value": 1000},
                {"field": "product.model", "operator": "=", "value": "M60 set"},
                {"field": "dealer.code", "operator": "=", "value": "00123"}]
    ast["filters"].extend(deepcopy(literals))
    text = render_asl_extraction_json(ast, [repair, deepcopy(repair)])
    assert displayed(text)["filters"] == [repair["previous_filter"], *literals]
    assert text.count("`product.product_name` → `sales_order.product_code`") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", [True, False])
async def test_both_adapter_paths_show_names_but_send_original_codes_to_sql(surface):
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
    assert displayed(parsing["message"])["filters"] == [repair["previous_filter"]]
    translated = json.loads(client.calls[1][2]["asl"])
    assert translated["filters"] == ast["filters"] == result.asl["filters"]
    assert next(i for i, e in enumerate(events) if e == parsing) < next(
        i for i, e in enumerate(events) if e["stage"] == "SEMANTIC_QUERY_PLANNING")
