from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.models import ChatRequest, ToolConfig


def request_payload(**overrides):
    payload = {
        "application_id": "app-1",
        "conversation_id": "conversation-1",
        "message_id": "message-1",
        "question": "查询销售额",
    }
    payload.update(overrides)
    return payload


def test_web_search_flag_matches_new_agent_request_contract():
    assert ChatRequest(**request_payload()).web_search is False
    assert ChatRequest(**request_payload(web_search=True)).web_search is True


@pytest.mark.parametrize("name", ["sql翻译器", "自然语言提取AST", "inventory.lookup-v2"])
def test_tool_name_accepts_platform_unicode_identifiers(name):
    tool = ToolConfig(name=name, url="http://192.168.1.20/tool")

    assert tool.name == name


@pytest.mark.parametrize("name", ["sql 翻译器", "../translator", "sql/translator", "sql\\translator", "sql\ntranslator"])
def test_tool_name_rejects_unsafe_or_ambiguous_identifiers(name):
    with pytest.raises(ValidationError):
        ToolConfig(name=name, url="http://192.168.1.20/tool")


def test_chat_request_accepts_platform_tools_with_chinese_names():
    request = ChatRequest(
        **request_payload(
            semantic_model_id=6,
            tools=[
                {
                    "name": "sql翻译器",
                    "url": "http://192.168.1.175:48000/api/ast-to-sql",
                    "http_method": "post",
                    "inputSchema": {
                        "type": "object",
                        "required": ["asl"],
                        "properties": {"asl": {"type": "string"}},
                    },
                },
                {
                    "name": "自然语言提取AST",
                    "url": "http://192.168.1.49:8021/agent/query",
                    "http_method": "post",
                    "inputSchema": {
                        "type": "object",
                        "required": [
                            "query",
                            "semantic_model_id",
                            "business_domain_id",
                        ],
                    },
                },
            ],
        )
    )

    assert [tool.name for tool in request.tools] == ["sql翻译器", "自然语言提取AST"]


def test_tool_config_accepts_platform_time_out_alias():
    request = ChatRequest(
        **request_payload(
            tools=[
                {
                    "name": "inventory.lookup",
                    "url": "http://192.168.1.20/tool",
                    "time_out": 15,
                }
            ]
        )
    )

    assert request.tools[0].timeout == 15


def test_tool_config_ignores_unknown_platform_extension_fields():
    request = ChatRequest(
        **request_payload(
            tools=[
                {
                    "name": "inventory.lookup",
                    "url": "http://192.168.1.20/tool",
                    "time_out": 20,
                    "vendor_extension": {"trace": True},
                }
            ]
        )
    )

    assert request.tools[0].timeout == 20
    assert not hasattr(request.tools[0], "vendor_extension")


def test_required_text_is_trimmed_before_validation():
    request = ChatRequest(
        **request_payload(
            application_id="  app-1  ",
            conversation_id="  conversation-1  ",
            message_id="  message-1  ",
            question="  查询销售额  ",
        )
    )

    assert request.application_id == "app-1"
    assert request.conversation_id == "conversation-1"
    assert request.message_id == "message-1"
    assert request.question == "查询销售额"


@pytest.mark.parametrize(
    "field",
    ["application_id", "conversation_id", "message_id", "question"],
)
def test_required_text_rejects_blank_values(field):
    with pytest.raises(ValidationError):
        ChatRequest(**request_payload(**{field: " \t\r\n "}))


@pytest.mark.parametrize(
    "field", ["semantic_model_id", "business_domain_id", "database_id"]
)
@pytest.mark.parametrize("value", [0, -1, True, "1", 1.5])
def test_optional_model_and_domain_ids_reject_non_positive_or_non_integer_values(
    field, value
):
    with pytest.raises(ValidationError):
        ChatRequest(**request_payload(**{field: value}))


def test_optional_model_and_domain_ids_accept_positive_integers_or_none():
    request = ChatRequest(
        **request_payload(semantic_model_id=1, business_domain_id=None, database_id=5)
    )

    assert request.semantic_model_id == 1
    assert request.business_domain_id is None
    assert request.database_id == 5


def test_legacy_business_domain_id_is_normalized_to_new_list():
    request = ChatRequest(**request_payload(business_domain_id=13))
    assert request.business_domain_ids == [13]


def test_business_domain_ids_are_optional_deduplicated_and_sorted():
    request = ChatRequest(**request_payload(business_domain_ids=[18, 13, 18]))
    assert request.business_domain_id is None
    assert request.business_domain_ids == [13, 18]


def test_legacy_and_new_business_domain_fields_must_not_conflict():
    with pytest.raises(ValidationError):
        ChatRequest(
            **request_payload(
                business_domain_id=13,
                business_domain_ids=[13, 18],
            )
        )


@pytest.mark.parametrize("value", [[0], [-1], [True], ["13"], [1.5]])
def test_business_domain_ids_reject_invalid_values(value):
    with pytest.raises(ValidationError):
        ChatRequest(**request_payload(business_domain_ids=value))


def test_knowledge_base_names_are_trimmed_deduplicated_and_sorted():
    request = ChatRequest(
        **request_payload(
            knowledge_base_names=[" kb-z ", "kb-a", "kb-z", " kb-b"]
        )
    )

    assert request.knowledge_base_names == ["kb-a", "kb-b", "kb-z"]


def test_equivalent_knowledge_base_scopes_have_identical_serialization():
    first = ChatRequest(
        **request_payload(knowledge_base_names=["kb-b", " kb-a ", "kb-b"])
    )
    second = ChatRequest(
        **request_payload(knowledge_base_names=["kb-a", "kb-b"])
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


@pytest.mark.parametrize(
    "names",
    [
        ["   "],
        ["x" * 129],
        ["kb"] * 51,
        [1],
    ],
)
def test_knowledge_base_names_reject_invalid_items_or_excessive_input(names):
    with pytest.raises(ValidationError):
        ChatRequest(**request_payload(knowledge_base_names=names))


def test_history_allows_all_timestamps_to_be_omitted():
    request = ChatRequest(
        **request_payload(
            history=[
                {"role": "user", "content": "问题", "message_id": "old-1"},
                {"role": "assistant", "content": "回答", "message_id": "old-2"},
            ]
        )
    )

    assert all(item.created_at is None for item in request.history)


def test_history_accepts_timezone_aware_oldest_to_newest_timestamps():
    request = ChatRequest(
        **request_payload(
            history=[
                {
                    "role": "user",
                    "content": "问题",
                    "created_at": "2026-08-20T08:00:00+08:00",
                },
                {
                    "role": "assistant",
                    "content": "回答",
                    "created_at": "2026-08-20T01:00:00Z",
                },
            ]
        )
    )

    assert request.history[0].created_at < request.history[1].created_at


def test_history_rejects_partially_missing_timestamps():
    with pytest.raises(ValidationError, match="provided for every item"):
        ChatRequest(
            **request_payload(
                history=[
                    {
                        "role": "user",
                        "content": "问题",
                        "created_at": "2026-08-20T08:00:00+08:00",
                    },
                    {"role": "assistant", "content": "回答"},
                ]
            )
        )


def test_history_rejects_naive_timestamps():
    with pytest.raises(ValidationError, match="must include a timezone"):
        ChatRequest(
            **request_payload(
                history=[
                    {
                        "role": "user",
                        "content": "问题",
                        "created_at": datetime(2026, 8, 20, 8, 0),
                    }
                ]
            )
        )


def test_history_rejects_newest_to_oldest_timestamps():
    with pytest.raises(ValidationError, match="oldest to newest"):
        ChatRequest(
            **request_payload(
                history=[
                    {
                        "role": "user",
                        "content": "较新",
                        "created_at": datetime(2026, 8, 20, 2, tzinfo=timezone.utc),
                    },
                    {
                        "role": "assistant",
                        "content": "较旧",
                        "created_at": datetime(2026, 8, 20, 1, tzinfo=timezone.utc),
                    },
                ]
            )
        )


def test_chat_request_still_forbids_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ChatRequest(**request_payload(unknown_field="value"))
