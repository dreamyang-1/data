import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from api import _asl_validation_error_code, _candidate_scope_reason
from asl_contract import ASLValidationError
from capacity_control import (
    AslCapacityController,
    AslCapacityPolicy,
    AslGenerationDeadlineExceeded,
    AslUpstreamRateLimited,
)
from scope_contract import require_candidate_scope


def test_time_anchor_validation_has_specific_code() -> None:
    error = ValueError("ASL time_context anchor was not retrieved from semantic scope")
    assert _asl_validation_error_code(error) == "ASL_TIME_ANCHOR_INVALID"


def test_metric_validation_has_specific_code() -> None:
    error = ValueError("ASL omitted metrics explicitly named in the question")
    assert _asl_validation_error_code(error) == "ASL_METRIC_SELECTION_INVALID"


def test_empty_projection_is_not_misclassified_as_metric_conflict() -> None:
    error = ValueError(
        "ASL must contain a metric, a detail projection, or a clarification ambiguity"
    )
    assert _asl_validation_error_code(error) == "ASL_EMPTY_QUERY_PROJECTION"


def test_structured_detail_projection_error_keeps_its_exact_code() -> None:
    error = ASLValidationError(
        "ASL_DETAIL_PROJECTION_MISSING",
        "required detail projection is missing",
        field="dimensions",
    )
    assert _asl_validation_error_code(error) == "ASL_DETAIL_PROJECTION_MISSING"


def test_agent_query_returns_bounded_structured_validation_context() -> None:
    error = ASLValidationError(
        "ASL_ENTITY_MENTION_UNRESOLVED",
        "internal reason must stay private",
        field="filters",
        details={
            "mention": "费森尤斯",
            "candidate_field_count": 12,
            "token": "must-not-be-exposed",
        },
    )

    with patch("api.main", side_effect=error):
        response = TestClient(api.app).post(
            "/agent/query",
            json={"query": "查询费森尤斯产品", "semantic_model_id": 81},
        )

    assert response.status_code == 502
    payload = response.json()["detail"]
    assert payload["code"] == "ASL_ENTITY_MENTION_UNRESOLVED"
    assert payload["field"] == "filters"
    assert payload["details"] == {
        "mention": "费森尤斯",
        "candidate_field_count": 12,
    }
    assert "internal reason" not in response.text


def test_unknown_validation_keeps_generic_code() -> None:
    assert _asl_validation_error_code(ValueError("invalid envelope")) == "ASL_OUTPUT_INVALID"


def test_upstream_rate_limit_preserves_retry_after_contract() -> None:
    with patch.object(
        api._asl_capacity,
        "run",
        side_effect=AslUpstreamRateLimited(retry_after_seconds=7),
    ):
        response = TestClient(api.app).post(
            "/agent/query",
            json={"query": "query sales", "semantic_model_id": 6},
        )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.json() == {
        "success": False,
        "detail": {
            "code": "ASL_UPSTREAM_RATE_LIMITED",
            "message": "upstream ASL model is rate limited; retry later",
        },
    }


def test_agent_query_three_way_fanout_queues_model_calls() -> None:
    controller = AslCapacityController(
        AslCapacityPolicy(
            max_concurrency=1,
            queue_timeout_seconds=1,
            operation_timeout_seconds=0.2,
            request_deadline_seconds=2,
            max_waiters=2,
            max_rate_limit_retries=1,
        )
    )
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def model_call(*_args, **_kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.04)
            fingerprint = "sha256:" + ("a" * 64)
            return {
                "result": '{"version":"2.0"}',
                "semantic_evidence": {
                    "evidence_version": "1.0",
                    "producer": "OAGNET",
                    "semantic_model_id": 6,
                    "requested_business_domain_ids": [],
                    "resolved_business_domain_ids": [],
                    "selected_metrics": [],
                    "asl_signature": fingerprint,
                    "evidence_fingerprint": fingerprint,
                },
            }
        finally:
            with state_lock:
                active -= 1

    def request(index: int):
        return TestClient(api.app).post(
            "/agent/query",
            json={"query": f"query sales {index}", "semantic_model_id": 6},
        )

    with patch.object(api, "_asl_capacity", controller), patch(
        "api.main", side_effect=model_call
    ) as generated, ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(request, range(3)))

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert generated.call_count == 3
    assert max_active == 1


def test_agent_query_deadline_has_stable_gateway_timeout_code() -> None:
    with patch.object(
        api._asl_capacity,
        "run",
        side_effect=AslGenerationDeadlineExceeded("deadline"),
    ):
        response = TestClient(api.app).post(
            "/agent/query",
            json={"query": "query sales", "semantic_model_id": 6},
        )

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "ASL_GENERATION_DEADLINE_EXCEEDED"


def test_candidate_scope_reason_maps_each_internal_stage() -> None:
    cases = {
        "candidate metadata is missing": "metadata_missing",
        "candidate model is unproven or incompatible": "model_unproven",
        "candidate domain is unproven or incompatible": "domain_unproven",
        "unknown internal wording": "unverified",
    }
    for reason, stage in cases.items():
        assert _candidate_scope_reason(
            ValueError(f"SEMANTIC_SCOPE_MISMATCH: {reason}")
        ) == stage


def test_display_resolve_scope_mismatch_names_stage_and_candidate(monkeypatch) -> None:
    metadata = {
        "semantic_model_id": 82,
        "business_domain_id": 206,
        "metric_name": "revenue",
        "metric_code": "revenue",
    }
    monkeypatch.setattr(api, "embed_query", lambda text: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda *a, **kw: [SimpleNamespace(id="m", metadata=metadata, score=1)],
    )
    response = TestClient(api.app).post(
        "/vector/semantic-elements/resolve",
        json={
            "semantic_model_id": 81,
            "business_domain_ids": [205],
            "candidates": [{"candidate_id": "c1", "slot": "metric", "value": "revenue"}],
        },
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "SEMANTIC_SCOPE_MISMATCH"
    assert detail["stage"] == "model_unproven"
    assert detail["candidate"] == {"candidate_id": "c1", "slot": "metric"}
    # Internal wording and raw retrieval metadata must stay private.
    assert "unproven or incompatible" not in response.text
    assert "82" not in response.text


def test_display_resolve_domain_scope_mismatch_names_domain_stage(monkeypatch) -> None:
    metadata = {
        "semantic_model_id": 81,
        "business_domain_id": 206,
        "metric_name": "revenue",
        "metric_code": "revenue",
    }
    monkeypatch.setattr(api, "embed_query", lambda text: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda *a, **kw: [SimpleNamespace(id="m", metadata=metadata, score=1)],
    )
    response = TestClient(api.app).post(
        "/vector/semantic-elements/resolve",
        json={
            "semantic_model_id": 81,
            "business_domain_ids": [205],
            "candidates": [{"candidate_id": "c2", "slot": "metric", "value": "revenue"}],
        },
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "SEMANTIC_SCOPE_MISMATCH"
    assert detail["stage"] == "domain_unproven"
    assert detail["candidate"] == {"candidate_id": "c2", "slot": "metric"}


def test_display_resolve_unknown_scope_failure_stays_bounded(monkeypatch) -> None:
    metadata = {
        "semantic_model_id": 81,
        "business_domain_id": 205,
        "metric_name": "revenue",
        "metric_code": "revenue",
    }

    def broken(*_args, **_kwargs):
        raise ValueError("SEMANTIC_SCOPE_MISMATCH: totally unexpected wording")

    monkeypatch.setattr(api, "embed_query", lambda text: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda *a, **kw: [SimpleNamespace(id="m", metadata=metadata, score=1)],
    )
    monkeypatch.setattr(api, "require_candidate_scope", broken)
    response = TestClient(api.app).post(
        "/vector/semantic-elements/resolve",
        json={
            "semantic_model_id": 81,
            "business_domain_ids": [205],
            "candidates": [{"candidate_id": "c3", "slot": "metric", "value": "revenue"}],
        },
    )

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "SEMANTIC_SCOPE_MISMATCH"
    assert detail["stage"] == "unverified"
    assert "unexpected wording" not in response.text


def test_display_resolve_proven_candidate_still_resolves(monkeypatch) -> None:
    metadata = {
        "semantic_model_id": 81,
        "business_domain_id": 205,
        "metric_name": "revenue",
        "metric_code": "revenue",
    }
    monkeypatch.setattr(api, "embed_query", lambda text: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda *a, **kw: [SimpleNamespace(id="m", metadata=metadata, score=1)],
    )
    require_candidate_scope(metadata, 81, [205])
    response = TestClient(api.app).post(
        "/vector/semantic-elements/resolve",
        json={
            "semantic_model_id": 81,
            "business_domain_ids": [205],
            "candidates": [{"candidate_id": "ok", "slot": "metric", "value": "revenue"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["matches"][0]["candidate_id"] == "ok"
