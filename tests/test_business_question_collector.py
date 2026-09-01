from datetime import datetime, timezone

from app.services.business_question_collector import BusinessQuestionCollector


def test_collector_appends_readable_question_and_deduplicates_message(tmp_path):
    document_path = tmp_path / "实际业务问题.md"
    collector = BusinessQuestionCollector(document_path)
    recorded_at = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)

    assert collector.record(
        application_id="应用一",
        conversation_id="会话一",
        message_id="消息一",
        question="统计本月销售额\n并按城市分组",
        recorded_at=recorded_at,
    )
    assert not collector.record(
        application_id="应用一",
        conversation_id="会话一",
        message_id="消息一",
        question="统计本月销售额\n并按城市分组",
        recorded_at=recorded_at,
    )

    content = document_path.read_text(encoding="utf-8")
    assert content.count("# 实际业务问题") == 1
    assert content.count("统计本月销售额") == 1
    assert "> 并按城市分组" in content
    assert "2026-09-01 20:30:00 +0800" in content
    assert '- 应用："应用一"' in content
    assert '- 会话："会话一"' in content
    assert '- 消息："消息一"' in content


def test_collector_reloads_deduplication_keys_from_document(tmp_path):
    document_path = tmp_path / "实际业务问题.md"
    first = BusinessQuestionCollector(document_path)
    second = BusinessQuestionCollector(document_path)
    record = {
        "application_id": "app",
        "conversation_id": "conversation",
        "message_id": "message",
        "question": "查询经销商清单",
    }

    assert first.record(**record)
    assert not second.record(**record)
    assert document_path.read_text(encoding="utf-8").count("查询经销商清单") == 1
