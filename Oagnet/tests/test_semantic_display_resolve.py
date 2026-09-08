from types import SimpleNamespace

import api


def test_semantic_display_resolver_returns_only_exact_vector_catalog_matches(monkeypatch):
    records = {
        "销售额": [
            SimpleNamespace(
                id="metric-1",
                score=0.91,
                metadata={
                    "type": "metric",
                    "metric_code": "annual_total_sales",
                    "metric_name": "含税销售总额",
                    "synonyms": ["销售额"],
                },
            )
        ],
        "经销商": [
            SimpleNamespace(
                id="entity-1",
                score=0.94,
                metadata={
                    "type": "entity",
                    "entity_code": "dealer",
                    "entity_name": "经销商",
                },
            )
        ],
        "费森尤斯": [
            SimpleNamespace(
                id="value-1",
                score=0.96,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "product_brand",
                    "attr_name": "商品品牌",
                    "attr_value": "费森尤斯医疗用品股份有限公司",
                    "entity_name": "产品",
                },
            )
        ],
        "总共": [
            SimpleNamespace(
                id="near-but-not-exact",
                score=0.99,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "product_name",
                    "attr_name": "商品名称",
                    "attr_value": "某产品",
                    "entity_name": "产品",
                },
            )
        ],
    }
    monkeypatch.setattr(api, "embed_query", lambda value: value)
    monkeypatch.setattr(
        api._store,
        "search",
        lambda vector, *, top_k, where: records.get(vector, []),
    )
    request = api.SemanticDisplayResolveRequest(
        semantic_model_id=81,
        business_domain_ids=[1],
        candidates=[
            {"candidate_id": "metric:0", "slot": "metric", "value": "销售额"},
            {"candidate_id": "entity:0", "slot": "entity", "value": "经销商"},
            {"candidate_id": "filter:0:0", "slot": "filter", "value": "费森尤斯"},
            {"candidate_id": "filter:1:0", "slot": "filter", "value": "总共"},
        ],
    )

    response = api.semantic_display_elements_resolve(request)

    assert [item.candidate_id for item in response.matches] == [
        "metric:0", "entity:0", "filter:0:0",
    ]
    assert response.matches[0].canonical_name == "含税销售总额"
    assert response.matches[1].canonical_name == "经销商"
    assert response.matches[2].canonical_value == "费森尤斯医疗用品股份有限公司"
    # Similarity by itself is not enough: the high-scoring unrelated value is
    # not allowed into visible diagnostics.
    assert all(item.input_value != "总共" for item in response.matches)


def test_metric_context_resolves_registered_long_name_without_accepting_generic_suffix(
    monkeypatch,
):
    metric = SimpleNamespace(
        id="metric-coverage",
        score=0.93,
        metadata={
            "type": "metric",
            "metric_code": "screening_area_hospital_coverage",
            "metric_name": "区域医院覆盖率",
            "synonyms": ["经销商医院覆盖率"],
        },
    )
    shorter_nested_metric = SimpleNamespace(
        id="metric-covered-hospitals",
        score=0.96,
        metadata={
            "type": "metric",
            "metric_code": "cooperating_hospital_count",
            "metric_name": "已合作医院数",
            "synonyms": ["医院覆盖"],
        },
    )
    monkeypatch.setattr(api, "embed_query", lambda value: value)
    monkeypatch.setattr(
        api._store,
        "search",
        lambda _vector, *, top_k, where: [shorter_nested_metric, metric],
    )
    request = api.SemanticDisplayResolveRequest(
        semantic_model_id=81,
        business_domain_ids=[205],
        candidates=[
            {"candidate_id": "metric:0", "slot": "metric", "value": "覆盖率"},
            {
                "candidate_id": "metric:0:context",
                "slot": "metric",
                "value": "统计上海市各个经销商的区域医院覆盖率",
            },
        ],
    )

    response = api.semantic_display_elements_resolve(request)

    assert [item.candidate_id for item in response.matches] == [
        "metric:0:context",
    ]
    assert response.matches[0].canonical_name == "区域医院覆盖率"
    assert response.matches[0].canonical_code == "screening_area_hospital_coverage"


def test_semantic_display_resolver_returns_exact_canonical_filter_value(monkeypatch):
    monkeypatch.setattr(api, "embed_query", lambda value: value)
    monkeypatch.setattr(api._store, "find_exact", lambda _where: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda _vector, *, top_k, where: [
            SimpleNamespace(
                id="value-1",
                score=0.96,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "product_brand",
                    "attr_name": "商品品牌",
                    "attr_value": "费森尤斯",
                    "entity_name": "产品",
                },
            )
        ],
    )
    request = api.SemanticDisplayResolveRequest(
        semantic_model_id=81,
        candidates=[
            {"candidate_id": "filter:0:0", "slot": "filter", "value": "费森尤斯"},
        ],
    )

    response = api.semantic_display_elements_resolve(request)

    assert len(response.matches) == 1
    assert response.matches[0].canonical_name == "商品品牌"
    assert response.matches[0].canonical_value == "费森尤斯"


def test_semantic_display_filter_value_is_scoped_to_its_source_field(monkeypatch):
    monkeypatch.setattr(api, "embed_query", lambda value: value)
    monkeypatch.setattr(api._store, "find_exact", lambda _where: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda _vector, *, top_k, where: [
            SimpleNamespace(
                id="specification-1",
                score=0.99,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "specification",
                    "attr_name": "规格型号",
                    "attr_value": "1",
                    "entity_name": "商品",
                },
            ),
            SimpleNamespace(
                id="relation-type-1",
                score=0.90,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "relation_type",
                    "attr_name": "适用科室类型",
                    "attr_value": "1",
                    "entity_name": "商品适用科室关系",
                },
            ),
        ],
    )
    request = api.SemanticDisplayResolveRequest(
        semantic_model_id=81,
        candidates=[{
            "candidate_id": "filter:0:0",
            "slot": "filter",
            "value": "1",
            "field_name": "适用科室类型",
        }],
    )

    response = api.semantic_display_elements_resolve(request)

    assert len(response.matches) == 1
    assert response.matches[0].canonical_name == "适用科室类型"
    assert response.matches[0].canonical_code == "relation_type"


def test_semantic_display_filter_field_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(api, "embed_query", lambda value: value)
    monkeypatch.setattr(api._store, "find_exact", lambda _where: [])
    monkeypatch.setattr(
        api._store,
        "search",
        lambda _vector, *, top_k, where: [
            SimpleNamespace(
                id="specification-1",
                score=1.0,
                metadata={
                    "type": "entity_attribute_value",
                    "attr_code": "specification",
                    "attr_name": "规格型号",
                    "attr_value": "1",
                    "entity_name": "商品",
                },
            )
        ],
    )
    request = api.SemanticDisplayResolveRequest(
        semantic_model_id=81,
        candidates=[{
            "candidate_id": "filter:0:0",
            "slot": "filter",
            "value": "1",
            "field_name": "适用科室类型",
        }],
    )

    response = api.semantic_display_elements_resolve(request)

    assert response.matches == []
