from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.domain.models import ChatRequest, DataQueryResult, Dataset, PrimaryIntent, TrustedIdentity
from app.services.result_cleanup import clean_name_list, cleanup_message
from app.services.orchestrator import DataAnalysisOrchestrator
from app.services.progress import progress_scope
from test_orchestrator import service


def result(rows, columns=None, **kwargs):
    return DataQueryResult(
        asl={"metrics": [], "dimensions": [{"name": "hospital.hospital_name"}], "filters": []},
        sql="SELECT hospital_name FROM hospital",
        dataset=Dataset(columns=columns or ["医院名称"], rows=rows, row_count=len(rows),
                        snapshot_id="cleanup-test", data_as_of=datetime.now(timezone.utc), **kwargs),
    )


def test_duplicate_and_missing_names_cleaned_without_mutating_source():
    original = result([{"医院名称": value} for value in ["甲医院", "甲医院", " 乙医院 ", None, "—", "", "NULL", "未知", "乙医院"]])
    before = original.model_dump()
    cleaned = clean_name_list(PrimaryIntent.DETAIL_QUERY, original)
    assert cleaned.dataset.rows == [{"医院名称": v} for v in ["甲医院", "乙医院", "未知"]]
    assert cleaned.dataset.total_row_count == cleaned.dataset.row_count == 3
    assert cleaned.dataset.snapshot_id != original.dataset.snapshot_id
    assert original.model_dump() == before
    assert "去除 2 行完全重复记录、4 行空名称" in cleanup_message(cleaned)
    assert clean_name_list(PrimaryIntent.DETAIL_QUERY, cleaned) is cleaned


def test_same_name_different_identity_and_partial_names_retained():
    rows = [{"医院名称": "甲医院", "医院编码": code, "科室名称": dept}
            for code, dept in [("01", None), ("02", None), ("01", "内科")]]
    original = result(rows, list(rows[0]))
    assert clean_name_list(PrimaryIntent.DETAIL_QUERY, original) is original


def test_same_named_hospitals_show_distinguishing_codes_in_final_table():
    rows = [{"医院名称": "甲医院", "医院编码": code} for code in ["01", "02"]]
    table = DataAnalysisOrchestrator._markdown_result_table(list(rows[0]), rows)
    assert "医院编码" in table
    assert "01" in table and "02" in table


@pytest.mark.parametrize("intent", [PrimaryIntent.METRIC_QUERY, PrimaryIntent.TREND_ANALYSIS, PrimaryIntent.ANOMALY_ANALYSIS])
def test_non_detail_intents_unchanged(intent):
    original = result([{"医院名称": "甲医院"}] * 2)
    assert clean_name_list(intent, original) is original


@pytest.mark.parametrize("amount", [0, -500, 100000000000])
def test_transaction_facts_and_numeric_outliers_not_removed(amount):
    original = result([{"医院名称": "甲医院", "销售额": amount}] * 2, ["医院名称", "销售额"])
    assert clean_name_list(PrimaryIntent.DETAIL_QUERY, original) is original


def test_metrics_in_asl_prevent_cleanup_even_with_detail_intent():
    original = result([{"医院名称": "甲医院"}] * 2)
    original.asl["metrics"] = [{"name": "sales"}]
    assert clean_name_list(PrimaryIntent.DETAIL_QUERY, original) is original


@pytest.mark.parametrize("truncated", [True, False])
def test_incomplete_result_keeps_raw_file_and_does_not_claim_global_distinct_total(truncated):
    original = result([{"医院名称": "甲医院"}] * 2, total_row_count=100, truncated=truncated)
    original.result_file_url = "https://example.invalid/raw.csv"
    cleaned = clean_name_list(PrimaryIntent.DETAIL_QUERY, original)
    assert cleaned.dataset.truncated
    assert cleaned.dataset.total_row_count == cleaned.dataset.row_count == 1
    assert cleaned.execution_transforms[-1]["source_total_row_count"] == 100
    assert cleaned.result_file_url == original.result_file_url
    assert "全量去重后数量未知" in cleanup_message(cleaned)
    assert "附件是上游原始完整结果" in cleanup_message(cleaned)


def test_complete_cleaned_list_does_not_offer_raw_export():
    original = result([{"医院名称": "甲医院"}] * 2)
    original.result_file_url = "https://example.invalid/raw.csv"
    assert clean_name_list(PrimaryIntent.DETAIL_QUERY, original).result_file_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [["甲医院", "甲医院", None, "—"], [None, "—"]])
async def test_cleanup_in_real_completion_path_persistence_evidence_and_progress(values):
    orchestrator = service()
    orchestrator.settings.surface_asl_execution_enabled = True
    async def query_surface(request, identity, *, mentions):
        return result([{"医院名称": v} for v in values])
    orchestrator.adapters.query.retrieval.query_surface = query_surface
    orchestrator._persist_query_dataset = AsyncMock(return_value="cleaned-dataset")
    chat = ChatRequest(question="请提供南京市哪些医院使用费森尤斯产品。", conversation_id="cleanup-test",
                       semantic_model_id=81, message_id="m1", application_id="app")
    chat._completed_question_execution = True
    events = []
    with progress_scope(events.append):
        response = await orchestrator._handle(chat, TrustedIdentity(tenant_id="t", user_id="u"))
    assert response.status == "COMPLETED"
    assert "名单清理" in response.answer
    proof = next(item for item in response.evidence if item.kind == "RESULT_CLEANUP")
    assert proof.payload["invalid_rows_removed"] == 2
    if values[0]:
        persisted = orchestrator._persist_query_dataset.call_args.args[1]
        assert persisted.dataset.rows == [{"医院名称": "甲医院"}]
        assert response.dataset_id == "cleaned-dataset"
        stages = [event["stage"] for event in events]
        assert stages.index("DATA_RETRIEVAL") < stages.index("RELIABILITY_CHECK") < stages.index("INSIGHT_ANALYSIS")
    else:
        orchestrator._persist_query_dataset.assert_not_called()
        assert "清理后没有可展示的有效名称" in response.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [["甲医院", "甲医院", "—"], [None, "—"], ["甲医院"]])
async def test_partial_preview_cleanup_never_imports_raw_file_or_claims_empty_database(values):
    orchestrator = service()
    orchestrator.settings.surface_asl_execution_enabled = True
    async def query_surface(request, identity, *, mentions):
        source = result([{"医院名称": v} for v in values], truncated=True, total_row_count=100)
        source.result_file_url = "https://example.invalid/raw.csv"
        return source
    orchestrator.adapters.query.retrieval.query_surface = query_surface
    orchestrator._import_query_result_file = AsyncMock(return_value=None)
    orchestrator._persist_query_dataset = AsyncMock()
    chat = ChatRequest(question="请提供南京市哪些医院使用费森尤斯产品。", conversation_id="cleanup-partial",
                       semantic_model_id=81, message_id="m1", application_id="app")
    chat._completed_question_execution = True
    response = await orchestrator._handle(chat, TrustedIdentity(tenant_id="t", user_id="u"))
    assert "没有找到匹配的业务记录" not in response.answer
    if len(values) > 1:
        assert "全量去重后数量未知" in response.answer
        assert "附件是上游原始完整结果" in response.answer
        orchestrator._import_query_result_file.assert_not_called()
    else:
        assert "名单清理" not in response.answer
        orchestrator._import_query_result_file.assert_awaited_once()
    orchestrator._persist_query_dataset.assert_not_called()
