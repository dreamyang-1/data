from types import SimpleNamespace

import pytest

from app.semantic_v2 import models as m
from app.semantic_v2.limited_scalar_runtime import _limited_scalar_payload
from app.semantic_v2.persisted_scalar_api import PersistedScalarApiHandler
from test_v2_asl2_lowering import provider
from test_v2_authorized_catalog_bridge import IDENTITY, request
from test_v2_persisted_scalar_api import NOW, planned_artifacts, prepared_for


class ProbeStore:
    namespace_mode = "ISOLATED_TEST"

    def __init__(self, state=None):
        self.state = state
        self.loads = 0

    async def load(self, context, state_identity):
        self.loads += 1
        return SimpleNamespace(
            state=self.state,
            plans=(),
            message=lambda _message_id: None,
        )

    async def idempotency_record(self, snapshot, message_id):
        return None


def test_non_scalar_payload_is_rejected_before_scalar_field_access():
    with pytest.raises(ValueError, match="EXECUTION_PAYLOAD_UNSUPPORTED"):
        _limited_scalar_payload(m.ChatPayload())


def probe_handler(provider, *, state=None):
    prepared = prepared_for(provider)
    store = ProbeStore(state)
    calls = []

    async def planner(chat, identity, restored, plans):
        calls.append((chat, restored, plans))
        raise ValueError("V2_COMPAT_PROBE_REACHED")

    async def transport(_request):
        raise AssertionError("compatibility probe must not execute a query")

    handler = PersistedScalarApiHandler(
        store=store,
        context_resolver=lambda chat, identity: prepared.plan.permission_requirement,
        planner=planner,
        transport=transport,
        clock=lambda: NOW,
    )
    return handler, store, calls


@pytest.mark.asyncio
async def test_equivalent_legacy_department_is_consumed_before_feature_guard(provider):
    handler, store, calls = probe_handler(provider)

    response = await handler.handle(request(department=" 205 "), IDENTITY)

    assert response.error_code == "V2_COMPAT_PROBE_REACHED"
    assert store.loads == 1
    assert len(calls) == 1 and calls[0][0].department == ""


@pytest.mark.asyncio
async def test_conflicting_legacy_department_fails_closed_before_state_or_planner(provider):
    handler, store, calls = probe_handler(provider)

    response = await handler.handle(request(department="205,206"), IDENTITY)

    assert response.error_code == "V2_BUSINESS_DOMAIN_CONFLICT"
    assert store.loads == 0 and calls == []


@pytest.mark.asyncio
async def test_empty_history_uses_normal_v2_path(provider):
    handler, store, calls = probe_handler(provider)

    response = await handler.handle(request(history=[]), IDENTITY)

    assert response.error_code == "V2_COMPAT_PROBE_REACHED"
    assert store.loads == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_legacy_history_is_ignored_only_with_restored_v2_state(provider):
    prepared = prepared_for(provider)
    state, _plan = planned_artifacts(prepared)
    handler, store, calls = probe_handler(provider, state=state)
    chat = request(history=[{"role": "user", "content": "legacy display history"}])

    response = await handler.handle(chat, IDENTITY)

    assert response.error_code == "V2_COMPAT_PROBE_REACHED"
    assert store.loads == 1 and len(calls) == 1
    assert calls[0][1] is state
    assert calls[0][0].history == []
    assert handler._fingerprint(chat, IDENTITY) == handler._fingerprint(
        chat.model_copy(update={"history": []}), IDENTITY
    )


@pytest.mark.asyncio
async def test_legacy_history_without_v2_state_fails_closed(provider):
    handler, store, calls = probe_handler(provider)

    response = await handler.handle(
        request(history=[{"role": "user", "content": "legacy display history"}]),
        IDENTITY,
    )

    assert response.error_code == "V2_HISTORY_STATE_REQUIRED"
    assert store.loads == 1 and calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feature",
    [
        {"tools": [{"name": "external", "url": "https://example.com/tool"}]},
        {"dataset_id": "dataset-1"},
    ],
)
async def test_other_limited_mode_features_remain_unsupported(provider, feature):
    handler, store, calls = probe_handler(provider)

    response = await handler.handle(request(**feature), IDENTITY)

    assert response.error_code == "EXECUTION_REQUEST_FEATURE_UNSUPPORTED"
    assert store.loads == 0 and calls == []
