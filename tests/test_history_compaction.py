import pytest
from pydantic import ValidationError

from app.domain.models import ChatRequest, HistoryMessage
from app.services.history_compaction import compact_history


def message(index: int, role: str = "user", content: str | None = None) -> HistoryMessage:
    return HistoryMessage(role=role, content=content or f"普通消息{index}", message_id=f"h-{index}")


def test_compaction_keeps_task_origin_correction_clarification_and_recent_turns():
    history = [message(index, "user" if index % 2 == 0 else "assistant") for index in range(70)]
    history[0] = message(0, "user", "查询今年销售额")
    history[10] = message(10, "user", "不是销售额，改成订单量")
    history[11] = message(11, "assistant", "已改成订单量")
    history[20] = message(20, "user", "继续分析")
    history[21] = message(21, "assistant", "还需要补充时间范围")

    compacted = compact_history(history)

    ids = [item.message_id for item in compacted]
    assert len(compacted) <= 40
    assert ids == sorted(ids, key=lambda value: int(value.split("-")[1]))
    assert {"h-0", "h-10", "h-11", "h-20", "h-21"}.issubset(ids)
    assert {f"h-{index}" for index in range(46, 70)}.issubset(ids)


def test_chat_request_accepts_100_history_items_but_rejects_unbounded_history():
    base = {"semantic_model_id": 81,
        "application_id": "app",
        "conversation_id": "conversation",
        "message_id": "current",
        "question": "本月销售额",
    }
    accepted = ChatRequest(**base, history=[message(index) for index in range(100)])
    assert len(accepted.history) == 100

    with pytest.raises(ValidationError):
        ChatRequest(**base, history=[message(index) for index in range(101)])


def test_chat_request_rejects_excessive_aggregate_history_content():
    base = {"semantic_model_id": 81,
        "application_id": "app",
        "conversation_id": "conversation",
        "message_id": "current",
        "question": "本月销售额",
    }
    with pytest.raises(ValidationError, match="120000"):
        ChatRequest(
            **base,
            history=[message(index, content="x" * 8000) for index in range(16)],
        )
