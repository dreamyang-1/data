from prompt_build import SYSTEM_PROMPT, PromptBuilder
from vector_store import SearchResult


def _result(record_id: str, kind: str, **metadata) -> SearchResult:
    return SearchResult(
        id=record_id,
        score=float(metadata.pop("score", 0.9)),
        text=str(metadata.get("entity_name") or metadata.get("metric_name") or record_id),
        metadata={
            "type": kind,
            "semantic_model_id": 81,
            "business_domain_id": 205,
            **metadata,
        },
    )


def _entity(code: str, name: str, table: str) -> SearchResult:
    return _result(
        f"entity:{code}",
        "entity",
        entity_code=code,
        entity_name=name,
        attributes=[{
            "attr_code": f"{code}_code",
            "attr_name": f"{name}编码",
            "field_mapping": f"{table}.{code}_code",
            "is_primary_key": True,
        }, {
            "attr_code": f"{code}_secret",
            "attr_name": "内部字段",
            "field_mapping": f"{table}.secret",
        }],
        relations=[],
    )


def _relation(source: str, target: str, code: str) -> SearchResult:
    source_table = {
        "department": "department",
        "product_department_relation": "product_dept_relation",
        "product": "product",
        "dealer_product_relation": "dealer_product_relation",
        "dealer": "dealer",
    }[source]
    target_table = {
        "product_department_relation": "product_dept_relation",
        "product": "product",
        "dealer_product_relation": "dealer_product_relation",
        "dealer": "dealer",
        "dealer_profile": "dealer_result",
    }[target]
    return _result(
        f"relation:{code}",
        "relation",
        parent=source,
        relation_code=code,
        relation_name=code,
        target_entity=target,
        join_key={
            "source_field": f"{source_table}.{source}_code",
            "target_field": f"{target_table}.{source}_code",
        },
    )


def _where_type(where: dict) -> str:
    return next(
        str(item["type"])
        for item in where.get("$and", [where])
        if "type" in item
    )


class _Store:
    def __init__(self, direct: dict[str, list[SearchResult]], scoped: dict[str, list[SearchResult]]):
        self.direct = direct
        self.scoped = scoped

    def count(self) -> int:
        return 1

    def search(self, _vector, top_k: int, where: dict) -> list[SearchResult]:
        return list(self.direct.get(_where_type(where), []))[:top_k]

    def get_by_where(self, where: dict) -> list[SearchResult]:
        return list(self.scoped.get(_where_type(where), []))


def test_department_to_dealer_four_hop_path_and_metric_subject_are_completed():
    department = _entity("department", "科室", "department")
    product_department = _entity(
        "product_department_relation", "商品适用科室关系", "product_dept_relation"
    )
    product = _entity("product", "商品", "product")
    dealer_product = _entity(
        "dealer_product_relation", "经销商商品关系", "dealer_product_relation"
    )
    dealer = _entity("dealer", "经销商", "dealer")
    dealer_profile = _entity("dealer_profile", "经销商画像", "dealer_result")
    relations = [
        _relation(
            "department", "product_department_relation",
            "department_has_product_relation",
        ),
        _relation(
            "product_department_relation", "product",
            "product_department_relation_belongs_to_product",
        ),
        _relation(
            "product", "dealer_product_relation",
            "product_has_dealer_relation",
        ),
        _relation(
            "dealer_product_relation", "dealer",
            "dealer_product_relation_belongs_to_dealer",
        ),
        _relation("dealer", "dealer_profile", "dealer_has_profile"),
    ]
    metric = _result(
        "metric:dealer_recent_year_sales",
        "metric",
        metric_code="dealer_recent_year_sales",
        metric_name="经销商近一年销售额",
        source_dependency={"bind_entity": ["dealer_profile"]},
        calculation_rule={
            "calc_formula": "MAX(dealer_result.recent_year_amount)",
        },
    )
    store = _Store(
        direct={
            "entity": [department, dealer],
            "attribute": [],
            "relation": [],
            "metric": [metric],
            "dimension": [],
            "entity_attribute_value": [],
        },
        scoped={
            "entity": [
                department, product_department, product,
                dealer_product, dealer, dealer_profile,
            ],
            "relation": relations,
        },
    )
    builder = PromptBuilder(
        store,
        lambda _query: [0.1],
        top_k=4,
        semantic_model_id=81,
        business_domain_id=205,
        preferred_metric_codes=["dealer_recent_year_sales"],
    )

    knowledge = builder.retrieve(
        "产品适用于泌尿外科、肾脏内科，推荐上海经销商"
    )

    assert knowledge["_relational_completion"]["metric_anchor_entity_codes"] == [
        "dealer_profile"
    ]
    assert set(knowledge["_relational_completion"]["anchor_entity_codes"]) == {
        "department", "dealer", "dealer_profile",
    }
    assert {
        item.metadata["relation_code"] for item in knowledge["relations"]
    } == {item.metadata["relation_code"] for item in relations}
    assert {
        item.metadata["entity_code"] for item in knowledge["entities"]
    } == {
        "department", "product_department_relation", "product",
        "dealer_product_relation", "dealer", "dealer_profile",
    }
    completed = {
        item.metadata["entity_code"]: item for item in knowledge["entities"]
    }
    assert all(
        attribute["field_mapping"] != "product.secret"
        for attribute in completed["product"].metadata["attributes"]
    )


def test_registered_indirect_path_prompt_forbids_direct_relation_clarification():
    assert "已注册的间接关系可直接执行" in SYSTEM_PROMPT
    assert "不得因为两端没有直连关系而追问" in SYSTEM_PROMPT
    assert "仅保留某等级医院渠道" in SYSTEM_PROMPT
