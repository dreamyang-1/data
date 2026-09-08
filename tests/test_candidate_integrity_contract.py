"""Visible semantic options must retain their identity across clarification."""
import json
import hashlib

import pytest

from app.adapters.base import AdapterError
from pydantic import ValidationError

from app.domain.models import PendingState, SemanticAmbiguity
from app.services.clarification_policy import clarification_key, decide_clarification
from app.services.orchestrator import DataAnalysisOrchestrator
from test_semantic_choice_contract import (
    IDENTITY, AmbiguousRetrieval, ambiguity, chat, choose, pending, service,
)


def duplicate_payload():
    item = ambiguity().model_dump(mode="json")
    item["candidates"].insert(1, item["candidates"][0])
    item["candidate_details"].insert(1, dict(item["candidate_details"][0]))
    return item


def parse_options(payload):
    return DataAnalysisOrchestrator._semantic_ambiguities(
        AdapterError("ASL_AMBIGUOUS", "synthetic catalog ambiguity", details=payload)
    )


def test_duplicate_label_removal_keeps_selected_semantic_id_aligned():
    request = pending()
    request.semantic_ambiguities = parse_options([duplicate_payload()])
    assert request.semantic_ambiguities[0].candidates == ["含税销售额", "不含税销售额"]
    result = choose(request, "2")
    assert result.metrics[0].input == "不含税销售额"
    assert result.metrics[0].metric_id == "81:option_1"


class DuplicateOptionsRetrieval(AmbiguousRetrieval):
    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if not any(m.metric_id in {"81:option_0", "81:option_1"} for m in request.metrics):
            details = [duplicate_payload()]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        return await self.delegate.query(request, identity, **kwargs)


@pytest.mark.asyncio
async def test_visible_second_option_executes_second_catalog_candidate():
    retrieval = DuplicateOptionsRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    assert first.clarification_items[0].options == ["含税销售额", "不含税销售额"]
    second = await agent.handle(chat("2", "message-2"), IDENTITY)
    assert second.status == "COMPLETED"
    assert retrieval.requests[-1].metrics[0].metric_id == "81:option_1"
    assert [m.input for m in retrieval.requests[-1].metrics] == ["不含税销售额", "订单量"]


def same_labels_different_target(phrase, prefix):
    return SemanticAmbiguity(
        type="metric", phrase=phrase, ambiguity_id=prefix,
        question=f"请选择{phrase}的业务口径。", candidates=["实际口径", "预算口径"],
        candidate_details=[{
            "label": label, "canonical_name": phrase + suffix,
            "canonical_code": prefix + "_" + str(index), "version": "v1",
        } for index, (label, suffix) in enumerate([("实际口径", "实际值"), ("预算口径", "预算值")])],
        semantic_model_id=81, affected_slots=["metrics"],
    )


class SameLabelsRetrieval(AmbiguousRetrieval):
    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        if not any(m.input == "销售额实际值" for m in request.metrics):
            details = [same_labels_different_target("销售额", "sales").model_dump(mode="json"),
                       same_labels_different_target("订单量", "orders").model_dump(mode="json")]
            raise AdapterError("ASL_AMBIGUOUS", json.dumps(details, ensure_ascii=False), details=details)
        return await self.delegate.query(request, identity, **kwargs)


@pytest.mark.asyncio
async def test_distinct_target_with_same_option_labels_is_not_repeated_question():
    retrieval = SameLabelsRetrieval()
    agent = service(retrieval)
    first = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert first.status == "NEEDS_CLARIFICATION"
    count = len(retrieval.requests)
    second = await agent.handle(chat("1", "message-2"), IDENTITY)
    assert second.status == "NEEDS_CLARIFICATION"
    assert not second.clarification_decision_traces[0].already_asked
    assert "订单量" in second.clarification_questions[0]
    assert len(retrieval.requests) == count
    third = await agent.handle(chat("2", "message-3"), IDENTITY)
    assert third.status == "COMPLETED"
    assert [m.metric_id for m in retrieval.requests[-1].metrics] == ["81:sales_0", "81:orders_1"]


@pytest.mark.parametrize("position", [0, 1])
def test_paired_deduplication_is_idempotent_across_serialized_pending(position):
    raw = ambiguity().model_dump(mode="json")
    raw["candidates"].insert(position + 1, " " + raw["candidates"][position] + " ")
    raw["candidate_details"].insert(position + 1, dict(raw["candidate_details"][position]))
    item = SemanticAmbiguity.model_validate(raw)
    assert item.candidates == ["含税销售额", "不含税销售额"]
    assert [d["canonical_code"] for d in item.candidate_details] == ["option_0", "option_1"]
    request = pending()
    request.semantic_ambiguities = [item]
    state = PendingState(request=request, clarification_rounds=1, state_version=1)
    restored = PendingState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert choose(restored.request, "2").metrics[0].metric_id == "81:option_1"


def test_label_only_legacy_options_remain_label_only():
    item = SemanticAmbiguity(type="dimension", question="选择分组", candidates=["城市", "城市", "省份"])
    assert item.candidates == ["城市", "省份"]
    assert item.candidate_details == []


def test_trailing_missing_detail_is_padded_without_shifting_known_id():
    item = SemanticAmbiguity(type="metric", question="选择口径", candidates=["甲", "乙"],
                             candidate_details=[{"canonical_code": "a"}])
    assert item.candidate_details == [{"canonical_code": "a"}, {}]


@pytest.mark.parametrize("kind", ["conflict", "extra", "sparse", "mapping"])
def test_malformed_details_cannot_be_published_as_selectable_options(kind):
    raw = duplicate_payload()
    if kind == "conflict":
        raw["candidate_details"][1]["canonical_code"] = "different"
    elif kind == "extra":
        raw["candidate_details"].append({"canonical_code": "unpaired"})
    elif kind == "sparse":
        raw["candidate_details"][0] = None
    else:
        raw["candidate_details"] = {"canonical_code": "wrong-shape"}
    assert parse_options([raw]) == []
    # A malformed blocking choice cannot disappear beside another valid one.
    assert parse_options([ambiguity().model_dump(mode="json"), raw]) == []


def test_corrupt_legacy_pending_with_extra_details_fails_validation():
    request = pending()
    state = PendingState(request=request, clarification_rounds=1, state_version=1).model_dump(mode="json")
    state["request"]["semantic_ambiguities"][0]["candidate_details"].insert(1, {"canonical_code": "stale"})
    with pytest.raises(ValidationError):
        PendingState.model_validate(state)


class InvalidOptionsRetrieval(AmbiguousRetrieval):
    async def query(self, request, identity, **kwargs):
        self.requests.append(request.model_copy(deep=True))
        raw = duplicate_payload()
        raw["candidate_details"][1]["canonical_code"] = "conflicting"
        raise AdapterError("ASL_AMBIGUOUS", "synthetic malformed options", details=[raw])


@pytest.mark.asyncio
async def test_invalid_catalog_options_use_system_fallback_without_user_question():
    retrieval = InvalidOptionsRetrieval()
    agent = service(retrieval)
    response = await agent.handle(chat("查询2026年7月销售额和订单量"), IDENTITY)
    assert response.status == "SAFE_FALLBACK"
    assert not response.clarification_questions
    assert not response.clarification_items
    assert response.clarification_decision_traces[0].reason_type == "SYSTEM_FAILURE"
    assert await agent.sessions.get_pending(IDENTITY.tenant_id, IDENTITY.user_id, "choice-app", "choice-conversation") is None


def decision(request, keys=()):
    return decide_clarification(request, "semantic_ambiguity", source_stage="SLOT_MERGE", asked_keys=set(keys))


@pytest.mark.parametrize("change", ["score", "wording", "generated_id", "order", "deferred"])
def test_same_displayed_business_decision_is_still_suppressed(change):
    request = pending()
    key, trace = decision(request)
    assert trace.decision == "ASK"
    changed = request.model_copy(deep=True)
    item = changed.semantic_ambiguities[0]
    if change == "score":
        item.candidate_details[0]["score"] = 0.9123
    elif change == "wording":
        item.question = "请选择此次采用的口径。"
    elif change == "generated_id":
        item.ambiguity_id = "regenerated-transport-id"
    elif change == "order":
        item.candidates.reverse()
        item.candidate_details.reverse()
    else:
        changed.semantic_ambiguities.append(ambiguity("dimension", "区域"))
    new_key, repeated = decision(changed, [key])
    assert new_key == key
    assert repeated.already_asked and repeated.decision == "SUPPRESS"


@pytest.mark.parametrize("change", ["target", "role", "code", "model", "domain", "version"])
def test_different_business_decision_does_not_share_repetition_key(change):
    request = pending()
    key, _ = decision(request)
    changed = request.model_copy(deep=True)
    item = changed.semantic_ambiguities[0]
    if change == "target":
        item.phrase = "订单量"
    elif change == "role":
        item.type = "dimension"
    elif change == "code":
        item.candidate_details[0]["canonical_code"] = "different_metric"
    elif change == "model":
        item.semantic_model_id = 82
    elif change == "domain":
        item.candidate_details[0]["business_domain_id"] = 205
    else:
        item.semantic_model_version = "v2"
    new_key, new_trace = decision(changed, [key])
    assert new_key != key
    assert not new_trace.already_asked and new_trace.decision == "ASK"


def test_trace_contains_only_visible_candidate_hashes():
    request = pending()
    request.semantic_ambiguities.append(ambiguity("dimension", "区域"))
    _, trace = decision(request)
    assert len(trace.candidate_ids) == 2
    serialized = trace.model_dump_json()
    assert all(term not in serialized for term in ("销售额", "省份", "城市", "option_0"))


def test_record_only_candidate_identity_is_not_reduced_to_its_label():
    request = pending()
    request.semantic_ambiguities[0].candidate_details = [{"record_id": "a"}, {"record_id": "b"}]
    key, _ = decision(request)
    request.semantic_ambiguities[0].candidate_details[0]["record_id"] = "c"
    changed, trace = decision(request, [key])
    assert changed != key and not trace.already_asked


def old_key(request):
    options = {c for a in request.semantic_ambiguities if a.blocking for c in a.candidates}
    identities = ['option-' + hashlib.sha256(c.encode()).hexdigest()[:20] for c in options]
    return clarification_key("semantic_ambiguity", identities)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_first", [False, True])
async def test_old_pending_key_restores_only_the_question_that_was_displayed(answer_first):
    agent = service(AmbiguousRetrieval())
    request = pending()
    request.semantic_ambiguities = [same_labels_different_target("销售额", "sales"),
                                    same_labels_different_target("订单量", "orders")]
    request.ambiguities = [a.question for a in request.semantic_ambiguities]
    state = PendingState(request=request, clarification_rounds=1, state_version=1,
                         asked_clarification_keys=[old_key(request)])
    await agent.sessions.put_pending(state, expected_version=0)
    current = choose(request) if answer_first else request.model_copy(deep=True)
    current.pending_state_version = 1
    response = await agent._request_clarification(current, 2)
    if answer_first:
        assert response.status == "NEEDS_CLARIFICATION"
        assert "订单量" in response.clarification_questions[0]
        assert not response.clarification_decision_traces[0].already_asked
    else:
        assert response.status == "SAFE_FALLBACK"
        assert not response.clarification_questions
        assert response.clarification_decision_traces[0].already_asked


@pytest.mark.asyncio
async def test_nonblocking_note_does_not_replace_visible_blocking_choice():
    agent = service(AmbiguousRetrieval())
    request = pending()
    note = ambiguity("dimension", "区域")
    note.blocking = False
    request.semantic_ambiguities.insert(0, note)
    request.ambiguities = [a.question for a in request.semantic_ambiguities]
    response = await agent._request_clarification(request, 1)
    assert response.status == "NEEDS_CLARIFICATION"
    assert response.clarification_items[0].options == ["含税销售额", "不含税销售额"]
    result = choose(request, "2")
    assert result.metrics[0].metric_id == "81:option_1"
    assert result.dimensions == request.dimensions
