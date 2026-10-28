"""任务表跨轮传递：store 读写、结果概要压缩、落盘组装、上下文瘦身。"""

import asyncio
from types import SimpleNamespace

from app.domain.models import Dataset
from app.semantic_v2.context_proposal import _trim_prior_task_table
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore


def test_inmemory_task_table_roundtrip():
    sessions = InMemorySessionStore()
    table = {"message_id": "m-1", "question": "各经销商销量", "tasks": [{"任务": "task-1"}]}
    asyncio.run(sessions.put_task_table("t", "u", "app", "conv", table))
    loaded = asyncio.run(sessions.get_task_table("t", "u", "app", "conv"))
    assert loaded == table
    assert asyncio.run(sessions.get_task_table("t", "u", "app", "other")) is None


def test_summarize_query_result_keeps_columns_and_sample():
    dataset = Dataset(
        columns=["经销商", "_internal", "销量"],
        rows=[{"经销商": "A", "_internal": "x", "销量": 10},
              {"经销商": "B", "_internal": "y", "销量": 20}],
        row_count=2, snapshot_id="s", total_row_count=2,
        data_as_of="2026-10-16T00:00:00+00:00",
    )
    summary = DataAnalysisOrchestrator._summarize_query_result(dataset)
    assert summary["columns"] == ["经销商", "销量"]
    assert summary["row_count"] == 2
    assert summary["sample"][0] == {"经销商": "A", "销量": "10"}


def test_summarize_query_result_drops_sample_when_oversize():
    # 50列 × 80字符/值的采样总长超 3000，整份采样清掉只留行列数
    columns = [f"c{i}" for i in range(50)]
    dataset = Dataset(
        columns=columns,
        rows=[{column: "值" * 80 for column in columns}],
        row_count=1, snapshot_id="s", total_row_count=1,
        data_as_of="2026-10-16T00:00:00+00:00",
    )
    summary = DataAnalysisOrchestrator._summarize_query_result(dataset)
    assert "sample" not in summary
    assert summary["row_count"] == 1


def test_persist_conversation_task_table_assembles_payload():
    sessions = InMemorySessionStore()
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.sessions = sessions
    chat = SimpleNamespace(
        message_id="m-2", rewritten_question=None,
        original_question="上月各经销商含税销售额", question="上月各经销商含税销售额",
        application_id="app", conversation_id="conv",
        authorized_semantic_scope=SimpleNamespace(fingerprint=lambda: "fp-test"),
        _task_table=[
            {"任务": "task-1", "名称": "查询上月各经销商含税销售额",
             "是否查询数据库": "是", "执行状态": "已完成"},
            {"任务": "task-2", "名称": "取排名前5", "是否查询数据库": "否（复用上一轮查询结果）",
             "执行状态": "已完成"},
        ],
        _task_result_summaries={
            "task-1": {"summary": {"columns": ["经销商"], "row_count": 8}, "dataset_id": "ds-1"},
        },
        _last_result_summary={"columns": ["经销商"], "row_count": 5},
        _last_result_dataset_id="ds-2",
    )
    asyncio.run(orchestrator._persist_conversation_task_table(chat, SimpleNamespace(tenant_id="t", user_id="u")))
    payload = asyncio.run(sessions.get_task_table("t", "u", "app", "conv"))
    assert payload["message_id"] == "m-2"
    assert payload["question"] == "上月各经销商含税销售额"
    assert payload["tasks"][0]["结果概要"] == {"columns": ["经销商"], "row_count": 8}
    assert payload["tasks"][0]["数据集ID"] == "ds-1"
    # task-2 没有独立查询，不该带兜底概要之外的伪数据
    assert "结果概要" not in payload["tasks"][1]
    # 汇总非空时不用 last_result 兜底
    assert "last_result" not in payload


def test_persist_conversation_task_table_single_task_fallback():
    sessions = InMemorySessionStore()
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.sessions = sessions
    chat = SimpleNamespace(
        message_id="m-3", rewritten_question=None, original_question="查销量", question="查销量",
        application_id="app", conversation_id="conv",
        authorized_semantic_scope=SimpleNamespace(fingerprint=lambda: "fp-test"),
        _task_table=[{"任务": "task-1", "名称": "查销量", "是否查询数据库": "是", "执行状态": "已完成"}],
        _task_result_summaries={},
        _last_result_summary={"columns": ["销量"], "row_count": 3},
        _last_result_dataset_id="ds-9",
    )
    asyncio.run(orchestrator._persist_conversation_task_table(chat, SimpleNamespace(tenant_id="t", user_id="u")))
    payload = asyncio.run(sessions.get_task_table("t", "u", "app", "conv"))
    # 单任务轮兜底概要挂到唯一任务行上，模型从 tasks 里统一读
    assert payload["tasks"][0]["结果概要"] == {"columns": ["销量"], "row_count": 3}
    assert payload["tasks"][0]["数据集ID"] == "ds-9"
    assert "last_result" not in payload


def test_persist_conversation_task_table_without_table_is_noop():
    sessions = InMemorySessionStore()
    orchestrator = object.__new__(DataAnalysisOrchestrator)
    orchestrator.sessions = sessions
    chat = SimpleNamespace(
        message_id="m-4", rewritten_question=None, original_question="你好", question="你好",
        application_id="app", conversation_id="conv",
        _task_table=None, _task_result_summaries={},
        _last_result_summary=None, _last_result_dataset_id=None,
    )
    asyncio.run(orchestrator._persist_conversation_task_table(chat, SimpleNamespace(tenant_id="t", user_id="u")))
    assert asyncio.run(sessions.get_task_table("t", "u", "app", "conv")) is None


def test_trim_prior_task_table_keeps_small_table():
    table = {"message_id": "m", "tasks": [{"任务": "task-1", "结果概要": {"sample": [1]}}]}
    assert _trim_prior_task_table(table) is table


def test_trim_prior_task_table_drops_sample_then_summary():
    big_sample = [{"v": "x" * 2000}]
    table = {
        "message_id": "m",
        "tasks": [
            {"任务": "task-1", "结果概要": {"columns": ["c"], "sample": big_sample}},
            {"任务": "task-2", "结果概要": {"columns": ["c"]}},
        ],
    }
    trimmed = _trim_prior_task_table(table, budget=800)
    assert "sample" not in trimmed["tasks"][0]["结果概要"]
    assert trimmed["tasks"][1]["结果概要"]["columns"] == ["c"]
    trimmed2 = _trim_prior_task_table(table, budget=120)
    assert "结果概要" not in trimmed2["tasks"][0]
    assert trimmed2["tasks"][0]["任务"] == "task-1"
