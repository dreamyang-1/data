from datetime import datetime, timezone
import json

import pytest

from app.presentation.catalog_diagnostics import catalog_configuration_answer
from app.domain.models import ChatRequest, TrustedIdentity
from app.semantic_v2.context_state_store import RedisContextStateStore
from app.semantic_v2.context_v1_execution import V2ContextV1ExecutionBridge


IDENTITY = TrustedIdentity(tenant_id="tenant", user_id="user", roles=[])


def request(**updates):
    return ChatRequest(**(dict(application_id="app", conversation_id="conversation", message_id="message",
        question="Sales", semantic_model_id=81, business_domain_ids=[205]) | updates))


class DeploymentRedis:
    """Only the atomic state-store operations used by these two bridge tests."""
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, key_count, *args):
        if key_count == 1:
            if len(args) == 3:
                key, encoded, _ttl = args
                if key in self.values: return 0
                self.values[key] = encoded
                return 1
            key, expected, encoded, _ttl = args
            current = json.loads(self.values[key]).get("revision", 0) if key in self.values else 0
            if current != int(expected): return 0
            self.values[key] = encoded
            return 1
        assert key_count == 2
        state, guard, expected, encoded, _ttl, marker, _guard_ttl = args
        current = json.loads(self.values[state]).get("revision", 0) if state in self.values else 0
        if current != int(expected): return 0
        if guard in self.values: return -1
        self.values[state], self.values[guard] = encoded, marker
        return 1

    async def delete(self, *keys):
        for key in keys: self.values.pop(key, None)
        return len(keys)

    async def aclose(self):
        return None


def handler(provider, redis, v1):
    class NoModel:
        async def complete(self, **_kwargs):
            raise AssertionError("invalid catalog must not reach a model")

    return V2ContextV1ExecutionBridge(
        store=RedisContextStateStore(redis, prefix="youo:data-analysis:v2-context-live:catalog-diagnostics-test", ttl_seconds=3600,
            idempotency_ttl_seconds=7200), catalog=provider[0], model=NoModel(), v1_executor=v1,
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
        startup_receipt={"runtime_mode": "V2_CONTEXT_V1_EXECUTION"})


class CatalogFailure(ValueError):
    def __init__(self, *issues):
        super().__init__("CATALOG_VALUE_SOURCE_MAPPING_INVALID")
        self.issues = issues


def render(exc, domains=(205,)):
    return catalog_configuration_answer(exc, semantic_model_id=81, business_domain_ids=domains)


@pytest.mark.parametrize("key,location", [
    ("entity_code", "semantic_model_entity_type.code"),
    ("attr_code", "semantic_model_attribute_config.code"),
    ("mapping_column", "semantic_model_attribute_config.mapping_column"),
    ("relation_code", "semantic_model_relation_config.code"),
    ("main_join_column", "semantic_model_entity_sub_table_mapping.main_join_column"),
    ("sub_join_column", "semantic_model_entity_sub_table_mapping.sub_join_column"),
    ("indicator_code", "semantic_model_indicator.indicator_code"),
    ("dependence_atomic_indicator", "semantic_model_indicator.dependence_atomic_indicator"),
    ("dim_code", "semantic_model_dimension.code"),
    ("main_table_name", "semantic_model_entity_type.main_table_name"),
])
def test_precise_object_and_configuration_location(key, location):
    answer = render(CatalogFailure(dict(key=key, reason="MISSING", entity_name="医院", entity_id="entity-1",
        attribute_id="attr-1", attr_name="城市", mapping_table="hospitals", mapping_column="city")))
    assert location in answer and "医院" in answer and "entity-1" in answer
    assert "hospitals.city" in answer and "为空或缺失" in answer and "处理：" in answer
    assert "尚未执行 SQL" in answer


def test_legacy_code_without_details_does_not_invent_a_field():
    answer = render(RuntimeError("CATALOG_VALUE_SOURCE_MAPPING_INVALID"), domains=())
    assert "未提供具体对象明细" in answer and "无法确认是哪个字段或关系" in answer
    assert "模型 ID：81" in answer and "当前模型范围" in answer


def test_unreviewed_keys_values_raw_sql_and_secrets_are_not_echoed():
    answer = render(CatalogFailure(
        dict(key="attr_code", reason="MISSING", entity_name="[click](https://secret)",
             attr_name="SELECT *; password=secret", mapping_table="ok", mapping_column="col", password="hidden"),
        dict(key="password", reason="MISSING", entity_name="hidden"),
        dict(key="attr_code", reason="raw private exception"),
        dict(key="host", reason="MISSING", host="private-db.invalid", data_source_id=7),
    ))
    for private in ("https://secret", "SELECT *", "password=secret", "hidden", "private-db.invalid", "raw private exception"):
        assert private not in answer
    assert "数据源配置 host" in answer and "数据源「7」" in answer


def test_foreign_domain_and_unsupported_issue_contract_are_not_rendered():
    answer = render(CatalogFailure(dict(key="attr_code", reason="MISSING", business_domain_id=206, entity_name="foreign")))
    assert "foreign" not in answer and "未提供具体对象明细" in answer
    exc = CatalogFailure(); exc.issues = "not-a-list"
    assert "未提供具体对象明细" in render(exc)


def test_many_issues_are_bounded_and_not_silently_dropped():
    answer = render(CatalogFailure(*(dict(key="attr_code", reason="MISSING", attribute_id=i) for i in range(60))))
    assert "60 项配置问题" in answer and "另有 10 项" in answer
    assert "属性「59」" not in answer


@pytest.mark.asyncio
async def test_bridge_preserves_detailed_error_and_idempotent_saved_response():
    class InvalidCatalog:
        def for_request(self, *_):
            raise CatalogFailure(dict(key="attr_code", reason="MISSING", entity_code="hospital", attr_name="城市",
                attribute_id="1205", mapping_table="hospitals", mapping_column="city", business_domain_id=205))

    async def forbidden_v1(*_):
        raise AssertionError("invalid catalog must not execute SQL/V1")

    bridge = handler((InvalidCatalog(),), DeploymentRedis(), forbidden_v1)
    chat = request(message_id="catalog-detailed-error")
    result = await bridge.handle(chat, IDENTITY)
    assert result.status == "SAFE_FALLBACK" and result.error_code == "SEMANTIC_CATALOG_INVALID"
    assert "hospitals.city" in result.answer and "1205" in result.answer
    saved = await bridge.store.load(chat, IDENTITY)
    assert saved.message(chat.message_id)["response"]["answer"] == result.answer
    repeated = await bridge.handle(chat, IDENTITY)
    assert repeated.answer == result.answer


def test_stream_returns_detailed_configuration_as_answer_not_transport_error():
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    class InvalidCatalog:
        def for_request(self, *_):
            raise CatalogFailure(dict(key="relation_code", reason="MISSING", entity_code="dealer",
                relation_name="经销商品", target_entity="product", business_domain_id=205))

    async def forbidden_v1(*_):
        raise AssertionError("invalid catalog must not reach V1")

    bridge = handler((InvalidCatalog(),), DeploymentRedis(), forbidden_v1)
    settings = Settings(_env_file=None, env="test", adapter_mode="mock", session_store_mode="memory",
        long_term_memory_mode="disabled", business_question_collection_enabled=False,
        allow_missing_trusted_identity_headers=True, trusted_backend_token="test-token")
    with TestClient(create_app(settings, isolated_chat_handler=bridge)) as client:
        response = client.post("/agent_chat/stream", json=dict(application_id="catalog-diagnostics",
            conversation_id="catalog-diagnostics-stream", message_id="catalog-diagnostics-stream-msg",
            question="查询合作商品", semantic_model_id=81, business_domain_ids=[205]),
            headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    assert "semantic_model_relation_config.code" in response.text
    assert "经销商品" in response.text and "product" in response.text
    assert '"type": "complete"' in response.text and '"type": "error"' not in response.text
    assert '"error_code": "SEMANTIC_CATALOG_INVALID"' in response.text
