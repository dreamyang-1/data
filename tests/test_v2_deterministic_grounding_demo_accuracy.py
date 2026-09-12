"""Demo sprint: bounded deterministic grounding before SemanticEdits."""
from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import Settings
from app.semantic_v2.deterministic_grounding import build_deterministic_grounding, _unique_handle
from app.semantic_v2.pipeline import CurrentTurnParser, CurrentTurnSemanticParse
from app.semantic_v2.recognition import RawTurnPlanner
from app.semantic_v2.recognition_client import RecognitionFailure, RecognitionModelClient
from app.semantic_v2.state_machine import ConversationState
from context_fixture_contract import fixture_proposal
from test_v2_entity_instance_preservation_round23 import (
    PRODUCT, HOSPITAL, PROVINCE, entity_catalog, filter_values,
)
from test_catalog_publication import publish, reseal
from test_catalog_value_sources import source
from test_v2_raw_turn_recognition import IDENTITY, binding, edit, parse, request
import catalog_value_sources


NOW = datetime.fromisoformat("2026-09-09T09:00:00+08:00")
DEALER = "国药控股演示经销商"


@pytest.fixture
def sprint_catalog(entity_catalog):
    service, _, _, _, _, business, _, rows = entity_catalog
    snapshot = deepcopy(entity_catalog[4][(81, (205,))])
    document = snapshot["documents"][0]
    document["metrics"][0]["time_caliber"] = {"time_anchor": "hospitals.created_date"}
    document["metrics"].extend([
        dict(metric_code="quantity", metric_name="销售数量", business_domain=205,
             formula="SUM(quantity)"),
        dict(metric_code="orders", metric_name="订单笔数", business_domain=205,
             formula="COUNT(order_id)"),
    ])
    document["entities"].append(dict(entity_id=208, entity_code="dealer",
        entity_name="经销商", entity_alias=["经销单位"], business_domain=205,
        attributes=[dict(attribute_id=1501, attr_code="dealer_name", attr_name="经销商名称",
            is_main_attribute=True, field_mapping="dealers.dealer_name")], relations=[]))
    document["entities"][0]["attributes"].append(dict(attribute_id=1207,
        attr_code="created_date", attr_name="创建日期", is_main_attribute=False,
        field_mapping="hospitals.created_date"))
    document["dimensions"].extend([
        dict(dim_code="product", dim_name="产品",
             bind_entities=[dict(entity="206", attr="1301", businessDomain="205")]),
        dict(dim_code="dealer", dim_name="经销商",
             bind_entities=[dict(entity="208", attr="1501", businessDomain="205")]),
    ])
    snapshot["physical_catalog"]["tables"].append(dict(table_id=4, table_name="dealers",
        semantic_model_id=81, data_source_id=7,
        fields=[dict(field_id=40, field_name="dealer_name", table_id=4)]))
    snapshot["physical_catalog"]["tables"][0]["fields"].append(
        dict(field_id=4, field_name="created_date", table_id=1))
    rows.append(source(entity_id=208, entity_code="dealer", attribute_id=1501,
        attr_code="dealer_name", attr_name="经销商名称", mapping_table="dealers",
        mapping_column="dealer_name", table_id=4, field_id=40, vectorization=1))
    rows.append(source(attribute_id=1207, attr_code="created_date", attr_name="创建日期",
        mapping_column="created_date", field_id=4, data_type="datetime", vectorization=0,
        is_main_attribute=0))
    business["dealer_name"] = [DEALER]
    snapshot["physical_catalog"]["entity_value_sources"] = catalog_value_sources.capture_value_sources({
        "semantic_model_id": 81, "business_domain_ids": [205], "scope_mode": "EXPLICIT_DOMAINS"})
    entity_catalog[4][(81, (205,))] = reseal(snapshot)
    publish(service, publication_id="deterministic-grounding-sprint")
    return entity_catalog


class CurrentTurnTransport:
    def __init__(self, steps):
        self.steps = steps
        self.current = 0
        self.current_calls = 0
        self.semantic_calls = 0

    def __call__(self, request_info):
        body = json.loads(request_info.content)
        context = json.loads(body["messages"][1]["content"])
        if "parse" in context:
            self.semantic_calls += 1
            step = self.steps[self.current - 1]
            draft = step[2](context) if callable(step[2]) else deepcopy(step[2])
            data = draft
        else:
            self.current_calls += 1
            question, parsed, _ = self.steps[self.current]
            assert context["question"] == question
            data = deepcopy(parsed)
            data["context_proposal"] = fixture_proposal(data, context)
            for mention in data.get("mentions", []):
                mention["source_turn_id"] = context["turn_id"]
            self.current += 1
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
            "message": {"content": json.dumps(data)}}]})


def engine(catalog, steps):
    transport = CurrentTurnTransport(steps)
    settings = Settings(_env_file=None, intent_model_base_url="https://model.invalid/v1",
        intent_model_api_key="test-only-key", intent_model_name="existing-configured-model",
        intent_model_max_retries=0)
    client = RecognitionModelClient(settings, httpx.MockTransport(transport))
    return RawTurnPlanner(client, catalog[0], clock=lambda: NOW), transport


def scalar_step(surface, entity, *, metric="销售额", text=None):
    text = text or f"查询{surface}的{metric}"
    parsed = parse(text, [(surface, "SUBJECT_ENTITY", "subject", "SET"),
                          (metric, "MEASURE", "metrics", "SET")],
                   shape="SCALAR_AGGREGATE")
    def fallback(context):
        return dict(payload_type="SCALAR_AGGREGATE", edits=[
            edit("subject", binding(context, entity, "SUBJECT_ENTITY", "m0", "ENTITY"), ids=("m0",)),
            edit("metrics", [binding(context, metric, "MEASURE", "m1")], ids=("m1",)),
        ])
    return text, parsed, fallback


async def run_steps(catalog, steps):
    planner, transport = engine(catalog, steps)
    state = None
    plans = {}
    results = []
    for index, step in enumerate(steps):
        result = await planner.run(request(question=step[0], message_id=f"ground-{index}"), IDENTITY,
            state=state, plans=tuple(plans.values()))
        state = result.next_state
        plan = result.plan["logical_plan"]
        plans[plan["task_id"]] = result.plan_state
        results.append(result)
    return results, transport


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,entity", [(PRODUCT, "产品"), (HOSPITAL, "医院"), (PROVINCE, "省份")])
async def test_exact_specific_entity_uses_source_proof_without_semantic_edits(sprint_catalog, surface, entity):
    result, transport = await run_steps(sprint_catalog, [scalar_step(surface, entity)])
    assert transport.semantic_calls == 0
    assert filter_values(result[0]) == [surface]
    assert result[0].plan["logical_plan"]["payload"]["subject"]["display_name"] == entity


@pytest.mark.asyncio
async def test_generic_grouped_hospital_stays_grouping_not_name_filter(sprint_catalog):
    text = "按医院统计销售额"
    parsed = parse(text, [("医院", "SUBJECT_ENTITY", "subject", "SET"),
                          ("销售额", "MEASURE", "metrics", "SET")], shape="GROUPED_AGGREGATE")
    parsed["mentions"][0]["candidate_roles"].append("GROUP_BY")
    parsed["explicit_slot_mentions"]["dimensions"] = ["m0"]
    parsed["operation_markers"].append(dict(mention_id="m0", operation_hint="SET", slot_name="dimensions"))
    result, transport = await run_steps(sprint_catalog, [(text, parsed, {"payload_type": "GROUPED_AGGREGATE"})])
    payload = result[0].plan["logical_plan"]["payload"]
    assert transport.semantic_calls == 0
    assert payload["filters"] is None
    assert payload["group_by"][0]["display_name"] == "医院"


@pytest.mark.asyncio
async def test_grouped_hospital_with_specific_product_preserves_both_roles(sprint_catalog):
    text = f"按医院统计{PRODUCT}的销售额"
    parsed = parse(text, [("医院", "SUBJECT_ENTITY", "subject", "SET"),
                          (PRODUCT, "FILTER_VALUE", "filter_expression", "SET"),
                          ("销售额", "MEASURE", "metrics", "SET")], shape="GROUPED_AGGREGATE")
    parsed["mentions"][0]["candidate_roles"].append("GROUP_BY")
    parsed["explicit_slot_mentions"]["dimensions"] = ["m0"]
    parsed["operation_markers"].append(dict(mention_id="m0", operation_hint="SET", slot_name="dimensions"))
    result, transport = await run_steps(sprint_catalog, [(text, parsed, {"payload_type": "GROUPED_AGGREGATE"})])
    payload = result[0].plan["logical_plan"]["payload"]
    assert transport.semantic_calls == 0
    assert payload["subject"]["display_name"] == "医院"
    assert payload["group_by"][0]["display_name"] == "医院"
    assert filter_values(result[0]) == [PRODUCT]


def ranking_step(target):
    text = f"销售额排名前5的{target}"
    parsed = parse(text, [("销售额", "MEASURE", "metrics", "SET"),
                          ("前5", "LIMIT", "ranking_spec", "SET"),
                          (target, "SUBJECT_ENTITY", "subject", "SET")], shape="RANKING")
    parsed["mentions"][2]["candidate_roles"].append("GROUP_BY")
    # The generated CurrentTurn schema presents every slot key. Empty
    # unsupported slots are absence, while any non-empty one must still make
    # the deterministic path decline.
    parsed["explicit_slot_mentions"].update({
        "projection_spec": [], "comparison_spec": [],
        "delivery_spec": [], "relationship_spec": [],
    })
    return text, parsed, {"payload_type": "RANKING"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["产品", "经销商"])
async def test_simple_top5_derives_catalog_grouping_and_skips_semantic_edits(sprint_catalog, target):
    result, transport = await run_steps(sprint_catalog, [ranking_step(target)])
    payload = result[0].plan["logical_plan"]["payload"]
    assert transport.semantic_calls == 0
    assert payload["ranking"]["limit"] == 5
    assert payload["ranking"]["rank_by"]["display_name"] == "销售额"
    assert payload["group_by"][0]["display_name"] == target


def test_unique_longest_governed_metric_term_without_fuzzy_matching():
    candidates = [
        dict(binding_handle="included", mention_id="m1", role="MEASURE",
             catalog_type="METRIC", name="含税销售总额", code="included",
             aliases=["销售额"]),
        dict(binding_handle="excluded", mention_id="m1", role="MEASURE",
             catalog_type="METRIC", name="不含税销售总额", code="excluded",
             aliases=["不含税销售额"]),
    ]
    assert _unique_handle(candidates, "m1", "MEASURE", "含税销售额",
                          catalog_type="METRIC") == "included"
    assert _unique_handle(candidates, "m1", "MEASURE", "不含税销售额",
                          catalog_type="METRIC") == "excluded"
    assert _unique_handle(candidates, "m1", "MEASURE", "收入金额",
                          catalog_type="METRIC") is None


def test_entity_term_containment_is_not_a_deterministic_identity_match():
    candidates = [dict(binding_handle="product", mention_id="m1", role="SUBJECT_ENTITY",
        catalog_type="ENTITY", name="商品", code="product", aliases=["货品"])]
    assert _unique_handle(candidates, "m1", "SUBJECT_ENTITY", "某个商品",
                          catalog_type="ENTITY") is None


@pytest.mark.asyncio
async def test_explicit_new_task_does_not_inherit_prior_specific_filter(sprint_catalog):
    first = scalar_step(PRODUCT, "产品")
    second = scalar_step("医院", "医院", text="切换话题：查询医院销售额")
    second[1]["topic_shift_signals"] = ["EXPLICIT_NEW_TASK"]
    results, transport = await run_steps(sprint_catalog, [first, second])
    assert transport.semantic_calls == 0
    assert results[0].plan["logical_plan"]["task_id"] != results[1].plan["logical_plan"]["task_id"]
    assert results[1].plan["logical_plan"]["payload"]["filters"] is None


@pytest.mark.asyncio
async def test_missing_exact_proof_falls_back_without_publishing_broadened_task(sprint_catalog):
    sprint_catalog[5]["product_name"] = []
    step = scalar_step("不存在的具体产品", "产品")
    planner, transport = engine(sprint_catalog, [step])
    with pytest.raises(RecognitionFailure, match="V2_EXPLICIT_ENTITY_INSTANCE_UNPROVEN"):
        await planner.run(request(question=step[0]), IDENTITY)
    assert transport.semantic_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_normalized_value_falls_back_without_fast_path(sprint_catalog):
    surface = "甲-医院"
    sprint_catalog[5]["hospital_name"] = ["甲—医院", "甲－医院"]
    step = scalar_step(surface, "医院")
    planner, transport = engine(sprint_catalog, [step])
    with pytest.raises(RecognitionFailure, match="V2_EXPLICIT_ENTITY_INSTANCE_AMBIGUOUS"):
        await planner.run(request(question=step[0]), IDENTITY)
    assert transport.semantic_calls == 1


@pytest.mark.asyncio
async def test_specific_entity_replacement_falls_back_instead_of_retaining_old_filter(sprint_catalog):
    first = scalar_step(PRODUCT, "产品")
    text = "那改成另一个具体产品呢"
    parsed = parse(text, [("另一个具体产品", "SUBJECT_ENTITY", "subject", "REPLACE")],
        follow=True, shape="SCALAR_AGGREGATE")
    second = (text, parsed, {"payload_type": "INHERIT"})
    planner, transport = engine(sprint_catalog, [first, second])
    first_result = await planner.run(request(question=first[0], message_id="replace-0"), IDENTITY)
    with pytest.raises(RecognitionFailure):
        await planner.run(request(question=text, message_id="replace-1"), IDENTITY,
            state=first_result.next_state, plans=(first_result.plan_state,))
    assert transport.semantic_calls == 1


@pytest.mark.asyncio
async def test_relationship_query_never_uses_deterministic_fast_path(sprint_catalog):
    text = "查询产品合作的医院名单"
    parsed = parse(text, [("产品", "SUBJECT_ENTITY", "subject", "SET"),
                          ("合作", "RELATIONSHIP", "relationship_spec", "SET")], shape="RELATION_LIST")
    step = (text, parsed, {"payload_type": "RELATION_LIST"})
    planner, transport = engine(sprint_catalog, [step])
    with pytest.raises(RecognitionFailure):
        await planner.run(request(question=text), IDENTITY)
    assert transport.semantic_calls == 1


@pytest.mark.asyncio
async def test_nonempty_unsupported_slot_still_uses_semantic_edits(sprint_catalog):
    text, parsed, draft = ranking_step("经销商")
    parsed["explicit_slot_mentions"]["projection_spec"] = ["m2"]
    planner, transport = engine(sprint_catalog, [(text, parsed, draft)])
    with pytest.raises(RecognitionFailure):
        await planner.run(request(question=text), IDENTITY)
    assert transport.semantic_calls == 1


def test_active_pending_never_uses_deterministic_fast_path():
    text = "销售额"
    parsed = CurrentTurnSemanticParse.model_validate(parse(text, [
        ("销售额", "MEASURE", "metrics", "SET")], shape="SCALAR_AGGREGATE"))
    current = SimpleNamespace(pending=object(), tasks={})
    assert build_deterministic_grounding(session=None, parse=parsed, candidates=[], handles={},
        context_trace={"FINAL_RELATION": "NEW_TASK"}, current=current,
        pending=object(), now=NOW) is None


@pytest.mark.asyncio
async def test_time_followup_and_metric_add_keep_existing_task(sprint_catalog):
    first = ("查询去年销售额", parse("查询去年销售额", [
        ("去年", "TIME_RANGE", "time_spec", "SET"),
        ("销售额", "MEASURE", "metrics", "SET")], shape="SCALAR_AGGREGATE"),
        {"payload_type": "SCALAR_AGGREGATE"})
    second = ("换今年", parse("换今年", [("今年", "TIME_RANGE", "time_spec", "REPLACE")],
        follow=True, shape="SCALAR_AGGREGATE"), {"payload_type": "INHERIT"})
    third = ("再加销售数量", parse("再加销售数量", [
        ("销售数量", "MEASURE", "metrics", "ADD")], follow=True,
        shape="SCALAR_AGGREGATE"), {"payload_type": "INHERIT"})
    results, transport = await run_steps(sprint_catalog, [first, second, third])
    assert transport.semantic_calls == 0
    assert len({item.plan["logical_plan"]["task_id"] for item in results}) == 1
    assert {row["canonical_code"] for row in results[-1].plan["logical_plan"]["payload"]["measures"]} == {
        "amount", "quantity"}
    assert results[0].plan["logical_plan"]["payload"]["time"]["range"] != \
        results[1].plan["logical_plan"]["payload"]["time"]["range"]
