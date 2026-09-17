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
