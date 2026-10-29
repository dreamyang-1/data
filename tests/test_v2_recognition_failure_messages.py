from app.semantic_v2.recognition_client import RecognitionFailure


def test_context_too_large_does_not_blame_complete_user_question():
    message = RecognitionFailure(
        "V2_RECOGNITION_CONTEXT_TOO_LARGE",
        stage="v2_semantic_edits",
    ).public_message()

    assert "语义字段绑定" in message
    assert "候选目录" in message
    assert "当前问题并非缺少信息" in message
    assert "管理员" in message
    assert "V2_RECOGNITION_CONTEXT_TOO_LARGE" in message
    assert "重新表述完整问题" not in message


def test_schema_violation_names_exact_structural_field_and_rule():
    message = RecognitionFailure(
        "V2_MODEL_DYNAMIC_SCHEMA_VIOLATION",
        stage="v2_semantic_edits",
        instance_path=("filter_edits", 0, "operator"),
        validator="enum",
    ).public_message()

    assert "filter_edits.0.operator" in message
    assert "enum" in message
    assert "语义字段绑定" in message
    assert "动态 JSON Schema" in message
    assert "产品、品牌、医院" not in message


def test_unknown_recognition_failure_does_not_invent_missing_categories():
    message = RecognitionFailure("V2_UNRESOLVED_TEST").public_message()

    assert "足够证据" in message
    assert "V2_UNRESOLVED_TEST" in message
    assert "请确认名称或补充它属于" not in message
