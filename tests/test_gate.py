"""前置门控：jev 主判定、qwen 两种兜底、action 推导与解析健壮性。"""
import json

import httpx
import pytest
from pydantic import SecretStr

import app.gate.gate as gate_module
from app.config import Settings
from app.decision.jev_client import JevChoiceResult
from app.gate.gate import (
    GateDecision,
    _decision_from_qwen,
    _parse_qwen_output,
    _qwen_gate,
    build_gate_context,
    run_gate,
)


def make_settings(**updates) -> Settings:
    values = {
        "env": "test",
        "adapter_mode": "mock",
        "gate_enabled": True,
        "gate_judge": "jev",
        "gate_spec_cache_seconds": 300,
        "jev_enabled": True,
        "jev_api_key": SecretStr("test-key"),
        "jev_model": "jev-1.13.0",
        "jev_base_url": "https://jev.test/v1/systemone",
        "jev_proxy": "",
        "jev_min_confidence": 0.8,
        "jev_timeout_seconds": 3.0,
        "intent_model_api_key": SecretStr("qwen-key"),
        "intent_model_base_url": "https://qwen.test/v1",
        "intent_model_name": "qwen3.7-max",
    }
    values.update(updates)
    return Settings(**values)


def jev_result(choice: str, confidence: float) -> JevChoiceResult:
    return JevChoiceResult(choice=choice, confidence=confidence, probabilities={})


def qwen_decision(gate_class: str, reason: str = "") -> GateDecision:
    return GateDecision(
        gate_class=gate_class,
        confidence=0.95,
        need_clarification=gate_class == "待定",
        reason=reason,
        judge="qwen",
    )


@pytest.fixture(autouse=True)
def no_spec(monkeypatch):
    """语义规范加载统一打桩，避免测试碰平台库与文件服务。"""
    async def fake_load(settings, semantic_model_id):
        return ""

    monkeypatch.setattr(gate_module, "_load_spec", fake_load)


@pytest.fixture
def jev_calls(monkeypatch):
    """打桩 jev_choice，返回预设结果并记录调用。"""
    state = {"result": None, "calls": 0, "error": None}

    async def fake_choice(settings, *, state_text, question_name,
                          instructions, criteria, transport=None):
        state["calls"] += 1
        state["state_text"] = state_text
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(gate_module, "jev_choice", fake_choice)
    return state


@pytest.fixture
def qwen_calls(monkeypatch):
    """打桩 _qwen_gate，返回预设结果并记录调用。"""
    state = {"result": None, "calls": 0}

    async def fake_qwen(settings, *, question, context, spec, transport=None):
        state["calls"] += 1
        return state["result"]

    monkeypatch.setattr(gate_module, "_qwen_gate", fake_qwen)
    return state


# ---------- run_gate 路由矩阵 ----------

@pytest.mark.asyncio
async def test_gate_disabled_returns_none(jev_calls, qwen_calls):
    result = await run_gate(
        make_settings(gate_enabled=False), question="你好"
    )
    assert result is None
    assert jev_calls["calls"] == 0
    assert qwen_calls["calls"] == 0


@pytest.mark.asyncio
async def test_gate_settings_none_returns_none(jev_calls):
    assert await run_gate(None, question="你好") is None
    assert jev_calls["calls"] == 0


@pytest.mark.asyncio
async def test_jev_business_pass(jev_calls, qwen_calls):
    jev_calls["result"] = jev_result("业务场景问题", 0.96)
    result = await run_gate(make_settings(), question="查询上海各经销商覆盖率")
    assert result is not None
    assert result.action == "pass"
    assert result.judge == "jev"
    assert qwen_calls["calls"] == 0


@pytest.mark.asyncio
async def test_jev_followup_pass(jev_calls, qwen_calls):
    jev_calls["result"] = jev_result("疑似业务追问", 0.95)
    result = await run_gate(make_settings(), question="生成统计图")
    assert result is not None
    assert result.action == "pass"
    assert qwen_calls["calls"] == 0


@pytest.mark.asyncio
async def test_jev_chat_short_circuit(jev_calls, qwen_calls):
    jev_calls["result"] = jev_result("闲聊", 1.0)
    result = await run_gate(make_settings(), question="你好")
    assert result is not None
    assert result.action == "chat"
    assert qwen_calls["calls"] == 0


@pytest.mark.asyncio
async def test_jev_failure_falls_back_to_qwen(jev_calls, qwen_calls):
    jev_calls["result"] = None  # 调用失败
    qwen_calls["result"] = qwen_decision("待定", reason="缺少上下文")
    result = await run_gate(make_settings(), question="生成统计图")
    assert result is not None
    assert result.judge == "qwen"
    assert result.action == "clarify"
    assert qwen_calls["calls"] == 1


@pytest.mark.asyncio
async def test_jev_low_confidence_falls_back_to_qwen(jev_calls, qwen_calls):
    # 低置信在 jev_choice 内返回 None，与失败同路兜底
    jev_calls["result"] = None
    qwen_calls["result"] = qwen_decision("业务场景问题")
    result = await run_gate(make_settings(), question="查询业务员的手机号")
    assert result is not None
    assert result.judge == "qwen"
    assert result.action == "pass"


@pytest.mark.asyncio
async def test_jev_exception_falls_back_to_qwen(jev_calls, qwen_calls):
    jev_calls["error"] = RuntimeError("boom")
    qwen_calls["result"] = qwen_decision("闲聊")
    result = await run_gate(make_settings(), question="你好")
    assert result is not None
    assert result.judge == "qwen"
    assert result.action == "chat"


@pytest.mark.asyncio
async def test_jev_danger_requires_qwen_confirmation(jev_calls, qwen_calls):
    jev_calls["result"] = jev_result("数据危险操作", 0.99)
    qwen_calls["result"] = qwen_decision("数据危险操作", reason="含删除意图")
    result = await run_gate(make_settings(), question="把结果删掉")
    assert result is not None
    assert result.action == "danger"
    assert result.judge == "qwen"
    assert qwen_calls["calls"] == 1


@pytest.mark.asyncio
async def test_jev_danger_overruled_by_qwen(jev_calls, qwen_calls):
    # 元讨论豁免场景：jev 判危险，qwen 复核改判业务场景，不拦截
    jev_calls["result"] = jev_result("数据危险操作", 0.94)
    qwen_calls["result"] = qwen_decision("业务场景问题", reason="元讨论豁免")
    result = await run_gate(make_settings(), question="删除订单需要什么审批流程")
    assert result is not None
    assert result.action == "pass"


@pytest.mark.asyncio
async def test_jev_danger_qwen_confirm_fails_abstains(jev_calls, qwen_calls):
    jev_calls["result"] = jev_result("数据危险操作", 0.99)
    qwen_calls["result"] = None
    result = await run_gate(make_settings(), question="把结果删掉")
    assert result is None


@pytest.mark.asyncio
async def test_qwen_judge_mode_calls_qwen_directly(jev_calls, qwen_calls):
    qwen_calls["result"] = qwen_decision("非业务场景问题")
    result = await run_gate(
        make_settings(gate_judge="qwen"), question="今天天气怎么样"
    )
    assert result is not None
    assert result.action == "off_topic"
    assert jev_calls["calls"] == 0
    assert qwen_calls["calls"] == 1


@pytest.mark.asyncio
async def test_jev_state_carries_spec_and_context(jev_calls, monkeypatch):
    async def fake_load(settings, semantic_model_id):
        return "# 业务语义规范\n内容"

    monkeypatch.setattr(gate_module, "_load_spec", fake_load)
    jev_calls["result"] = jev_result("疑似业务追问", 0.9)

    question_log = [
        {"question": "上一轮问题", "answer_summary": "上一轮回答摘要"},
    ]
    await run_gate(make_settings(), question="生成统计图", question_log=question_log)
    state = jev_calls["state_text"]
    assert "# 业务语义规范" in state
    assert "上一轮问题" in state
    assert "上一轮回答摘要" in state


# ---------- qwen 输出解析 ----------

def test_parse_qwen_output_strips_fence():
    text = "```json\n{\"intent_type\": 2, \"intent_name\": \"业务场景问题\"}\n```"
    parsed = _parse_qwen_output(text)
    assert parsed is not None
    assert parsed["intent_type"] == 2


def test_parse_qwen_output_extracts_embedded_json():
    parsed = _parse_qwen_output('前缀解释 {\"intent_type\": 1} 后缀')
    assert parsed == {"intent_type": 1}


def test_parse_qwen_output_garbage_returns_none():
    assert _parse_qwen_output("无法解析的内容") is None


def test_decision_from_qwen_maps_type_number():
    decision = _decision_from_qwen({
        "intent_type": 5,
        "confidence": 0.9,
        "schema_coverage": "covered",
        "unsupported_fields": ["手机号"],
        "reason": "含删除意图",
    })
    assert decision is not None
    assert decision.gate_class == "数据危险操作"
    assert decision.action == "danger"
    assert decision.reason == "含删除意图"


def test_decision_from_qwen_rejects_unknown_class():
    assert _decision_from_qwen({"intent_name": "不存在的类"}) is None
    assert _decision_from_qwen({"intent_type": 99}) is None


# ---------- qwen HTTP 调用 ----------

@pytest.mark.asyncio
async def test_qwen_gate_http_roundtrip():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "intent_type": 2,
                "intent_name": "业务场景问题",
                "confidence": 0.95,
                "need_clarification": False,
                "schema_coverage": "covered",
                "unsupported_fields": [""],
                "reason": "可独立映射业务域",
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        })

    decision = await _qwen_gate(
        make_settings(),
        question="查询上海各经销商覆盖率",
        context="",
        spec="# 业务语义规范\n内容",
        transport=httpx.MockTransport(handler),
    )
    assert decision is not None
    assert decision.action == "pass"
    assert decision.reason == "可独立映射业务域"
    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "system"
    assert "# 业务语义规范" in messages[0]["content"]
    assert "查询上海各经销商覆盖率" in messages[1]["content"]


@pytest.mark.asyncio
async def test_qwen_gate_http_error_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    decision = await _qwen_gate(
        make_settings(), question="x", context="", spec="",
        transport=httpx.MockTransport(handler),
    )
    assert decision is None


# ---------- 上下文渲染 ----------

def test_build_gate_context_empty():
    assert build_gate_context(None) == ""
    assert build_gate_context([]) == ""


def test_build_gate_context_renders_question_log():
    question_log = [
        {"question": "最早的问题"},
        {
            "question": "最近的问题",
            "answer_summary": "返回了经销商名单",
            "clarification_questions": ["要看哪个省份？"],
        },
    ]
    text = build_gate_context(question_log)
    lines = text.split("\n")
    # 最近的在前，与 relation 判定的渲染顺序一致
    assert lines[0].startswith("1. 最近的问题")
    assert "返回了经销商名单" in lines[0]
    assert "要看哪个省份？" in lines[0]
    assert lines[1] == "2. 最早的问题"


def test_build_gate_context_truncates_and_skips_empty():
    question_log = [
        {"answer_summary": "没有问题原文的条目"},
        {"question": "长问题" * 100},
    ]
    text = build_gate_context(question_log, max_chars=10)
    lines = text.split("\n")
    assert len(lines) == 1
    assert lines[0] == "1. " + "长问题" * 3 + "长"
