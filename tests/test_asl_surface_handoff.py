"""Surface payload handoff 组装接口测试。

覆盖：原文精确保留、多个细粒度提取、错误角色原样保留为参考、空提取、
越界长度/数量、伪造身份/确认/权限字段拒绝、不修改传入对象。
"""
import copy

import pytest

from app.services.asl_surface_handoff import build_surface_asl_input


def test_null_role_hint_matches_oagnet_contract():
    payload = build_surface_asl_input("查询", [{"text": "医院", "role_hint": None}])
    assert payload["surface_evidence"]["mentions"][0]["role_hint"] is None


def test_non_string_extra_keys_raise_value_error():
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": "医院", 1: True, "scope": 81}])


def test_returns_exactly_three_contract_keys() -> None:
    payload = build_surface_asl_input("查询 华东区 销售额", [])
    assert set(payload) == {"query", "retrieval_query", "surface_evidence"}


def test_structured_extraction_is_passed_through_verbatim() -> None:
    extraction = {
        "意图": "明细查询",
        "业务域": ["医药销售域"],
        "实体": ["商品", "使用科室"],
        "指标": [],
        "维度": [],
        "展示字段": [{"entity": "科室", "field": "科室名称"}],
        "过滤条件": [{"field": "商品品牌", "op": "=", "value": ["百特"]}],
        "时间粒度": {"unit": None, "time_range": None},
        "排序": [],
        "限制": None,
        "输出要求": "默认输出表格",
    }
    payload = build_surface_asl_input("查询", [], structured_extraction=extraction)
    assert set(payload) == {
        "query", "retrieval_query", "surface_evidence", "structured_extraction",
    }
    # 原样透传且为深拷贝，后续修改不影响调用方对象。
    assert payload["structured_extraction"] == extraction
    assert payload["structured_extraction"] is not extraction


def test_structured_extraction_none_keeps_three_keys() -> None:
    payload = build_surface_asl_input("查询", [], structured_extraction=None)
    assert set(payload) == {"query", "retrieval_query", "surface_evidence"}


def test_structured_extraction_non_dict_raises() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [], structured_extraction="明细查询")


def test_completed_question_is_preserved_verbatim_in_both_fields() -> None:
    question = "  查询 华东区，2026年 Q2 的销售额（含税）？ "
    payload = build_surface_asl_input(question, [])
    assert payload["query"] == question
    assert payload["retrieval_query"] == question
    # Chinese, spaces, and punctuation must all survive untouched.
    assert "，" in payload["query"]
    assert payload["query"].startswith("  ")
    assert payload["query"].endswith(" ")


def test_multiple_fine_grained_mentions_are_kept_in_order() -> None:
    mentions = [
        {"text": "华东区", "role_hint": "dimension"},
        {"text": "销售额", "role_hint": "metric"},
        {"text": "2026年Q2"},
    ]
    payload = build_surface_asl_input("查询华东区销售额", mentions)
    evidence_mentions = payload["surface_evidence"]["mentions"]
    assert [item["text"] for item in evidence_mentions] == [
        "华东区", "销售额", "2026年Q2",
    ]
    assert evidence_mentions[0]["role_hint"] == "dimension"
    assert evidence_mentions[1]["role_hint"] == "metric"
    # role_hint is optional and must not be invented when absent.
    assert "role_hint" not in evidence_mentions[2]


def test_wrong_role_hint_stays_a_reference_not_a_confirmation() -> None:
    payload = build_surface_asl_input(
        "订单数按医院统计",
        [{"text": "医院", "role_hint": "metric"}],
    )
    mention = payload["surface_evidence"]["mentions"][0]
    assert mention == {"text": "医院", "role_hint": "metric"}
    # The assembled payload must not carry any confirmed/verified marker.
    assert "confirmed" not in str(payload)


def test_empty_mention_list_is_legal() -> None:
    payload = build_surface_asl_input("查询销售总额", [])
    assert payload["surface_evidence"] == {"mentions": []}


def test_question_over_length_limit_is_rejected_without_truncation() -> None:
    question = "销" * 4001
    with pytest.raises(ValueError):
        build_surface_asl_input(question, [])
    assert len(question) == 4001  # input untouched


def test_question_at_limit_is_accepted() -> None:
    payload = build_surface_asl_input("销" * 4000, [])
    assert len(payload["query"]) == 4000


@pytest.mark.parametrize("question", ["", "   ", "\t\n"])
def test_blank_question_is_rejected(question: str) -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input(question, [])


def test_mentions_over_count_limit_is_rejected() -> None:
    mentions = [{"text": f"m{i}"} for i in range(51)]
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", mentions)


def test_mention_text_over_length_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": "字" * 301}])


def test_empty_mention_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": ""}])


def test_role_hint_over_length_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": "华东", "role_hint": "r" * 81}])


@pytest.mark.parametrize("extra_key", ["field_id", "confirmed", "scope", "role"])
def test_extra_identity_or_confirmation_keys_are_rejected(extra_key: str) -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": "华东区", extra_key: "x"}])


def test_missing_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"role_hint": "dimension"}])


@pytest.mark.parametrize("bad_value", [123, None, ["华东"], {"text": "华东"}])
def test_non_string_text_is_rejected_without_conversion(bad_value) -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", [{"text": bad_value}])


def test_non_dict_mention_and_non_list_mentions_are_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", ["华东区"])
    with pytest.raises(ValueError):
        build_surface_asl_input("查询", {"text": "华东区"})


def test_non_string_question_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_surface_asl_input(12345, [])


def test_input_objects_are_not_mutated() -> None:
    mentions = [
        {"text": "华东区", "role_hint": "dimension"},
        {"text": "销售额"},
    ]
    snapshot = copy.deepcopy(mentions)
    question = "查询华东区销售额"
    payload = build_surface_asl_input(question, mentions)

    assert mentions == snapshot
    # Returned mentions are fresh copies, not shared references.
    payload["surface_evidence"]["mentions"][0]["text"] = "篡改"
    assert mentions[0]["text"] == "华东区"


def test_role_hint_is_not_normalized_or_mapped() -> None:
    # No business role mapping: whatever hint comes in is passed through as-is.
    payload = build_surface_asl_input(
        "查询", [{"text": "医院", "role_hint": "未知自定义提示"}]
    )
    assert payload["surface_evidence"]["mentions"][0]["role_hint"] == "未知自定义提示"
