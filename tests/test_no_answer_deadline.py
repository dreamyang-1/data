import asyncio
from uuid import uuid4

import pytest

from app.config import Settings
from app.domain.models import AgentResponse, PrimaryIntent
from app.main import create_app
from test_api import TestClient


@pytest.mark.parametrize('path', ['/agent_chat', '/agent_chat/stream'])
@pytest.mark.parametrize('isolated', [False, True])
def test_answer_waits_past_removed_budget(monkeypatch, path, isolated):
    # A legacy deployment value must no longer impose a total deadline.
    monkeypatch.setenv('DATA_AGENT_REQUEST_TIMEOUT_SECONDS', '0.01')
    settings = Settings(_env_file=None, env='test', adapter_mode='mock',
        intent_model_enabled=False, business_question_collection_enabled=False,
        trusted_backend_token='phase0c-fixture-token')
    completed = []

    async def answer(chat):
        await asyncio.sleep(.08)
        completed.append(True)
        return AgentResponse(request_id=uuid4(), conversation_id=chat.conversation_id,
            status='COMPLETED', intent=PrimaryIntent.CHAT, answer='完整回答')

    class Handler:
        async def handle(self, chat, identity):
            return await answer(chat)

    class Workflow:
        async def ainvoke(self, state):
            return {'response': await answer(state['chat'])}

    app = create_app(settings, **({'isolated_chat_handler': Handler()} if isolated else {}))
    with TestClient(app) as client:
        if not isolated:
            object.__setattr__(app.state.container, 'workflow', Workflow())
        response = client.post(path, json={'semantic_model_id':81, 'business_domain_ids':[],
            'application_id':'app1', 'conversation_id':str(uuid4()),
            'message_id':str(uuid4()), 'question':'请处理这个问题'})
    assert response.status_code == 200
    assert completed == [True]
    assert '完整回答' in response.text
    assert not hasattr(settings, 'request_timeout_seconds')
