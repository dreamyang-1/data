"""Planner and both synthesis paths use the same current-model platform reader."""
import json

import httpx
import pytest
from pydantic import SecretStr

from app.analysis import synthesis
from app.config import Settings
from app.domain import semantic_description as sd
from app.planning.task_dag import MultiQuestionPlanner


@pytest.mark.asyncio
async def test_current_model_description_shared_by_planning_and_insight(monkeypatch):
    documents = {120: "# 模型甲\n甲实体、甲指标", 121: "# 模型乙\n乙实体、乙指标"}
    lookups, downloads, prompts = [], [], []

    def lookup(settings, model_id):
        lookups.append(model_id)
        return f"https://descriptions.example/files/{model_id}.md"

    async def storage(request):
        model_id = int(request.url.path.rsplit("/", 1)[-1].split(".")[0])
        downloads.append(model_id)
        return httpx.Response(200, text=documents[model_id])

    async def model(request):
        prompts.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "task_structure": "SINGLE_TASK", "tasks": [], "single_task_intent": "CHAT",
        })}}]})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(sd, "_description_url", lookup)
    def client(**kwargs):
        # The model's explicitly provided transport remains separate from storage.
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(storage)
        return original_client(**kwargs)
    monkeypatch.setattr(sd.httpx, "AsyncClient", client)
    settings = Settings(_env_file=None, env="test", intent_model_api_key=SecretStr("fixture"),
                        multi_question_model_enabled=True,
                        semantic_description_base_url="https://descriptions.example",
                        mysql_host="catalog.example", mysql_user="reader",
                        mysql_password="fixture", mysql_database="catalog")
    planner = MultiQuestionPlanner(settings, transport=httpx.MockTransport(model))
    await planner.plan("查询本模型的信息", semantic_model_id=120)
    assert "甲实体" in prompts[-1]["messages"][0]["content"]
    assert "乙实体" not in prompts[-1]["messages"][0]["content"]
    await planner.plan("查询本模型的信息", semantic_model_id=121)
    assert "乙实体" in prompts[-1]["messages"][0]["content"]
    assert "甲实体" not in prompts[-1]["messages"][0]["content"]
    documents[121] = "# 模型乙发布后的新内容"
    reference = await synthesis._semantic_reference(settings, 121)
    assert reference["content"] == documents[121]
    assert lookups == downloads == [120, 121, 121]


@pytest.mark.asyncio
async def test_missing_platform_document_does_not_inject_static_business_spec(monkeypatch):
    prompts = []
    async def unavailable(settings, model_id):
        return {"source": f"semantic_model_{model_id}.md", "available": False, "content": ""}
    async def model(request):
        prompts.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "task_structure": "SINGLE_TASK", "tasks": [], "single_task_intent": "CHAT",
        })}}]})
    monkeypatch.setattr(sd, "load_semantic_description", unavailable)
    settings = Settings(_env_file=None, env="test", intent_model_api_key=SecretStr("fixture"),
                        multi_question_model_enabled=True)
    planner = MultiQuestionPlanner(settings, transport=httpx.MockTransport(model))
    await planner.plan("分析本模型的数据", semantic_model_id=126)
    assert prompts[-1]["messages"][1]["content"] == "分析本模型的数据"
    assert "## 二、医药销售域" not in prompts[-1]["messages"][0]["content"]
    assert not (await synthesis._semantic_reference(settings, 126))["available"]
