import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

import agent
from prompt_build import SYSTEM_PROMPT


CODE = "total_hospitals_in_region"
NAME = "区域全部医院总数"


def catalog(name=NAME, formula="COUNT(DISTINCT hospital.hospital_code)"):
    return {"metrics": [SimpleNamespace(id=CODE, score=0.99, metadata={
        "type": "metric", "metric_code": CODE, "metric_name": name,
        "semantic_model_id": 81, "business_domain_id": 205,
        "source_dependency": {"bind_entity": ["hospital"]},
        "calculation_rule": {"calc_formula": formula},
    })], "entities": [SimpleNamespace(id="hospital", score=0.99, metadata={
        "type": "entity", "entity_code": "hospital", "entity_name": "医院",
        "semantic_model_id": 81, "business_domain_id": 205,
    })]}


def draft(alias="医院总数"):
    return {"version": "2.0", "intent": "query", "subject": {"entity": "hospital"},
            "metrics": [{"name": CODE, "alias": alias, "time_anchor": None}],
            "dimensions": [], "filters": [], "time_context": None,
            "sort": {"field": CODE, "field_type": "metric", "direction": "DESC"},
            "limit": None, "having": [], "ambiguity": []}


@pytest.mark.parametrize("alias", ["医院总数", "医院数量", "模型随意改写", None, "", NAME])
def test_final_validator_uses_vector_standard_name_not_model_alias(alias):
    ast = draft(alias)
    result = json.loads(agent._validate_asl_output(json.dumps(ast), catalog()))
    assert result["metrics"] == [{"name": CODE, "alias": NAME, "time_anchor": None}]
    ast["metrics"][0]["alias"] = NAME
    assert result == ast  # Does not change metric identity, sort, filters or grain.


def test_missing_alias_is_completed_and_json_metadata_supported():
    ast = draft()
    ast["metrics"][0].pop("alias")
    info = catalog()
    m = info["metrics"][0].metadata
    m["calculation_rule"] = json.dumps(m["calculation_rule"])
    result = json.loads(agent._validate_asl_output(json.dumps(ast), info))
    assert result["metrics"][0]["alias"] == NAME


@pytest.mark.parametrize("name", [None, "", "  "])
def test_missing_catalog_name_uses_standard_code_without_new_blocker(name):
    result = json.loads(agent._validate_asl_output(json.dumps(draft()), catalog(name)))
    assert result["metrics"][0]["alias"] == CODE


@pytest.mark.parametrize("suffix", ["AS `目录列名`", "as canonical_count", 'AS "目录列名"', "AS '目录列名';"])
def test_old_formula_alias_does_not_override_canonical_metric_label(suffix):
    result = json.loads(agent._validate_asl_output(json.dumps(draft()), catalog(formula="COUNT(*) " + suffix)))
    assert result["metrics"][0]["alias"] == NAME


def test_cast_as_type_is_not_output_alias():
    result = json.loads(agent._validate_asl_output(json.dumps(draft()), catalog(formula="CAST(COUNT(*) AS UNSIGNED)")))
    assert result["metrics"][0]["alias"] == NAME


def test_standard_looking_alias_cannot_authorize_unrecalled_code():
    ast = draft(NAME)
    ast["metrics"][0]["name"] = "invented_hospital_count"
    with pytest.raises(ValueError, match="not retrieved from semantic scope"):
        agent._validate_asl_output(json.dumps(ast), catalog())


def test_prompt_no_longer_instructs_model_to_copy_user_wording():
    assert "alias` 填用户原话" not in SYSTEM_PROMPT
    assert '"alias": "用户原话"' not in SYSTEM_PROMPT
    assert "所选 `metric_code` 对应的 `metric_name`" in SYSTEM_PROMPT


@pytest.mark.parametrize("include_evidence", [False, True])
def test_main_canonicalizes_model_alias_before_return_and_evidence(monkeypatch, include_evidence):
    class Builder:
        def __init__(self, *args, **kwargs):
            self.last_knowledge = deepcopy(catalog())
        def build(self, query):
            return "offline scoped catalog"
    observed = []
    def evidence(content, *args):
        observed.append(json.loads(content))
        return {"selected_metrics": []}
    monkeypatch.setattr(agent, "PromptBuilder", Builder)
    monkeypatch.setattr(agent, "_get_chat_model", lambda: object())
    monkeypatch.setattr(agent, "create_deep_agent", lambda **kwargs: SimpleNamespace(
        invoke=lambda _: {"messages": [SimpleNamespace(content=json.dumps(draft()))]}))
    monkeypatch.setattr(agent, "_normalize_dynamic_subject", lambda content, *args: content)
    monkeypatch.setattr(agent, "_build_semantic_evidence", evidence)
    result = agent.main("查询医院总数", store=object(), semantic_model_id=81,
                        business_domain_ids=[205], include_evidence=include_evidence)
    ast = json.loads(result["result"] if include_evidence else result)
    assert ast["metrics"][0]["name"] == CODE
    assert ast["metrics"][0]["alias"] == NAME
    if include_evidence:
        assert observed[0]["metrics"] == ast["metrics"]
