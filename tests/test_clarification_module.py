"""澄清上下文独立模块的离线测试：问题链落盘、上下文构建、判定解析。"""
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis
import pytest

from app.clarification import build_context_text, decide
from app.clarification.context_builder import (
    build_question_chain_section,
    build_result_section,
)
from app.stores.session import InMemorySessionStore, RedisSessionStore


def _entry(question: str, message_id: str = "m1", **extra) -> dict:
    entry = {
        "message_id": message_id,
        "question": question,
        "intent": "DATA_QUERY",
        "status": "COMPLETED",
        "answer_summary": "查询完成，共3行",
        "clarification_questions": [],
        "missing_slots": [],
        "result_summaries": [],
    }
    entry.update(extra)
    return entry


class TestQuestionLogStores:
    def test_inmemory_roundtrip_and_cap(self):
        store = InMemorySessionStore()

        async def run():
            for index in range(25):
                await store.append_question_log(
                    "t", "u", "1", "c", _entry(f"问题{index}", f"m{index}"), max_items=20
                )
            return await store.get_question_log("t", "u", "1", "c")

        log = asyncio.run(run())
        assert len(log) == 20
        # 保留最近 20 轮：最早的是问题5，最新的是问题24
        assert log[0]["question"] == "问题5"
        assert log[-1]["question"] == "问题24"

    def test_redis_roundtrip_and_cap(self):
        store = RedisSessionStore(
            fakeredis.aioredis.FakeRedis(),
            ttl_seconds=3600,
            response_ttl_seconds=3600,
            prefix="da-test",
        )

        async def run():
            for index in range(23):
                await store.append_question_log(
                    "t", "u", "1", "c", _entry(f"问题{index}", f"m{index}"), max_items=20
                )
            log = await store.get_question_log("t", "u", "1", "c")
            # 空会话读取
            empty = await store.get_question_log("t", "u", "1", "other")
            return log, empty

        log, empty = asyncio.run(run())
        assert len(log) == 20
        # lpush 新头在前：第一条是最新一轮
        assert log[0]["question"] == "问题22"
        assert log[-1]["question"] == "问题3"
        assert empty == []

    def test_redis_invalid_entry_skipped(self):
        store = RedisSessionStore(
            fakeredis.aioredis.FakeRedis(),
            ttl_seconds=3600,
            response_ttl_seconds=3600,
            prefix="da-test",
        )

        async def run():
            key = store._key("question-log", "t", "u", "1", "c")
            await store.redis.lpush(key, "not-json")
            await store.redis.lpush(key, json.dumps(_entry("正常问题")))
            return await store.get_question_log("t", "u", "1", "c")

        log = asyncio.run(run())
        assert len(log) == 1
        assert log[0]["question"] == "正常问题"


class TestContextBuilder:
    def test_question_chain_orders_oldest_first(self):
        log = [
            _entry("最新问题", "m2", clarification_questions=["要看哪家？"]),
            _entry("最早问题", "m1", missing_slots=["time_range"]),
        ]
        text = build_question_chain_section(log)
        assert "【近期用户问题】" in text
        assert text.index("最早问题") < text.index("最新问题")
        assert "系统反问：要看哪家？" in text
        assert "缺失参数：time_range" in text

    def test_result_section_passes_summary_through(self):
        table = {
            "question": "查经销商名单",
            "tasks": [
                {
                    "名称": "查经销商名单",
                    "结果概要": {
                        "columns": ["经销商名称", "含税金额"],
                        "row_count": 2,
                        "sample": [
                            {"经销商名称": "A经销商", "含税金额": "100"},
                            {"经销商名称": "B经销商", "含税金额": "80"},
                        ],
                    },
                },
                "坏行",
                {"名称": "无概要任务"},
            ],
        }
        text = build_result_section(table)
        # 全列透传，不砍列不砍行
        assert "经销商名称=A经销商" in text
        assert "含税金额=100" in text
        assert "经销商名称=B经销商" in text
        assert "共2行" in text
        assert "无概要任务" in text
        assert "坏行" not in text

    def test_result_section_empty_without_tasks(self):
        assert build_result_section(None) == ""
        assert build_result_section({"question": "x", "tasks": []}) == ""

    def test_build_context_text_assembles_sections(self):
        text = build_context_text(
            current_question="就看华东那几家",
            question_log=[_entry("查2025年经销商名单", "m1")],
            task_table={"question": "查名单", "tasks": []},
            extra_sections={"结构化参数": "指标：含税金额"},
        )
        assert text.index("【近期用户问题】") < text.index("【本轮用户问题】")
        assert "【本轮用户问题】\n就看华东那几家" in text
        assert "【结构化参数】" in text
        # 概要里没有任务行时结果区块不应出现，避免"结果"区块空壳误导模型
        assert "【上一轮查询结果】" not in text


class TestDecide:
    @staticmethod
    def _complete(payload: str):
        async def complete(_instruction: str, _context_text: str) -> str:
            return payload

        return complete

    def test_prompt_bans_fabricating_objects_beyond_sample(self):
        from app.clarification.decision import CLARIFY_DECISION_PROMPT

        # 指代替换仅限采样里出现过的值，采样不足时转澄清，禁止编造名单
        assert "采样中出现过的具体值" in CLARIFY_DECISION_PROMPT
        assert "不得编造" in CLARIFY_DECISION_PROMPT
        # 默认值与必问项的边界写成明确规则，不靠模型自行拿捏
        assert "近12个月" in CLARIFY_DECISION_PROMPT
        assert "必须追问" in CLARIFY_DECISION_PROMPT

    def test_new_task(self):
        payload = json.dumps(
            {
                "relation": "NEW_TASK",
                "completed_question": "查询2025年销售明细",
                "reason": "用户换了话题",
            }
        )
        decision = asyncio.run(
            decide(current_question="查销售明细", context_text="x", complete=self._complete(payload))
        )
        assert decision.relation == "NEW_TASK"
        assert decision.completed_question == "查询2025年销售明细"

    def test_complete_with_question(self):
        payload = json.dumps(
            {
                "relation": "COMPLETE",
                "completed_question": "查询A经销商、B经销商2025年含税金额",
                "reason": "指代已用上轮结果值替换",
            }
        )
        decision = asyncio.run(
            decide(current_question="就看那几家", context_text="x", complete=self._complete(payload))
        )
        assert decision.relation == "COMPLETE"
        assert "A经销商" in decision.completed_question

    def test_clarify(self):
        payload = json.dumps(
            {
                "relation": "CLARIFY",
                "questions": ["要查哪个业务域？", "时间范围是？"],
                "reason": "缺业务域",
            }
        )
        decision = asyncio.run(
            decide(current_question="查金额", context_text="x", complete=self._complete(payload))
        )
        assert decision.relation == "CLARIFY"
        assert len(decision.questions) == 2

    def test_transport_error_propagates(self):
        async def broken_complete(_instruction: str, _context_text: str) -> str:
            raise RuntimeError("gateway 500")

        with pytest.raises(RuntimeError, match="gateway 500"):
            asyncio.run(
                decide(current_question="查金额", context_text="x", complete=broken_complete)
            )

    def test_malformed_output_raises_system_error(self):
        # 模型故障按系统失败抛错，由接线层走 SAFE_FALLBACK，不转用户澄清
        from app.clarification import ClarificationDecisionError

        with pytest.raises(ClarificationDecisionError):
            asyncio.run(
                decide(current_question="查金额", context_text="x", complete=self._complete("不是JSON"))
            )

    def test_relation_payload_mismatch_raises(self):
        # 说 COMPLETE 却没给问题、说 CLARIFY 却没给问题清单，都不猜
        from app.clarification import ClarificationDecisionError

        for payload in (
            json.dumps({"relation": "COMPLETE", "reason": "缺问题文本"}),
            json.dumps({"relation": "CLARIFY", "reason": "缺问题清单"}),
        ):
            with pytest.raises(ClarificationDecisionError):
                asyncio.run(
                    decide(current_question="查金额", context_text="x", complete=self._complete(payload))
                )

    def test_questions_capped_at_two(self):
        payload = json.dumps(
            {
                "relation": "CLARIFY",
                "questions": ["问1", "问2", "问3", "问4"],
                "reason": "r",
            }
        )
        decision = asyncio.run(
            decide(current_question="查金额", context_text="x", complete=self._complete(payload))
        )
        assert len(decision.questions) == 2
