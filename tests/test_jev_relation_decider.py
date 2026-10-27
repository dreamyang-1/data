"""jev relation 终审：状态拼装、路由守卫、终审采纳与开关接线。"""
import asyncio
import logging

import pytest

from app.decision.jev_client import JevChoiceResult
from app.decision.relation_decider import (
    adopt_relation_verdict,
    build_relation_state,
    relation_override_guard,
)
from app.semantic_v2.context_v1_execution import V2ContextV1ExecutionBridge
from app.semantic_v2.recognition import RawTurnPlanner

DECIDER_LOGGER = "app.decision.relation_decider"


class _Capture(logging.Handler):
    """直接挂在目标 logger 上，避开全局日志配置对传播的影响。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def result(choice: str, confidence: float = 0.9) -> JevChoiceResult:
    return JevChoiceResult(choice=choice, confidence=confidence, probabilities={})


class _State:
    def __init__(self, pending=None):
        self.pending = pending


class _Proposal:
    def __init__(self, relation: str = "NEW_TASK"):
        self.relation = relation


# ---------- build_relation_state ----------

class _Req:
    question = "那华东的呢"


def test_state_first_turn_marks_empty_history():
    state = build_relation_state(_Req(), question_log=None)
    assert "【当前用户问题】那华东的呢" in state
    assert "无（本轮为会话首轮" in state
    assert "会话最近问题" not in state


def test_state_includes_recent_question_chain():
    log = [
        {"question": "今年上海市销售额多少", "answer_summary": "共 1 行，总额 120 万",
         "clarification_questions": ["要按月展示吗？"], "missing_slots": []},
        {"question": "按月展示", "answer_summary": ""},
    ]
    state = build_relation_state(_Req(), question_log=log, pending=object(), task_count=2)
    assert "今年上海市销售额多少" in state
    assert "上轮回答摘要：共 1 行" in state
    assert "系统曾反问：要按月展示吗" in state
    assert "存在等待用户回复的系统反问" in state
    assert "历史任务】2 个" in state
    # 只保留最近 3 轮，旧轮次不进 state
    assert state.count("｜") <= 4


def test_state_ignores_malformed_log_entries():
    state = build_relation_state(_Req(), question_log=["bad", None, {"question": "正常问题"}])
    assert "正常问题" in state
    assert "bad" not in state


# ---------- relation_override_guard ----------

def test_guard_allows_new_task_when_clean():
    trace = {"FINAL_TARGET": None}
    assert relation_override_guard("NEW_TASK", trace, _State(pending=None)) is None


def test_guard_rejects_new_task_with_bound_target():
    trace = {"FINAL_TARGET": object()}
    assert relation_override_guard(
        "NEW_TASK", trace, _State(pending=None)
    ) == "target_task_bound"


def test_guard_rejects_new_task_with_pending_clarification():
    trace = {"FINAL_TARGET": None}
    assert relation_override_guard(
        "NEW_TASK", trace, _State(pending=object())
    ) == "pending_clarification_active"


def test_guard_rejects_clarification_reply_without_pending():
    trace = {"FINAL_TARGET": object()}
    assert relation_override_guard(
        "ANSWER_CLARIFICATION", trace, _State(pending=None)
    ) == "no_pending_clarification"


def test_guard_allows_clarification_reply_with_pending():
    trace = {"FINAL_TARGET": None}
    assert relation_override_guard(
        "ANSWER_CLARIFICATION", trace, _State(pending=object())
    ) is None


def test_guard_rejects_followup_without_target():
    trace = {"FINAL_TARGET": None}
    assert relation_override_guard(
        "DRILL_DOWN", trace, _State(pending=None)
    ) == "no_target_for_followup"


def test_guard_allows_followup_with_target():
    trace = {"FINAL_TARGET": object()}
    assert relation_override_guard("DRILL_DOWN", trace, _State(pending=None)) is None


# ---------- adopt_relation_verdict ----------

@pytest.mark.asyncio
async def test_adopt_without_task_is_noop():
    assert await adopt_relation_verdict(
        None, _Proposal(), _State(), {"FINAL_RELATION": "NEW_TASK"}, "m1"
    ) is False


@pytest.mark.asyncio
async def test_adopt_agreement_keeps_qwen_relation():
    logger = logging.getLogger(DECIDER_LOGGER)
    capture = _Capture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        task = asyncio.create_task(asyncio.sleep(0, result=result("NEW_TASK")))
        proposal = _Proposal(relation="NEW_TASK")
        trace = {"FINAL_RELATION": "NEW_TASK", "FINAL_TARGET": None}
        adopted = await adopt_relation_verdict(task, proposal, _State(), trace, "m1")
    finally:
        logger.removeHandler(capture)
    assert adopted is False
    assert proposal.relation == "NEW_TASK"
    assert trace["FINAL_RELATION"] == "NEW_TASK"
    messages = [m for m in capture.records if "jev_relation verdict" in m]
    assert len(messages) == 1 and "agreed" in messages[0]


@pytest.mark.asyncio
async def test_adopt_override_rewrites_trace_and_proposal():
    task = asyncio.create_task(asyncio.sleep(0, result=result("MODIFY")))
    proposal = _Proposal(relation="NEW_TASK")
    trace = {"FINAL_RELATION": "NEW_TASK", "FINAL_TARGET": object()}
    adopted = await adopt_relation_verdict(task, proposal, _State(), trace, "m2")
    assert adopted is True
    assert proposal.relation == "MODIFY"
    assert trace["FINAL_RELATION"] == "MODIFY"


@pytest.mark.asyncio
async def test_adopt_guard_rejection_keeps_qwen_relation():
    logger = logging.getLogger(DECIDER_LOGGER)
    capture = _Capture()
    logger.addHandler(capture)
    logger.setLevel(logging.INFO)
    try:
        # jev 判新问题但已有绑定任务，守卫必须拦下
        task = asyncio.create_task(asyncio.sleep(0, result=result("NEW_TASK")))
        proposal = _Proposal(relation="MODIFY")
        trace = {"FINAL_RELATION": "MODIFY", "FINAL_TARGET": object()}
        adopted = await adopt_relation_verdict(task, proposal, _State(), trace, "m3")
    finally:
        logger.removeHandler(capture)
    assert adopted is False
    assert proposal.relation == "MODIFY"
    assert trace["FINAL_RELATION"] == "MODIFY"
    messages = [m for m in capture.records if "jev_relation override rejected" in m]
    assert len(messages) == 1 and "target_task_bound" in messages[0]


@pytest.mark.asyncio
async def test_adopt_cancels_slow_task_and_falls_back():
    async def slow():
        await asyncio.sleep(5)
        return result("NEW_TASK")

    task = asyncio.create_task(slow())
    proposal = _Proposal(relation="NEW_TASK")
    trace = {"FINAL_RELATION": "NEW_TASK", "FINAL_TARGET": None}
    adopted = await adopt_relation_verdict(task, proposal, _State(), trace, "m4")
    assert adopted is False
    assert proposal.relation == "NEW_TASK"
    # 超时任务被取消，不悬挂
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), timeout=1)


@pytest.mark.asyncio
async def test_adopt_swallows_task_exception():
    async def boom():
        raise RuntimeError("unexpected")

    task = asyncio.create_task(boom())
    proposal = _Proposal(relation="NEW_TASK")
    trace = {"FINAL_RELATION": "NEW_TASK", "FINAL_TARGET": None}
    assert await adopt_relation_verdict(task, proposal, _State(), trace, "m5") is False


@pytest.mark.asyncio
async def test_adopt_skips_when_jev_returns_none():
    task = asyncio.create_task(asyncio.sleep(0, result=None))
    proposal = _Proposal(relation="NEW_TASK")
    trace = {"FINAL_RELATION": "NEW_TASK", "FINAL_TARGET": None}
    assert await adopt_relation_verdict(task, proposal, _State(), trace, "m6") is False


@pytest.mark.asyncio
async def test_adopt_skips_when_qwen_relation_missing():
    task = asyncio.create_task(asyncio.sleep(0, result=result("NEW_TASK")))
    proposal = _Proposal(relation="NEW_TASK")
    trace = {"FINAL_RELATION": None, "FINAL_TARGET": None}
    assert await adopt_relation_verdict(task, proposal, _State(), trace, "m7") is False


# ---------- 开关接线 ----------

def test_planner_stores_decider_config():
    class StubSettings:
        jev_model = "jev-1.13.0"

    stub = StubSettings()
    engine = RawTurnPlanner(
        None, None, jev_relation_enabled=True, jev_settings=stub
    )
    assert engine.jev_relation_enabled is True
    assert engine.jev_settings is stub
    default_engine = RawTurnPlanner(None, None)
    assert default_engine.jev_relation_enabled is False
    assert default_engine.jev_settings is None


def test_bridge_stores_decider_config():
    bridge = V2ContextV1ExecutionBridge(
        store=object(), catalog=None, model=None, v1_executor=None,
        clock=lambda: None, startup_receipt={},
        jev_relation_enabled=True, jev_settings=object(),
    )
    assert bridge.jev_relation_enabled is True
    assert bridge.jev_settings is not None
    default_bridge = V2ContextV1ExecutionBridge(
        store=object(), catalog=None, model=None, v1_executor=None,
        clock=lambda: None, startup_receipt={},
    )
    assert default_bridge.jev_relation_enabled is False
    assert default_bridge.jev_settings is None
