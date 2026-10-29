"""Round 2.3: exact current entity instances cannot collapse to entity types."""
from copy import deepcopy

import pytest

from app.semantic_v2.canonical_execution_bridge import build_canonical_analysis_request
from app.semantic_v2.catalog_bridge import ScopedPlanSession
from app.semantic_v2.completed_question import build_completed_question_display
from app.semantic_v2.pipeline import AuthorizedLogicalPlan, CurrentTurnParser, CurrentTurnSemanticParse
from app.semantic_v2.recognition_client import RecognitionFailure
from app.semantic_v2.state_machine import ConversationState
from test_v2_raw_turn_recognition import (
    IDENTITY, binding, edit, parse, planner, publish, request, reseal, system, turns,
)
import catalog_value_sources
import mysql_tool as mysql
from test_catalog_value_sources import source


PRODUCT = "耐高压植入式给药装置及附件"
HOSPITAL = "上海市皮肤病医院"
PROVINCE = "四川省"


@pytest.fixture
def entity_catalog(monkeypatch):
    service, store, registry, redis, _, overrides = system()
    snapshot = __import__("test_v2_raw_turn_recognition").authority()
    document = snapshot["documents"][0]
    hospital = document["entities"][0]
    hospital["entity_alias"] = ["医疗机构", "合作医院"]
    hospital["attributes"].append(dict(attribute_id=1206, attr_code="hospital_name",
        attr_name="医院名称", is_main_attribute=True, field_mapping="hospitals.hospital_name"))
    snapshot["physical_catalog"]["tables"][0]["fields"].append(
        dict(field_id=3, field_name="hospital_name", table_id=1))
    document["entities"].extend([
        dict(entity_id=206, entity_code="product", entity_name="产品", entity_alias=["商品", "药品"],
            business_domain=205, attributes=[dict(attribute_id=1301, attr_code="product_name",
                attr_name="产品名称", is_main_attribute=True, field_mapping="products.product_name")], relations=[]),
        dict(entity_id=207, entity_code="province", entity_name="省份", entity_alias=["省"],
            business_domain=205, attributes=[dict(attribute_id=1401, attr_code="province_name",
                attr_name="省份名称", is_main_attribute=True, field_mapping="provinces.province_name")], relations=[]),
    ])
    document["dimensions"].append(dict(dim_code="hospital", dim_name="医院",
        bind_entities=[dict(entity="205", attr="1206", businessDomain="205")]))
    snapshot["physical_catalog"]["tables"].extend([
        dict(table_id=2, table_name="products", semantic_model_id=81, data_source_id=7,
            fields=[dict(field_id=20, field_name="product_name", table_id=2)]),
        dict(table_id=3, table_name="provinces", semantic_model_id=81, data_source_id=7,
            fields=[dict(field_id=30, field_name="province_name", table_id=3)]),
    ])
    rows = [
        source(vectorization=1),
        source(attribute_id=1206, attr_code="hospital_name", attr_name="医院名称",
            mapping_column="hospital_name", field_id=3, vectorization=1),
        source(entity_id=206, entity_code="product", attribute_id=1301, attr_code="product_name",
            attr_name="产品名称", mapping_table="products", mapping_column="product_name",
            table_id=2, field_id=20, vectorization=1),
        source(entity_id=207, entity_code="province", attribute_id=1401, attr_code="province_name",
            attr_name="省份名称", mapping_table="provinces", mapping_column="province_name",
            table_id=3, field_id=30, vectorization=1),
    ]

    def metadata(_sql, args=None):
        if args is None or len(args) == 2:
            return deepcopy(rows)
        return [deepcopy(row) for row in rows if str(row["attribute_id"]) == str(args[-1])]

    monkeypatch.setattr(mysql, "_query", metadata)
    snapshot["physical_catalog"]["entity_value_sources"] = (
        catalog_value_sources.capture_value_sources({
            "semantic_model_id": 81, "business_domain_ids": [205],
            "scope_mode": "EXPLICIT_DOMAINS",
        })
    )
    overrides[(81, (205,))] = reseal(snapshot)
    publish(service)
    business = {
        "city": [],
        "hospital_name": [HOSPITAL],
        "product_name": [PRODUCT],
        "province_name": [PROVINCE],
    }
    reads = []

    class Connection:
        def cursor(self): return self
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def execute(self, sql, params=None):
            if params is None:
                return
            column = next(name for name in business if "`" + name + "`" in sql)
            reads.append((column, params[0]))
            self.rows = (sorted(business[column])[:params[0]] if "MAX_EXECUTION_TIME" in sql else
                sorted(value for value in business[column]
                    if mysql.normalize_catalog_text(value) == params[0])[:params[1]])
        def fetchall(self): return [(value.encode("utf-8"),) for value in self.rows]
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(mysql.pymysql, "connect", lambda **_kwargs: Connection())
    return (service, store, registry, redis, overrides, business, reads, rows)


def entity_step(surface, entity_name, *, text=None):
    text = text or f"查询{surface}的销售额"
    parsed = parse(text, [(surface, "SUBJECT_ENTITY", "subject", "SET"),
                          ("销售额", "MEASURE", "metrics", "SET")], shape="SCALAR_AGGREGATE")

    def draft(context):
        return dict(payload_type="SCALAR_AGGREGATE", edits=[
            edit("subject", binding(context, entity_name, "SUBJECT_ENTITY", "m0", "ENTITY"), ids=("m0",)),
            edit("metrics", [binding(context, "销售额", "MEASURE", "m1")], ids=("m1",)),
        ])
    return text, parsed, draft


def filter_values(result):
    expression = result.plan["logical_plan"]["payload"]["filters"]
    if not expression:
        return []
    nodes = expression["children"] if expression["node_type"] == "BOOLEAN_GROUP" else [expression]
    return [node["value"]["ref"]["display_name"] for node in nodes
            if node["value"]["value_type"] == "ENTITY_REF"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,entity", [(PRODUCT, "产品"), (HOSPITAL, "医院"), (PROVINCE, "省份")])
async def test_exact_current_entity_instance_is_preserved_as_source_proved_filter(entity_catalog, surface, entity):
    step = entity_step(surface, entity)
    result = (await turns(planner(entity_catalog, [step])[0], [step]))[0]
    assert filter_values(result) == [surface]
    state = ConversationState.model_validate(result.next_state.payload)
    plan = AuthorizedLogicalPlan.model_validate(result.plan["logical_plan"])
    semantics = state.tasks[plan.task_id].versions[-1].semantics
    assert semantics.subject.display_name == entity
    assert filter_values(result) == [surface]
    assert any(proof.source == "VERIFIED_SOURCE_EXACT_LOOKUP"
               for proof in plan.permission_proofs if hasattr(proof, "source"))
    assert [(trace.slot_path, trace.target) for trace in result.edit_trace] == [
        ("filter_expression", "entity-instance:m0")]


@pytest.mark.asyncio
async def test_generic_grouped_hospital_remains_entity_type_not_source_value(entity_catalog):
    text = "按医院统计销售额"
    parsed = parse(text, [("医院", "SUBJECT_ENTITY", "subject", "SET"),
                          ("销售额", "MEASURE", "metrics", "SET")], shape="GROUPED_AGGREGATE")
    parsed["mentions"][0]["candidate_roles"].append("GROUP_BY")
    parsed["explicit_slot_mentions"]["dimensions"] = ["m0"]
    parsed["operation_markers"].append(dict(mention_id="m0", operation_hint="SET", slot_name="dimensions"))

    def draft(context):
        return dict(payload_type="GROUPED_AGGREGATE", edits=[
            edit("subject", binding(context, "医院", "SUBJECT_ENTITY", "m0", "ENTITY"), ids=("m0",)),
            edit("dimensions", [binding(context, "医院", "GROUP_BY", "m0", "DIMENSION")], ids=("m0",)),
            edit("metrics", [binding(context, "销售额", "MEASURE", "m1")], ids=("m1",)),
        ])
    step = (text, parsed, draft)
    result = (await turns(planner(entity_catalog, [step])[0], [step]))[0]
    assert result.plan["logical_plan"]["payload"]["filters"] is None
    assert not entity_catalog[6]


@pytest.mark.asyncio
async def test_grouping_subject_and_explicit_product_instance_remain_distinct(entity_catalog):
    text = f"按医院统计{PRODUCT}的销售额"
    parsed = parse(text, [("医院", "SUBJECT_ENTITY", "subject", "SET"),
                          (PRODUCT, "FILTER_VALUE", "filter_expression", "SET"),
                          ("销售额", "MEASURE", "metrics", "SET")], shape="GROUPED_AGGREGATE")
    parsed["mentions"][0]["candidate_roles"].append("GROUP_BY")
    parsed["explicit_slot_mentions"]["dimensions"] = ["m0"]
    parsed["operation_markers"].append(dict(mention_id="m0", operation_hint="SET", slot_name="dimensions"))

    def draft(context):
        field = binding(context, "产品名称", "FILTER_FIELD", "m1", "ATTRIBUTE")
        return dict(payload_type="GROUPED_AGGREGATE", source_value_requests=[dict(
            request_id="product", mention_id="m1", field_binding_handles=[field["binding_handle"]])], edits=[
            edit("subject", binding(context, "医院", "SUBJECT_ENTITY", "m0", "ENTITY"), ids=("m0",)),
            edit("dimensions", [binding(context, "医院", "GROUP_BY", "m0", "DIMENSION")], ids=("m0",)),
            edit("metrics", [binding(context, "销售额", "MEASURE", "m2")], ids=("m2",)),
            edit("filter_expression", dict(node_type="PREDICATE",
                field_ref={"value_field_request_id": "product"}, operator="EQ",
                value={"value_request_id": "product"}, source="USER_EXPLICIT", scope="CURRENT_TASK"),
                ids=("m1",)),
        ])
    step = (text, parsed, draft)
    result = (await turns(planner(entity_catalog, [step])[0], [step]))[0]
    payload = result.plan["logical_plan"]["payload"]
    assert payload["subject"]["display_name"] == "医院"
    assert payload["group_by"][0]["display_name"] == "医院"
    assert filter_values(result) == [PRODUCT]


@pytest.mark.asyncio
async def test_unproved_or_ambiguous_instance_fails_closed(entity_catalog):
    for surface, values, reason in [
        ("不存在的产品", [], "V2_EXPLICIT_ENTITY_INSTANCE_UNPROVEN"),
        ("甲-医院", ["甲—医院", "甲－医院"], "V2_EXPLICIT_ENTITY_INSTANCE_AMBIGUOUS"),
    ]:
        entity = "产品" if "产品" in surface else "医院"
        column = "product_name" if entity == "产品" else "hospital_name"
        entity_catalog[5][column] = values
        step = entity_step(surface, entity)
        with pytest.raises(RecognitionFailure, match=reason):
            await turns(planner(entity_catalog, [step])[0], [step])


@pytest.mark.asyncio
async def test_new_task_never_inherits_previous_instance(entity_catalog):
    first = entity_step(PRODUCT, "产品")
    engine, _ = planner(entity_catalog, [first])
    before = (await turns(engine, [first]))[0]
    second = entity_step("医院", "医院", text="切换话题：查询医院销售额")
    engine, _ = planner(entity_catalog, [second])
    result = await engine.run(request(question=second[0], message_id="new-turn"), IDENTITY,
        state=before.next_state, plans=(before.plan_state,))
    assert result.plan["logical_plan"]["task_id"] != before.plan["logical_plan"]["task_id"]
    assert result.plan["logical_plan"]["payload"]["filters"] is None


@pytest.mark.asyncio
async def test_wrong_slot_cannot_trigger_instance_preservation(entity_catalog):
    text = "查询耐高压植入式给药装置及附件"
    parsed = parse(text, [(PRODUCT, "SUBJECT_ENTITY", "metrics", "SET")], shape="METADATA_LOOKUP")

    def draft(context):
        return dict(payload_type="METADATA", edits=[
            edit("subject", binding(context, "产品", "SUBJECT_ENTITY", "m0", "ENTITY"), ids=("m0",)),
        ])
    step = (text, parsed, draft)
    with pytest.raises(RecognitionFailure):
        await turns(planner(entity_catalog, [step])[0], [step])


def test_non_current_evidence_cannot_trigger_instance_preservation(entity_catalog):
    current = ScopedPlanSession(request(question="查询产品", message_id="current"), IDENTITY, entity_catalog[0])
    parsed = CurrentTurnSemanticParse.model_validate(parse("查询产品", [
        ("产品", "SUBJECT_ENTITY", "subject", "SET")]))
    with pytest.raises(ValueError, match="current-turn"):
        CurrentTurnParser.parse(text="查询产品", turn_id="current", text_ref="current",
            parsed=parsed.model_copy(update={"mentions": [parsed.mentions[0].model_copy(
                update={"source_turn_id": "old"})]}))


@pytest.mark.asyncio
async def test_out_of_scope_source_receipt_cannot_publish_instance(entity_catalog, monkeypatch):
    pin = entity_catalog[0].pin(81, [205])
    original = pin.lookup_entity_values
    def out_of_scope(*args, **kwargs):
        receipt = original(*args, **kwargs)
        receipt["scope"] = dict(semantic_model_id=81, business_domain_ids=[206],
                                scope_mode="EXPLICIT_DOMAINS")
        return receipt
    monkeypatch.setattr(pin, "lookup_entity_values", out_of_scope)
    monkeypatch.setattr(entity_catalog[0], "pin", lambda *_args, **_kwargs: pin)
    step = entity_step(PRODUCT, "产品")
    with pytest.raises(ValueError, match="V2_SOURCE_VALUE_RECEIPT_MISMATCH"):
        await turns(planner(entity_catalog, [step])[0], [step])


@pytest.mark.asyncio
async def test_source_receipt_mismatch_stops_before_task_publication(entity_catalog, monkeypatch):
    pin = entity_catalog[0].pin(81, [205])
    original = pin.lookup_entity_values
    def mismatched(*args, **kwargs):
        return dict(original(*args, **kwargs), attribute_record_id="foreign")
    monkeypatch.setattr(pin, "lookup_entity_values", mismatched)
    monkeypatch.setattr(entity_catalog[0], "pin", lambda *_args, **_kwargs: pin)
    step = entity_step(PRODUCT, "产品")
    with pytest.raises(ValueError, match="V2_SOURCE_VALUE_RECEIPT_MISMATCH"):
        await turns(planner(entity_catalog, [step])[0], [step])


@pytest.mark.asyncio
async def test_completed_question_and_canonical_request_consume_the_same_instance(entity_catalog):
    step = entity_step(PRODUCT, "产品")
    result = (await turns(planner(entity_catalog, [step])[0], [step]))[0]
    plan = AuthorizedLogicalPlan.model_validate(result.plan["logical_plan"])
    next_state = ConversationState.model_validate(result.next_state.payload)
    display = build_completed_question_display(message_id="turn0", plan=plan,
        previous_state=None, next_state=next_state, context_trace=result.context_trace)
    canonical = build_canonical_analysis_request(chat=request(question=step[0], message_id="turn0"),
        identity=IDENTITY, plan=plan, display=display)
    assert PRODUCT in display.completed_question
    assert canonical.filters[0]["value"] == PRODUCT
    assert canonical.semantic_filter_bindings[0].canonical_value == PRODUCT
