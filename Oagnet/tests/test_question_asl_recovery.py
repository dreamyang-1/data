"""Natural-language fallback was removed; missing extraction is a failure."""
import json
import pytest
import agent

@pytest.mark.parametrize('question', ['查询医院总数', '查询上海经销商', '2'])
def test_question_alone_never_authorizes_asl_generation(monkeypatch, question):
    monkeypatch.setattr(agent, '_get_chat_model', lambda: pytest.fail('unexpected extraction'))
    result=json.loads(agent.main(question, completed_question=question,
        store=object(), semantic_model_id=81, business_domain_ids=[205]))
    assert result['ambiguity'] and result['metrics'] == []
