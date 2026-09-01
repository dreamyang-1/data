from __future__ import annotations

import json

import httpx
import pytest

from evals.run_live_medical_scenarios import (
    CASES_PATH,
    assess,
    call_stream,
    validate_cases,
)


def _sse(*events: dict) -> str:
    return "".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events
    )


def test_stream_call_validates_complete_answer_and_output_chunks() -> None:
    body = _sse(
        {"type": "updata_state", "data": "accepted"},
        {"type": "message_chunk", "step": "output", "content": "查询"},
        {"type": "message_chunk", "step": "output", "content": "完成"},
        {"type": "answer", "step": "output", "content": "查询完成"},
        {
            "type": "complete",
            "conversation_id": "conversation-1",
            "status": "COMPLETED",
            "intent": "DETAIL_QUERY",
            "answer": "查询完成",
            "evidence": [],
        },
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )
    )

    with httpx.Client(transport=transport) as client:
        payload, _, status_code = call_stream(
            client,
            "http://agent/agent_chat/stream",
            {},
            "conversation-1",
            "测试",
        )

    assert status_code == 200
    assert payload["_transport_failures"] == []
    ok, failures = assess(
        {"expected_intents": ["DETAIL_QUERY"]},
        payload,
        status_code,
        "conversation-1",
    )
    assert ok is True
    assert failures == []


def test_stream_call_rejects_missing_complete_event() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=_sse({"type": "updata_state", "data": "accepted"}),
        )
    )

    with httpx.Client(transport=transport) as client:
        payload, _, _ = call_stream(
            client,
            "http://agent/agent_chat/stream",
            {},
            "conversation-1",
            "测试",
        )

    assert "COMPLETE_EVENT_COUNT_0" in payload["_transport_failures"]


def test_assess_can_require_a_non_empty_ground_truth_query() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "DETAIL_QUERY",
        "answer": "没有结果",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {
                    "row_count": 0,
                    "quality_status": "PASS",
                    "result_fingerprint": "snapshot-1",
                    "data_as_of": "2026-08-28T00:00:00Z",
                    "source_data_as_of": "2025-12-30",
                    "source_watermark_field": "sales_order.created_date",
                },
            }
        ],
    }
    ok, failures = assess(
        {
            "expected_intents": ["DETAIL_QUERY"],
            "required_evidence": ["QUERY_RESULT"],
            "minimum_query_rows": 1,
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["QUERY_ROWS_BELOW_1"]


def test_assess_does_not_require_source_watermark_for_snapshot_query() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "DETAIL_QUERY",
        "answer": "查询完成",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {
                    "row_count": 1,
                    "quality_status": "PASS",
                    "result_fingerprint": "snapshot-1",
                    "data_as_of": "2026-08-28T00:00:00Z",
                },
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["DETAIL_QUERY"],
            "required_evidence": ["QUERY_RESULT"],
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is True
    assert failures == []


def test_assess_requires_source_watermark_for_temporal_query() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "TREND_ANALYSIS",
        "answer": "趋势查询完成",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {
                    "row_count": 3,
                    "quality_status": "PASS",
                    "result_fingerprint": "snapshot-1",
                    "data_as_of": "2026-08-28T00:00:00Z",
                },
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["TREND_ANALYSIS"],
            "required_evidence": ["QUERY_RESULT"],
            "require_source_watermark": True,
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["MISSING_QUERY_SOURCE_WATERMARK"]


def test_assess_requires_the_requested_analysis_granularity() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "TREND_ANALYSIS",
        "answer": "趋势查询完成",
        "evidence": [
            {
                "kind": "ANALYSIS_RESULT",
                "payload": {"facts": {"actual_granularity": "day"}},
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["TREND_ANALYSIS"],
            "expected_analysis_granularity": "month",
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["ANALYSIS_GRANULARITY_EXPECTED_month_ACTUAL_day"]


def test_assess_accepts_the_requested_analysis_granularity() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "TREND_ANALYSIS",
        "answer": "趋势查询完成",
        "evidence": [
            {
                "kind": "ANALYSIS_RESULT",
                "payload": {"facts": {"actual_granularity": "month"}},
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["TREND_ANALYSIS"],
            "expected_analysis_granularity": "month",
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is True
    assert failures == []


def test_assess_can_require_every_report_query_to_be_non_empty() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "REPORT_GENERATION",
        "answer": "报告完成",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {
                    "row_count": row_count,
                    "quality_status": "PASS",
                    "result_fingerprint": f"snapshot-{index}",
                    "data_as_of": "2026-08-28T00:00:00Z",
                    "source_data_as_of": "2025-12-30",
                    "source_watermark_field": "sales_order.created_date",
                },
            }
            for index, row_count in enumerate((3, 0, 2), start=1)
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["REPORT_GENERATION"],
            "required_evidence": ["QUERY_RESULT"],
            "minimum_query_evidence": 3,
            "minimum_each_query_rows": 1,
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["QUERY_ROWS_EACH_BELOW_1"]


def test_assess_can_require_a_generated_report_file() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "REPORT_GENERATION",
        "answer": "分析完成，但文件生成失败",
        "evidence": [],
        "files": [],
    }

    ok, failures = assess(
        {
            "expected_intents": ["REPORT_GENERATION"],
            "minimum_files": 1,
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["FILES_BELOW_1"]


def test_case_validation_rejects_required_clarification_without_follow_up() -> None:
    cases = [
        {
            "id": f"Q{index:02d}",
            "question": "测试",
            "expected_intents": ["DETAIL_QUERY"],
        }
        for index in range(19)
    ]
    cases[0]["clarification"] = "required"

    with pytest.raises(ValueError, match="needs follow_up"):
        validate_cases(cases)


def test_checked_in_medical_cases_have_valid_business_contracts() -> None:
    cases = validate_cases(json.loads(CASES_PATH.read_text(encoding="utf-8")))

    assert len(cases) == 19
    assert next(case for case in cases if case["id"] == "Q09")[
        "expected_analysis_granularity"
    ] == "month"
    assert next(case for case in cases if case["id"] == "Q10")[
        "expected_analysis_granularity"
    ] == "month"


def test_assess_ranking_contract_rejects_rows_without_ranking_facts() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "COMPARISON_ANALYSIS",
        "answer": "返回了三行经销商数据",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {
                    "columns": ["dealer_name", "total_sales"],
                    "row_count": 3,
                },
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["COMPARISON_ANALYSIS"],
            "analysis_contract": {
                "type": "ranking",
                "direction": "descending",
                "mode": "BOUNDED_TOP_N",
                "exact_count": 3,
            },
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["ANALYSIS_RANKING_CONTRACT_MISSING"]


def test_assess_ranking_contract_checks_metric_and_actual_order() -> None:
    facts = {
        "metric_column": "total_sales",
        "label_column": "dealer_name",
        "ranking_limit": None,
        "descending": True,
        "ranking_mode": "FULL_ORDERED_SET",
        "returned_object_count": 2,
        "profile_columns": [],
        "rankings": [
            {"rank": 1, "label": "甲", "value": 10, "profile": {}},
            {"rank": 2, "label": "乙", "value": 20, "profile": {}},
        ],
    }
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "COMPARISON_ANALYSIS",
        "answer": "已按销售额排序",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {"columns": ["dealer_name", "total_sales"]},
            },
            {"kind": "ANALYSIS_RESULT", "payload": {"facts": facts}},
        ],
    }
    case = {
        "expected_intents": ["COMPARISON_ANALYSIS"],
        "analysis_contract": {
            "type": "ranking",
            "direction": "descending",
            "mode": "FULL_ORDERED_SET",
        },
    }

    ok, failures = assess(case, payload, 200, "conversation-1")
    assert ok is False
    assert failures == ["RANKING_VALUES_NOT_SORTED"]

    facts["rankings"].reverse()
    facts["rankings"][0]["rank"] = 1
    facts["rankings"][1]["rank"] = 2
    ok, failures = assess(case, payload, 200, "conversation-1")
    assert ok is True
    assert failures == []


def test_assess_bounded_ranking_accepts_source_backed_shortfall_disclosure() -> None:
    facts = {
        "metric_column": "total_sales",
        "label_column": "dealer_name",
        "ranking_limit": 3,
        "requested_object_count": 3,
        "ranking_shortfall": 2,
        "descending": True,
        "ranking_mode": "BOUNDED_TOP_N",
        "returned_object_count": 1,
        "profile_columns": [],
        "rankings": [
            {"rank": 1, "label": "甲", "value": 10, "profile": {}},
        ],
    }
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "COMPARISON_ANALYSIS",
        "answer": "当前仅有1个对象满足条件，不足请求的前3名。",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {"columns": ["dealer_name", "total_sales"]},
            },
            {"kind": "ANALYSIS_RESULT", "payload": {"facts": facts}},
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["COMPARISON_ANALYSIS"],
            "analysis_contract": {
                "type": "ranking",
                "direction": "descending",
                "mode": "BOUNDED_TOP_N",
                "minimum_count": 1,
                "maximum_count": 3,
                "require_shortfall_disclosure": True,
            },
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is True
    assert failures == []


def test_assess_report_contract_rejects_three_copies_of_one_query() -> None:
    proof = {
        "columns": ["period", "sales"],
        "quality_status": "PASS",
        "result_fingerprint": "same-query",
        "data_as_of": "2026-08-28T00:00:00Z",
    }
    sections = [
        {
            "task_id": f"task-{index}",
            "status": "COMPLETED",
            "dataset_id": "same-dataset",
            "query_proofs": [proof],
        }
        for index in range(1, 4)
    ]
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "REPORT_GENERATION",
        "answer": "报告完成",
        "dataset_ids": ["same-dataset"],
        "files": [{"dataset_ids": ["same-dataset"]}],
        "evidence": [
            {
                "kind": "REPORT_DATASET_MANIFEST",
                "payload": {
                    "section_count": 3,
                    "available_section_count": 3,
                    "fully_completed_section_count": 3,
                    "sections": sections,
                },
            }
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["REPORT_GENERATION"],
            "report_contract": {
                "section_count": 3,
                "require_all_completed": True,
                "require_distinct_datasets": True,
                "require_distinct_query_fingerprints": True,
                "require_file_dataset_coverage": True,
            },
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == [
        "REPORT_DATASETS_NOT_DISTINCT",
        "REPORT_QUERY_FINGERPRINTS_NOT_DISTINCT",
    ]


def test_assess_web_contract_rejects_uncovered_ranked_entities() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "COMPARISON_ANALYSIS",
        "answer": "TOP2画像",
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {"columns": ["dealer_name", "score"]},
            },
            {
                "kind": "ANALYSIS_RESULT",
                "payload": {
                    "facts": {
                        "metric_column": "score",
                        "label_column": "dealer_name",
                        "descending": True,
                        "ranking_mode": "BOUNDED_TOP_N",
                        "returned_object_count": 2,
                        "profile_columns": [],
                        "rankings": [
                            {"rank": 1, "label": "甲公司", "value": 2, "profile": {}},
                            {"rank": 2, "label": "乙公司", "value": 1, "profile": {}},
                        ],
                    }
                },
            },
            {
                "kind": "WEB_SEARCH_RESULT",
                "payload": {
                    "records": [
                        {"title": "甲公司官网", "url": "https://a.example", "snippet": "甲公司介绍"},
                        {"title": "行业信息", "url": "https://b.example", "snippet": "其他企业"},
                    ]
                },
            },
        ],
    }

    ok, failures = assess(
        {
            "expected_intents": ["COMPARISON_ANALYSIS"],
            "analysis_contract": {
                "type": "ranking",
                "direction": "descending",
                "mode": "BOUNDED_TOP_N",
                "exact_count": 2,
            },
            "web_contract": {
                "minimum_records": 2,
                "require_record_urls": True,
                "require_ranking_label_coverage": True,
            },
        },
        payload,
        200,
        "conversation-1",
    )

    assert ok is False
    assert failures == ["WEB_RANKING_LABEL_COVERAGE_1_OF_2"]


def test_assess_non_null_group_preview_rejects_blank_hospital_group() -> None:
    payload = {
        "conversation_id": "conversation-1",
        "status": "COMPLETED",
        "intent": "METRIC_QUERY",
        "answer": '查询结果共2行：[{"hospital_name": null, "order_count": 2}, {"hospital_name": "甲医院", "order_count": 1}]',
        "evidence": [
            {
                "kind": "QUERY_RESULT",
                "payload": {"columns": ["hospital_name", "order_count"]},
            }
        ],
    }
    case = {
        "expected_intents": ["METRIC_QUERY"],
        "required_query_column_concepts": {
            "hospital_name": ["hospitalname", "医院名称", "=医院"]
        },
        "non_null_preview_column_concepts": {
            "hospital_name": ["hospitalname", "医院名称", "=医院"]
        },
    }

    ok, failures = assess(case, payload, 200, "conversation-1")
    assert ok is False
    assert failures == ["GROUP_PREVIEW_NULL_hospital_name"]

    payload["answer"] = payload["answer"].replace("null", '"乙医院"')
    ok, failures = assess(case, payload, 200, "conversation-1")
    assert ok is True
    assert failures == []
