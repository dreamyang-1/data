from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from minio_followup_store import DatasetReference, LoadedDataset

from app.adapters.base import AdapterBundle
from app.adapters.mock import build_mock_adapters
from app.adapters.semantic_query import CompositeSemanticQueryTool
from app.config import Settings
from app.domain.models import (
    CanonicalAnalysisRequest,
    ChatRequest,
    DataQueryResult,
    Dataset,
    PrimaryIntent,
    TrustedIdentity,
    TurnRelation,
)
from app.intent import RuleBasedIntentClassifier
from app.services import DataAnalysisOrchestrator
from app.stores import InMemorySessionStore
from app.stores.events import InMemorySessionEventStore, SessionEventType


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user")


def _filter(request: CanonicalAnalysisRequest, field: str) -> str | None:
    return next(
        (
            str(item.get("value"))
            for item in request.filters
            if str(item.get("field") or "") == field
        ),
        None,
    )


class _ResultStore:
    def __init__(self) -> None:
        self.items: dict[str, LoadedDataset] = {}
        self.count = 0

    def save_dataset(self, **kwargs):
        self.count += 1
        dataset_id = f"previous-result-{self.count}"
        now = datetime.now(timezone.utc)
        reference = DatasetReference(
            dataset_id=dataset_id,
            bucket="test",
            object_name=f"datasets/{dataset_id}.json.gz",
            scope=kwargs["scope"],
            columns=tuple(kwargs["columns"]),
            row_count=len(kwargs["rows"]),
            byte_size=100,
            snapshot_id=kwargs["snapshot_id"],
            data_as_of=kwargs["data_as_of"].isoformat(),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=1)).isoformat(),
            source_type=kwargs["source_type"],
            source_ref=kwargs["source_ref"],
            semantic_model_id=kwargs.get("semantic_model_id"),
            business_domain_ids=tuple(kwargs.get("business_domain_ids", [])),
            metric_ids=tuple(kwargs.get("metric_ids", [])),
        )
        self.items[dataset_id] = LoadedDataset(
            reference,
            tuple(dict(row) for row in kwargs["rows"]),
        )
        return reference

    def load_dataset(self, reference, *, current_scope):
        assert reference.scope == current_scope
        return self.items[reference.dataset_id]


class _CapturingRetrieval:
    def __init__(self) -> None:
        self.requests: list[CanonicalAnalysisRequest] = []

    async def health(self) -> bool:
        return True

    async def rewrite_health(self) -> bool:
        return True

    async def query(
        self,
        request: CanonicalAnalysisRequest,
        identity: TrustedIdentity,
        *,
        semantic_model_id: int | None,
        business_domain_id: int | None,
    ) -> DataQueryResult:
        self.requests.append(request.model_copy(deep=True))
        metric = (
            request.metrics[0].canonical_name or request.metrics[0].input
            if request.metrics else "销售额"
        )
        if request.primary_intent in {
            PrimaryIntent.TREND_ANALYSIS,
            PrimaryIntent.ROOT_CAUSE_ANALYSIS,
            PrimaryIntent.COMPARISON_ANALYSIS,
        }:
            rows = [
                {"交易日期": "2025-10", metric: 1_746_619.86},
                {"交易日期": "2025-11", metric: 319_528.97},
                {"交易日期": "2025-12", metric: 900_000.00},
            ]
        else:
            period = (
                request.time_range.start.strftime("%Y-%m")
                if request.time_range else "2025-11"
            )
            rows = [{"交易日期": period, metric: 100.0}]
        return DataQueryResult(
            asl={"version": "2.0", "intent": "query", "ambiguity": []},
            sql=f"SELECT period, value FROM sales_order /* call {len(self.requests)} */",
            dataset=Dataset(
                columns=list(rows[0]),
                rows=rows,
                row_count=len(rows),
                snapshot_id=f"snapshot-{len(self.requests)}",
                data_as_of=datetime(2025, 12, 30, tzinfo=timezone.utc),
            ),
            data_source_id="mock",
        )


def _agent():
    base = build_mock_adapters()
    retrieval = _CapturingRetrieval()
    sessions = InMemorySessionStore()
    events = InMemorySessionEventStore()
    agent = DataAnalysisOrchestrator(
        settings=Settings(
            env="test", adapter_mode="mock", intent_model_enabled=False
        ),
        classifier=RuleBasedIntentClassifier(),
        adapters=AdapterBundle(
            semantic=base.semantic,
            retrieval=retrieval,
            knowledge=base.knowledge,
            policy=base.policy,
            analysis=base.analysis,
            semantic_query=CompositeSemanticQueryTool(base.semantic, retrieval),
        ),
        sessions=sessions,
        dataset_store=_ResultStore(),
        event_store=events,
    )
    return agent, retrieval, sessions, events


async def _first_trend(agent, conversation_id: str):
    return await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id=conversation_id,
            message_id="m1",
            question="分析A产品2025年10月至12月销售趋势。",
            semantic_model_id=81,
        ),
        IDENTITY,
    )


@pytest.mark.asyncio
async def test_1_period_decline_reuses_previous_result_without_sql():
    agent, retrieval, _, events = _agent()
    first = await _first_trend(agent, "followup-1")
    assert first.status == "COMPLETED"

    second = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-1",
            message_id="m2",
            question="11月较10月下降多少？",
            semantic_model_id=81,
        ),
        IDENTITY,
    )

    assert second.status == "COMPLETED"
    assert len(retrieval.requests) == 1
    assert "1,427,090.89" in second.answer
    assert "81.71%" in second.answer
    assert second.evidence[0].payload["sql_executed"] is False
    trace = next(
        item for item in await events.list_events(
            "tenant", "user", "app", "followup-1", limit=100
        )
        if item.message_id == "m2"
        and item.event_type == SessionEventType.QUERY_RESOLUTION
    )
    assert trace.payload["turn_relation"] == "CURRENT_TOPIC_FOLLOWUP"
    assert trace.payload["resolved_periods"] == ["2025-11", "2025-10"]
    assert trace.payload["result_sufficiency"] is True
    assert trace.payload["execution_mode"] == "REUSE_PREVIOUS_RESULT"


@pytest.mark.asyncio
async def test_2_recovery_compares_with_immediately_preceding_period():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "followup-2")
    second = await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-2",
            message_id="m2",
            question="12月恢复了多少？",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    assert second.status == "COMPLETED"
    assert len(retrieval.requests) == 1
    assert "2025年12月较2025年11月" in second.answer
    assert "580,471.03" in second.answer


@pytest.mark.asyncio
async def test_3_why_decline_inherits_scope_but_requires_new_query():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "followup-3")
    await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-3",
            message_id="m2",
            question="为什么11月下降？",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    assert len(retrieval.requests) == 2
    request = retrieval.requests[-1]
    assert request.turn_relation == TurnRelation.CURRENT_TOPIC_FOLLOWUP
    assert request.primary_intent == PrimaryIntent.ROOT_CAUSE_ANALYSIS
    assert request.time_range.start.isoformat() == "2025-11-01"
    assert (_filter(request, "商品名称") or "").casefold() == "a"
    assert request.execution_mode == "QUERY_DATABASE"


@pytest.mark.asyncio
async def test_4_explicit_year_overrides_episode_year():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "followup-4")
    await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-4",
            message_id="m2",
            question="2024年11月呢？",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    request = retrieval.requests[-1]
    assert len(retrieval.requests) == 2
    assert request.time_range.start.isoformat() == "2024-11-01"
    assert (_filter(request, "商品名称") or "").casefold() == "a"


@pytest.mark.asyncio
async def test_5_explicit_metric_overrides_inherited_metric_but_month_uses_anchor():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "followup-5")
    await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-5",
            message_id="m2",
            question="11月销售量呢？",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    request = retrieval.requests[-1]
    assert len(retrieval.requests) == 2
    assert request.time_range.start.isoformat() == "2025-11-01"
    assert [item.input for item in request.metrics] == ["销售量"]
    assert (_filter(request, "商品名称") or "").casefold() == "a"


@pytest.mark.asyncio
async def test_6_complete_new_product_with_explicit_year_is_new_topic():
    agent, retrieval, _, _ = _agent()
    await _first_trend(agent, "followup-6")
    await agent.handle(
        ChatRequest(
            application_id="app",
            conversation_id="followup-6",
            message_id="m2",
            question="分析B产品2026年11月销售趋势。",
            semantic_model_id=81,
        ),
        IDENTITY,
    )
    request = retrieval.requests[-1]
    assert len(retrieval.requests) == 2
    assert request.turn_relation == TurnRelation.STANDALONE_NEW_TOPIC
    assert request.time_range.start.isoformat() == "2026-11-01"
    assert (_filter(request, "商品名称") or "").casefold() == "b"
    assert "'value': 'a'" not in str(request.filters).casefold()
