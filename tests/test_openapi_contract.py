from app.config import Settings
from app.main import create_app


def schema() -> dict:
    return create_app(
        Settings(env="test", adapter_mode="mock", intent_model_enabled=False)
    ).openapi()


def test_sync_chat_documents_message_id_conflict():
    operation = schema()["paths"]["/agent_chat"]["post"]

    conflict = operation["responses"]["409"]
    assert conflict["content"]["application/json"]["example"]["detail"]["code"] == (
        "MESSAGE_ID_REUSE_CONFLICT"
    )


def test_stream_chat_is_documented_as_named_sse_not_json():
    operation = schema()["paths"]["/agent_chat/stream"]["post"]
    success_content = operation["responses"]["200"]["content"]

    assert set(success_content) == {"text/event-stream"}
    example = success_content["text/event-stream"]["example"]
    assert '"type":"updata_state"' in example
    assert '"type":"message_chunk"' in example
    assert '"type":"answer"' in example
    assert '"type":"complete"' in example
    assert "event:" not in example


def test_agent_response_exposes_clarification_state():
    response_schema = schema()["components"]["schemas"]["AgentResponse"]

    assert "understood_slots" in response_schema["properties"]
    assert "clarification_round" in response_schema["properties"]
    assert "clarification_items" in response_schema["properties"]
    item_schema = schema()["components"]["schemas"]["ClarificationItem"]
    assert set(item_schema["required"]) == {"slot", "title", "question"}


def test_agent_response_exposes_safe_user_visible_analysis_process():
    document = schema()
    response_schema = document["components"]["schemas"]["AgentResponse"]
    process_schema = document["components"]["schemas"]["AnalysisProcessStep"]

    assert "analysis_process" in response_schema["properties"]
    assert set(process_schema["required"]) == {"stage", "status", "title", "summary"}
    assert "DETERMINISTIC_ANALYSIS" in process_schema["properties"]["stage"]["enum"]
    assert "NEEDS_INPUT" in process_schema["properties"]["status"]["enum"]


def test_agent_response_exposes_structured_analysis_requirements():
    document = schema()
    response_schema = document["components"]["schemas"]["AgentResponse"]
    requirement_schema = document["components"]["schemas"]["AnalysisRequirement"]

    assert "requirements" in response_schema["properties"]
    assert set(requirement_schema["required"]) == {
        "code", "category", "description", "action"
    }


def test_openapi_marks_always_required_trusted_identity_headers():
    operation = schema()["paths"]["/agent_chat"]["post"]
    headers = {
        item["name"].lower(): item
        for item in operation["parameters"]
        if item["in"] == "header"
    }

    assert headers["x-tenant-id"]["required"] is True
    assert headers["x-user-id"]["required"] is True
    assert headers["x-application-id"]["required"] is False


def test_openapi_marks_application_header_required_in_production():
    production_schema = create_app(
        Settings(
            env="production",
            adapter_mode="http",
            session_store_mode="redis",
            redis_url="redis://localhost:6379/15",
            long_term_memory_mode="disabled",
            intent_model_enabled=False,
        )
    ).openapi()
    parameters = production_schema["paths"]["/agent_chat"]["post"][
        "parameters"
    ]
    application_header = next(
        item for item in parameters if item["name"].lower() == "x-application-id"
    )

    assert application_header["required"] is True


def test_removed_management_endpoints_are_absent_from_openapi():
    paths = schema()["paths"]
    assert "/v1/data-analysis/memory" not in paths
    assert "/v1/data-analysis/memory/candidates" not in paths
    assert "/v1/data-analysis/capabilities/skills" not in paths
    assert not any(path.endswith("/export-report") for path in paths)


def test_chat_schema_exposes_platform_extension_contract():
    document = schema()
    request_schema = document["components"]["schemas"]["ChatRequest"]
    properties = request_schema["properties"]
    assert {
        "tools", "skills", "mcp", "use_longterm_memory", "temp_file_paths",
        "database_id", "web_search",
    } <= set(properties)
    assert properties["use_longterm_memory"]["default"] is False
    assert properties["web_search"]["default"] is False
