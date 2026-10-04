import asyncio
from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest

from app.adapters.base import AdapterError
from app.adapters.surface_asl import generate_surface_asl
from app.domain.semantic_scope import AuthorizedSemanticScope


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def response():
    return dict(success=True, semantic_model_id=81, business_domain_ids=[205],
                asl_validation="PASS", asl_contract=None,
                result={"metrics": [], "dimensions": [{"name": "hospital.name"}],
                        "filters": [], "time_context": None, "ambiguity": []},
                semantic_evidence={"semantic_model_id": 81,
                    "requested_business_domain_ids": [205],
                    "resolved_business_domain_ids": [205], "selected_metrics": []})


def run(client, **overrides):
    args = dict(completed_question="  请提供使用科室。 ",
                mentions=[{"text": "科室", "role_hint": "指标"}],
                authorized_scope=AuthorizedSemanticScope(semantic_model_id=81,
                    business_domain_ids=(205,), scope_mode="EXPLICIT_DOMAINS"),
                identity=object(), application_id="app", request_id="request")
    args.update(overrides)
    return asyncio.run(generate_surface_asl(client,
        SimpleNamespace(asl_generator_base_url="http://unused",
                        asl_generator_path="/agent/query",
                        asl_generation_timeout_seconds=60), **args))


def test_exact_question_advisory_roles_and_unmodified_asl():
    original = response()
    client = Client(deepcopy(original))
    result = run(client)
    payload = client.calls[0][0][2]
    assert payload["query"] == payload["retrieval_query"] == "  请提供使用科室。 "
    assert payload["surface_evidence"]["mentions"][0]["role_hint"] == "指标"
    assert "intent_asl_contract" not in payload
    assert "metric_ids" not in payload
    assert result["asl"] == original["result"]
    result["asl"]["metrics"].append({"name": "changed"})
    assert client.response == original
    assert len(client.calls) == 1  # no SQL/execution request


@pytest.mark.parametrize("change", [
    {"semantic_model_id": 82}, {"business_domain_ids": []},
    {"semantic_evidence": {}}, {"result": "not json"},
    {"result": []}, {"result": {"ambiguity": [{"question": "确认哪一项"}]}},
])
def test_rejects_unconfirmed_or_ambiguous_result(change):
    data = response()
    data.update(change)
    with pytest.raises(AdapterError):
        run(Client(data))


def test_missing_scope_rejected_before_network():
    client = Client(response())
    with pytest.raises(AdapterError):
        run(client, authorized_scope=None)
    assert client.calls == []


def test_governed_default_time_resolves_only_time_ambiguity_with_metric_anchor():
    data = response()
    data["result"]["metrics"] = [{"name": "sales_total_including_tax"}]
    data["result"]["ambiguity"] = [{
        "type": "time_anchor",
        "question": "正在销售缺少可执行的时间范围",
        "candidates": ["本月", "最近30天", "今年"],
    }]
    data["semantic_evidence"]["selected_metrics"] = [{
        "canonical_code": "sales_total_including_tax",
        "semantic_model_id": 81,
        "business_domain_id": 205,
        "time_anchor": "sales_order.created_date",
    }]

    result = run(
        Client(data),
        time_range=SimpleNamespace(
            start=date(2025, 9, 18),
            end_exclusive=date(2026, 9, 19),
        ),
    )

    assert result["asl"]["ambiguity"] == []
    assert result["asl"]["time_context"] == {
        "type": "range",
        "start": "2025-09-18",
        "end": "2026-09-18",
        "value": None,
        "unit": "day",
        "anchor": "sales_order.created_date",
    }


def test_empty_domains_stay_model_wide():
    data = response()
    data["business_domain_ids"] = []
    data["semantic_evidence"]["requested_business_domain_ids"] = []
    client = Client(data)
    run(client, authorized_scope=AuthorizedSemanticScope(
        semantic_model_id=81, scope_mode="MODEL_WIDE"))
    payload = client.calls[0][0][2]
    assert payload["business_domain_ids"] == []
    assert payload["business_domain_id"] is None


def test_model_wide_uses_unique_current_catalog_domain_for_execution():
    client = Client(response())
    run(
        client,
        authorized_scope=AuthorizedSemanticScope(
            semantic_model_id=81, scope_mode="MODEL_WIDE"
        ),
        resolved_business_domain_ids=(205,),
    )

    payload = client.calls[0][0][2]
    assert payload["business_domain_ids"] == [205]
    assert payload["business_domain_id"] == 205


def test_surface_plan_reaches_shared_translation_and_execution_without_asl_repair():
    import json
    from app.adapters.http import HttpDataRetrievalAdapter
    from app.config import Settings
    from app.domain.models import CanonicalAnalysisRequest, PrimaryIntent, TrustedIdentity

    scope = AuthorizedSemanticScope(semantic_model_id=81, scope_mode="MODEL_WIDE")
    request = CanonicalAnalysisRequest(conversation_id="surface", tenant_id="t", user_id="u",
        original_question="请提供使用科室", primary_intent=PrimaryIntent.DETAIL_QUERY,
        semantic_model_id=81, authorized_semantic_scope=scope)
    planned = response()
    planned["business_domain_ids"] = []
    planned["semantic_evidence"]["requested_business_domain_ids"] = []
    planned["result"]["projection_mode"] = "DISTINCT"

    class PipelineClient:
        def __init__(self):
            self.calls = []
            self.responses = iter([planned,
                {"success": True, "sql": "SELECT hospital_name FROM hospital"},
                {"success": True, "columns": ["hospital_name"],
                 "data": [{"hospital_name": "示例医院"}], "row_count": 1}])

        async def post(self, base, path, payload, **kwargs):
            self.calls.append((path, deepcopy(payload)))
            return next(self.responses)

    client = PipelineClient()
    adapter = HttpDataRetrievalAdapter(Settings(), client)
    result = asyncio.run(adapter.query_surface(request, TrustedIdentity(tenant_id="t", user_id="u"),
        mentions=[{"text": "科室", "role_hint": "指标"}]))
    assert len(client.calls) == 3
    assert client.calls[0][1]["query"] == request.original_question
    assert json.loads(client.calls[1][1]["asl"]) == planned["result"]
    assert result.asl == planned["result"]
    assert result.dataset.row_count == 1
    assert request.metrics == []
