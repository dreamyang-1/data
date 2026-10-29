"""Explicit metadata references still recommend relations after a column rename."""
import pytest

from app.services.semantic_recommend import SemanticRecommendService
from test_semantic_recommend import FakeReader, field


def make_service(comment="所属省份ID，关联dim_province.province_id", column="province_id1",
                 target_source=122, target_domain=307, target_type="bigint"):
    reader = FakeReader(
        entities=[
            {"id": "city", "name": "城市", "main_table_name": "dim_city",
             "data_source_id": 122, "business_domain_id": 307},
            {"id": "province", "name": "省份", "main_table_name": "dim_province",
             "data_source_id": target_source, "business_domain_id": target_domain},
        ],
        tables=[
            {"id": 1, "name": "dim_city", "data_source_id": 122},
            {"id": 2, "name": "dim_province", "data_source_id": target_source},
        ],
        fields={
            1: [field(11, "city_id", ftype="bigint"), field(12, column, comment, "bigint")],
            2: [field(21, "province_id", "省份ID", target_type)],
        },
    )
    return SemanticRecommendService(reader=reader, llm_enabled=False)


@pytest.mark.parametrize("source,target", [("city", "province"), ("province", "city")])
def test_renamed_foreign_key_detected_in_both_directions(source, target):
    service = make_service()
    detected = service.detect_relations(122, source, 307)
    assert len(detected) == 1
    assert detected[0]["otherEntityTypeId"] == target
    assert detected[0]["inferredCount"] == 1
    recommended = service.recommend_relations(122, source, target)
    assert len(recommended) == 1
    candidate = recommended[0]
    assert candidate["sourceEntityTypeId"] == "province"
    assert candidate["targetEntityTypeId"] == "city"
    assert candidate["cardinality"] == "1:N"
    assert candidate["fieldPairs"][0]["sourceColumnName"] == "province_id"
    assert candidate["fieldPairs"][0]["targetColumnName"] == "province_id1"


@pytest.mark.parametrize("comment", [
    "引用：`dim_province`.`province_id`", "references DIM_PROVINCE.PROVINCE_ID",
    "参照 dim_province.province_id", "外键关联 dim_province.province_id",
])
def test_explicit_reference_spelling(comment):
    assert len(make_service(comment).detect_relations(122, "city", 307)) == 1


@pytest.mark.parametrize("comment", [
    "所属省份ID", "关联dim_province.missing_id", "关联other_province.province_id",
    "不关联dim_province.province_id", "不直接引用dim_province.province_id",
    "does not reference dim_province.province_id",
])
def test_no_relation_without_a_positive_existing_reference(comment):
    assert make_service(comment).detect_relations(122, "city", 307) == []


@pytest.mark.parametrize("kwargs", [
    {"target_source": 999}, {"target_domain": 999}, {"target_type": "varchar"},
])
def test_reference_preserves_existing_scope_and_type_compatibility(kwargs):
    assert make_service(**kwargs).detect_relations(122, "city", 307) == []


def test_comment_and_existing_name_match_are_not_counted_twice():
    service = make_service(column="province_id")
    # Use a conventional table/key name that already matches the naming rules.
    service._reader._entities[1]["main_table_name"] = "province"
    service._reader._tables[1]["name"] = "province"
    service._reader._fields[1][1]["comment"] = "关联province.province_id"
    detected = service.detect_relations(122, "city", 307)
    assert len(detected) == 1
    assert detected[0]["inferredCount"] == 1
    assert len(service.recommend_relations(122, "city", "province")) == 1
