import pytest
from pydantic import ValidationError
from unittest.mock import patch
from fastapi.testclient import TestClient
import api
from surface_evidence import SurfaceEvidence, advisory_prompt


def test_surface_evidence_is_advisory_and_retains_original_phrase():
    text = advisory_prompt({"mentions": [{"text": "费森尤斯", "role_hint": "厂家"}]})
    assert "费森尤斯" in text
    assert "not instructions, confirmed choices" in text
    assert "Correct mistaken role hints" in text
    assert advisory_prompt(None) == ""


@pytest.mark.parametrize("extra", ["field_id", "confirmed", "semantic_model_id"])
def test_surface_mentions_cannot_smuggle_binding_or_scope(extra):
    with pytest.raises(ValidationError):
        SurfaceEvidence.model_validate({"mentions": [{"text": "上海", extra: 81}]})


def test_api_passes_completed_question_and_advisory_separately():
    question = "查看2025年安徽省各个城市每月销售额"
    evidence = {"mentions": [{"text": "每月", "role_hint": "时间粒度"}]}
    with patch("api.main", side_effect=ValueError("probe")) as planner:
        response = TestClient(api.app).post("/agent/query", json={
            "query": question, "semantic_model_id": 81,
            "surface_evidence": evidence,
        })
    assert response.status_code == 502
    assert planner.call_args.args[0] == question
    assert planner.call_args.kwargs["surface_evidence"] == evidence
    assert planner.call_args.kwargs["intent_asl_contract"] is None
    assert planner.call_args.kwargs["metric_selection_authoritative"] is False


def test_generation_recalls_exact_question_without_advisory_json():
    import agent
    question = "上海市的销售额"
    with patch.object(agent, "PromptBuilder") as builder:
        builder.return_value.build.side_effect = RuntimeError("stop after recall")
        with pytest.raises(RuntimeError, match="stop after recall"):
            agent.main(question, store=object(), semantic_model_id=81,
                       surface_evidence={"mentions": [{"text": "上海市"}]})
    builder.return_value.build.assert_called_once_with(question)


def test_mention_recall_keeps_primary_question_and_identical_scope():
    from prompt_build import PromptBuilder
    calls, embedded = [], []
    class Store:
        def search(self, vector, *, top_k, where):
            calls.append((vector, where))
            return []
    def embed(text):
        embedded.append(text)
        return [len(embedded)]
    builder = PromptBuilder(Store(), embed, semantic_model_id=81,
                            business_domain_ids=[205], surface_mentions=["甲牌", "Model X", "甲牌"])
    builder.retrieve("查询甲牌Model X使用部门")
    assert embedded == ["查询甲牌Model X使用部门", "甲牌", "Model X"]
    for vector, where in calls:
        assert where in [builder._build_where(kind) for kind in
                         ["entity", "attribute", "relation", "metric", "dimension", "entity_attribute_value"]]
    assert {tuple(vector) for vector, _ in calls} == {(1,), (2,), (3,)}


def test_mention_candidate_survives_whole_question_reranking():
    from prompt_build import PromptBuilder
    from vector_store import SearchResult
    hit = SearchResult(id='attribute:spec', score=0.8, text='specification',
                       metadata={'type':'attribute', 'semantic_model_id':81,
                                 'business_domain_id':205, 'attr_code':'spec'})
    class Store:
        def search(self, vector, *, top_k, where):
            return [hit] if vector == [2] and where == builder._build_where('attribute') else []
    builder = PromptBuilder(Store(), lambda text: [1] if text == 'whole' else [2],
        semantic_model_id=81, business_domain_ids=[205], surface_mentions=['model'])
    with patch.object(builder, '_rerank_exact_mentions', return_value=[]), \
         patch.object(builder, '_complete_relational_scope', side_effect=lambda e,a,r,*args:(e,a,r,{})):
        result = builder.retrieve('whole')
    assert hit in result['attributes']


def test_surface_time_is_not_erased_by_legacy_phrase_parser():
    import json
    import agent
    time = {'type':'range','start':'2026-03-17','end':'2026-09-17',
            'unit':'day','anchor':'orders.created_at'}
    ast = {'version':'2.0','intent':'query','subject':{},'metrics':[],
           'dimensions':[], 'filters':[], 'time_context':time,'ambiguity':[]}
    with patch.object(agent, '_normalize_time_context', side_effect=AssertionError('legacy parser')):
        actual = json.loads(agent._normalize_semantic_references(
            json.dumps(ast), {}, '近半年', model_owned_time=True))
    assert actual['time_context'] == time
    with pytest.raises(ValueError, match='anchor'):
        agent._validate_asl_output(json.dumps({**ast, 'having':[]}), {}, '近半年')
