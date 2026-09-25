"""Rejected filter facts survive vector validation and the public API boundary."""
import json

import pytest
from fastapi import HTTPException

import agent
import api
from asl_contract import ASLValidationError


FILTERS = [
    {"field": "sales_order.province", "operator": "=", "value": "上海"},
    {"field": "manufacturer.parent_brand", "operator": "=", "value": "费森尤斯"},
    {"field": "sales_order.amount", "operator": "=", "value": 1000},
]


@pytest.mark.parametrize("index", range(3))
def test_api_identifies_only_the_rejected_predicate(monkeypatch, index):
    # Synthetic fixtures: not evidence of which predicate failed in production.
    knowledge = {"_vector_authorized_fields": [
        item["field"] for i, item in enumerate(FILTERS) if i != index
    ]}

    def generate(*_args, **_kwargs):
        agent._validate_vector_grounded_asl(json.dumps({"filters": FILTERS}), knowledge)

    monkeypatch.setattr(api, "main", generate)
    with pytest.raises(HTTPException) as caught:
        api.agent_query(api.QueryRequest(query="订单查询", semantic_model_id=81,
                                        business_domain_ids=[205]))
    assert caught.value.status_code == 502
    body = caught.value.detail
    assert body["code"] == "ASL_FILTER_INVALID"
    assert body["field"] == FILTERS[index]["field"]
    assert body["details"] == {
        "validation_stage": "vector_grounding", "failed_filter": FILTERS[index],
    }
    assert knowledge["_vector_authorized_fields"] == [
        item["field"] for i, item in enumerate(FILTERS) if i != index
    ]


@pytest.mark.parametrize("operator,value", [
    ("=", 0), ("=", False), (">", 1000), ("BETWEEN", [0, 1000]),
    ("IN", ["上海", "北京"]),
])
def test_public_error_preserves_literals_without_extra_model_metadata(operator, value):
    predicate = {"field": "order.amount", "operator": operator, "value": value}
    with pytest.raises(ASLValidationError) as caught:
        agent._validate_vector_grounded_asl(
            json.dumps({"filters": [{**predicate, "untrusted_metadata": "not public"}]}), {},
        )
    context = api._public_asl_validation_context(caught.value)
    assert context["details"]["failed_filter"] == predicate


def test_valid_filters_and_first_failure_behavior_are_unchanged():
    content = json.dumps({"filters": FILTERS})
    agent._validate_vector_grounded_asl(content, {
        "_vector_authorized_fields": [item["field"] for item in FILTERS],
    })
    with pytest.raises(ASLValidationError) as caught:
        agent._validate_vector_grounded_asl(content, {})
    assert caught.value.details["failed_filter"] == FILTERS[0]

