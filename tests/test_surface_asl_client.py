import asyncio
from copy import deepcopy
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
    {"semantic_evidence": {}}, {"asl_validation": "FAIL"},
    {"asl_contract": {"metric_required": True}}, {"result": "not json"},
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
