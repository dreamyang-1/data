"""jev 决策客户端：MockTransport 注入，覆盖成功解析与各失败路径。"""
import json

import httpx
import pytest
from pydantic import SecretStr

import app.decision.jev_client as jev_module
from app.config import Settings
from app.decision.jev_client import JevChoiceResult, jev_choice, read_jev_api_key

CRITERIA = {"METRIC_QUERY": "统计汇总", "DETAIL_QUERY": "名单明细"}


def settings(**updates) -> Settings:
    values = {
        "env": "test",
        "adapter_mode": "mock",
        "jev_enabled": True,
        "jev_api_key": SecretStr("test-key"),
        "jev_model": "jev-1.13.0",
        "jev_base_url": "https://jev.test/v1/systemone",
        "jev_proxy": "",
        "jev_min_confidence": 0.8,
        "jev_timeout_seconds": 3.0,
    }
    values.update(updates)
    return Settings(**values)


def jev_response(choice: str, confidence: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={"answers": {"intent": {
            "type": "choice",
            "choice": choice,
            "confidence": confidence,
            "probabilities": {choice: confidence},
        }}},
    )


@pytest.mark.asyncio
async def test_jev_choice_parses_answer_and_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return jev_response("METRIC_QUERY", 0.93)

    result = await jev_choice(
        settings(), state_text="统计上海市销售额",
        question_name="intent", instructions="判定意图",
        criteria=CRITERIA, transport=httpx.MockTransport(handler),
    )
    assert isinstance(result, JevChoiceResult)
    assert result.choice == "METRIC_QUERY"
    assert result.confidence == pytest.approx(0.93)
    assert result.probabilities == {"METRIC_QUERY": 0.93}
    assert captured["url"] == "https://jev.test/v1/systemone"
    assert captured["auth"] == "Bearer test-key"
    question = captured["body"]["questions"]["intent"]
    assert question["criteria"] == CRITERIA
    assert captured["body"]["model"] == "jev-1.13.0"


@pytest.mark.asyncio
async def test_jev_choice_http_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    result = await jev_choice(
        settings(), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(handler),
    )
    assert result is None


@pytest.mark.asyncio
async def test_jev_choice_network_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("tunnel down")

    result = await jev_choice(
        settings(), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(handler),
    )
    assert result is None


@pytest.mark.asyncio
async def test_jev_choice_non_json_body_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    result = await jev_choice(
        settings(), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(handler),
    )
    assert result is None


@pytest.mark.asyncio
async def test_jev_choice_out_of_range_choice_returns_none():
    result = await jev_choice(
        settings(), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(
            lambda request: jev_response("CHATTING", 0.99)
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_jev_choice_low_confidence_returns_none():
    result = await jev_choice(
        settings(), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(
            lambda request: jev_response("METRIC_QUERY", 0.5)
        ),
    )
    assert result is None


@pytest.mark.asyncio
async def test_jev_choice_without_key_returns_none(monkeypatch):
    monkeypatch.setattr(jev_module, "_key_file_cache", "")
    result = await jev_choice(
        settings(jev_api_key=None), state_text="x", question_name="intent",
        instructions="i", criteria=CRITERIA,
        transport=httpx.MockTransport(
            lambda request: jev_response("METRIC_QUERY", 0.99)
        ),
    )
    assert result is None


def test_read_jev_api_key_prefers_settings_over_file(monkeypatch, tmp_path):
    monkeypatch.setattr(jev_module, "_key_file_cache", "file-key")
    assert read_jev_api_key(settings()) == "test-key"
    assert read_jev_api_key(settings(jev_api_key=None)) == "file-key"


def test_read_jev_api_key_reads_key_file(monkeypatch, tmp_path):
    key_file = tmp_path / "jev.key"
    key_file.write_text(" file-stored-key \n", encoding="utf-8")
    monkeypatch.setattr(jev_module, "_KEY_FILE", key_file)
    monkeypatch.setattr(jev_module, "_key_file_cache", None)
    assert read_jev_api_key(settings(jev_api_key=None)) == "file-stored-key"
