import json
from agent import _select_surface_mention_match, _apply_surface_mention_normalization
from test_intent_asl_contract import _entity, _detail_ast


def candidates():
    return [{"field": field, "canonical_value": "示例", "match_type": "EXACT"}
            for field in ("product.name", "brand.name")]


def test_tie_is_nonblocking_and_reports_selected_and_other_candidates():
    messages = []
    chosen = _select_surface_mention_match("示例", candidates(), {"product.name", "brand.name"},
                                          diagnostics=messages, selection_key="task-1")
    assert chosen in {("product.name", "示例"), ("brand.name", "示例")}
    assert messages[0]["resolved_field"] == chosen[0]
    assert messages[0]["score_type"] == "STRING_SIMILARITY"
    assert len(messages[0]["candidates"]) == 2


def test_retry_and_candidate_order_do_not_change_choice():
    choices = [_select_surface_mention_match("示例", values, {"product.name", "brand.name"},
                                            selection_key="model:domain:task")
               for values in [candidates(), list(reversed(candidates()))] * 5]
    assert len(set(choices)) == 1


def test_api_forwards_existing_idempotency_header_to_selection(monkeypatch):
    import api
    from fastapi.testclient import TestClient
    calls = []
    def generate(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError("probe stops before model access")
    monkeypatch.setattr(api, "main", generate)
    response = TestClient(api.app).post("/agent/query",
        json={"query": "示例", "semantic_model_id": 81},
        headers={"Idempotency-Key": "same-task"})
    assert response.status_code == 502
    assert calls[0]["selection_key"] == "same-task"


def test_duplicates_do_not_weight_choice_and_out_of_scope_is_excluded():
    messages = []
    chosen = _select_surface_mention_match("示例", candidates() * 3, {"product.name"}, diagnostics=messages)
    assert chosen == ("product.name", "示例")
    assert messages[0]["reason"] == "DUPLICATE_CANDIDATES"
    assert messages[0]["duplicate_count"] == 2
    assert messages[0]["candidates"][0]["field"] == "product.name"


def test_resolved_tie_retires_its_filter_question_not_time_or_other_values():
    from agent import _clear_advisory_filter_ambiguities
    ast = {"ambiguity": [
        {"type": "filter", "question": "示例对应哪个product.name？"},
        {"type": "filter", "question": "其他商品对应哪个product.name？"},
        {"type": "time", "phrase": "示例"},
    ]}
    _clear_advisory_filter_ambiguities(ast, "示例", "product.name", "示例")
    assert len(ast["ambiguity"]) == 2
    assert ast["ambiguity"][0]["question"].startswith("其他商品")


def test_unmatched_wildcard_is_dropped_with_notice_and_only_its_value_ambiguity_cleared(monkeypatch):
    import agent
    knowledge = {"entities": [_entity("product", "商品", "product.product_name", "商品名称")]}
    ast = json.loads(_detail_ast("product"))
    ast["filters"] = [{"field": "product.product_name", "operator": "LIKE", "value": "%未登记产品%"}]
    ast["ambiguity"] = [{"type": "filter", "phrase": "未登记产品"},
                        {"type": "time", "phrase": "未登记产品"},
                        {"type": "filter", "phrase": "其他值"}]
    monkeypatch.setattr(agent, "resolve_entity_attribute_catalog_matches", lambda *_: [])
    monkeypatch.setattr(agent, "load_published_entity_attribute_candidates", lambda *_: [])
    content, repairs = _apply_surface_mention_normalization(json.dumps(ast), knowledge,
        {"mentions": [{"text": "未登记产品"}]}, 81, 205)
    parsed = json.loads(content)
    assert parsed["filters"] == []
    assert len(parsed["ambiguity"]) == 2
    assert repairs[0]["type"] == "DROP_UNMATCHED_SURFACE_MENTION"
